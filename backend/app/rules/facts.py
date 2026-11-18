"""把图谱投影为规则引擎的事实集。

这一层承担「图 → 逻辑」的翻译：
  * 图中的边变成二元/三元谓词；
  * 需要聚合或时间比较的谓词（重复报修、历史成功率）在这里预计算，
    而不是塞进规则表达式，避免规则文件里出现脆弱的时间算术。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable

from ..kg import queries as Q
from ..kg.model import Node
from ..kg.store import GraphStore
from .engine import Atom

QUERY_CASE = "CASE:QUERY"


def _nbrs(store, node_id: str, rels, direction: str = "out", limit: int = 100):
    """store.neighbors 返回 (edge, node)；这里统一转成 (node, edge.props)，更贴合业务代码。"""
    return [(node, edge.props) for edge, node in store.neighbors(node_id, rels, direction, limit)]


@dataclass
class FactProjection:
    atoms: list[Atom] = field(default_factory=list)
    case_id: str = QUERY_CASE
    model: str | None = None
    product: str | None = None
    customer: str | None = None
    symptoms: list[Node] = field(default_factory=list)
    error_codes: list[Node] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def add(self, pred: str, *args: Any) -> None:
        self.atoms.append((pred, *(str(a) for a in args)))


def _cf(props: dict[str, Any]) -> float:
    try:
        return round(float(props.get("confidence", 0.0)), 4)
    except (TypeError, ValueError):
        return 0.0


def _tickets_of(store: GraphStore, product_id: str) -> list[Node]:
    return [n for n, _ in _nbrs(store, product_id, ["ABOUT"], direction="in", limit=500)
            if n.label == "Ticket"]


def _resolve_symptom(store: GraphStore, text: str) -> Node | None:
    hits = [n for n in store.search(text, label="Symptom", limit=5)]
    if hits:
        return hits[0]
    for node in store.nodes("Symptom"):
        if str(node.props.get("code", "")).upper() == text.upper():
            return node
    return None


def build_facts(
    store: GraphStore,
    case_no: str | None = None,
    model: str | None = None,
    code: str | None = None,
    symptom: str | None = None,
    check_results: Iterable[dict[str, str]] | None = None,
    actions: Iterable[str] | None = None,
    today: datetime | None = None,
) -> FactProjection:
    today = today or datetime.now()
    fp = FactProjection()

    # ---------- 1. 定位案例 ----------
    case: Node | None = None
    if case_no:
        case = next((n for n in store.nodes("Case") if n.props.get("caseNo") == case_no), None)
        if case is None:
            raise ValueError(f"未找到案例 {case_no}")
        fp.case_id = case.id
        fp.symptoms = [n for n, _ in _nbrs(store, case.id, ["OBSERVED"], "out", 50)]
        for node, _ in _nbrs(store, case.id, ["ABOUT"], "out", 5):
            if node.label == "Product":
                fp.product = node.id
                parent = store.neighbors(node.id, ["INSTANCE_OF"], "out", 1)
                fp.model = parent[0][0].id if parent else None
            elif node.label == "ProductModel":
                fp.model = node.id
        for node, _ in _nbrs(store, case.id, ["REPORTED_BY"], "out", 1):
            fp.customer = node.id
        fp.notes.append(f"案例 {case_no}（{case.label}）")

    if fp.model is None and model:
        node = Q.resolve_model(store, model)
        if node is None:
            raise ValueError(f"未找到型号 {model}")
        fp.model = node.id

    # 错误码：按型号解析，避免跨型号串码
    if code:
        nodes = Q.find_error_code(store, code, fp.model) if fp.model else Q.find_error_code(store, code)
        fp.error_codes = nodes
        if not nodes:
            raise ValueError(f"未找到错误码 {code}" + (f"（型号 {model}）" if model else ""))
        if not fp.symptoms:
            for node in nodes:
                for sym, _ in _nbrs(store, node.id, ["HAS_CODE"], "in", 5):
                    if sym not in fp.symptoms:
                        fp.symptoms.append(sym)

    if not fp.symptoms and symptom:
        node = _resolve_symptom(store, symptom)
        if node is None:
            raise ValueError(f"未找到症状 {symptom}")
        fp.symptoms.append(node)

    if not fp.symptoms:
        # 案例卡没有观察症状是正常情况：官方 FAQ 里的保养/操作类案例
        # （如「如何清理边刷」）只记录步骤，不对应任何故障。
        # 这类问题由 service._case_detail 按案例详情回答，不能当成「用户没说清」。
        # case_id 默认是哨兵 CASE:QUERY，只有节点真实存在才算「有案例」。
        if store.get(fp.case_id) is not None:
            fp.notes.append("案例未记录观察症状，按案例详情回答")
        else:
            raise ValueError("无法确定症状：请提供 caseNo、symptom 或 code")

    T = fp.case_id
    fp.add("observed_case", T)
    if fp.product:
        fp.add("case_about", T, fp.product)
    if fp.model:
        fp.add("case_about_model", T, fp.model)
    if fp.customer:
        fp.add("case_reported_by", T, fp.customer)

    # ---------- 2. 型号事实 ----------
    if fp.model:
        model_node = store.get(fp.model)
        if model_node:
            fp.add("model_code", fp.model, model_node.props.get("modelCode", ""))
        for comp, _ in _nbrs(store, fp.model, ["HAS_COMPONENT"], "out", 100):
            fp.add("model_has_component", fp.model, comp.id)
        for con, _ in _nbrs(store, fp.model, ["USES_CONSUMABLE"], "out", 100):
            fp.add("model_uses_consumable", fp.model, con.id)
        for item_label in ("Consumable", "SparePart"):
            for item in store.nodes(item_label):
                compat = [node for node, _ in _nbrs(store, item.id, ["COMPATIBLE_WITH"], "out", 20)]
                flag = 1 if any(node.id == fp.model for node in compat) else 0
                fp.add("compatible_flag", item.id, fp.model, flag)

    # 设备事实
    if fp.product:
        pnode = store.get(fp.product)
        if pnode:
            fp.add("warranty_status", fp.product, pnode.props.get("warrantyStatus", "unknown"))
        for fw, _ in _nbrs(store, fp.product, ["RUNS_FIRMWARE"], "out", 3):
            fp.add("runs_firmware", fp.product, fw.id)
            for fault, _ in _nbrs(store, fw.id, ["KNOWN_ISSUE"], "out", 20):
                fp.add("known_issue", fw.id, fault.id)
    if fp.customer:
        for product, _ in _nbrs(store, fp.customer, ["OWNS"], "out", 5):
            for env, _ in _nbrs(store, product.id, ["USED_IN"], "out", 5):
                fp.add("home_environment", fp.customer, env.props.get("code", ""))

    # ---------- 3. 症状 → 错误码 → 故障 ----------
    reachable_faults: set[str] = set()
    for sym in fp.symptoms:
        fp.add("observed", T, sym.id)
        if sym.props.get("severity") == "critical":
            fp.add("high_risk_symptom", sym.id)
        codes = [n for n, _ in _nbrs(store, sym.id, ["HAS_CODE"], "out", 20)]
        for code_node in codes:
            fp.add("has_code", sym.id, code_node.id)
            if fp.model and any(m.id == fp.model for m, _ in _nbrs(store, code_node.id, ["HAS_ERROR_CODE"], "in", 5)):
                fp.add("error_applies_to", code_node.id, fp.model)
            family = code_node.props.get("family", "")
            fp.add("error_family", code_node.id, family)
            cat = [n for n, _ in _nbrs(store, code_node.id, ["HAS_CATEGORY"], "out", 2)]
            if cat and cat[0].props.get("code") in ("SAFETY", "HARDWARE"):
                fp.add("code_category", code_node.id, cat[0].props.get("code"))
            if cat and cat[0].props.get("code") == "SAFETY":
                fp.add("high_risk_symptom", sym.id)

        faults = [(n, p) for n, p in _nbrs(store, sym.id, ["INDICATES"], "out", 20)]
        fp.add("symptom_fault_count", sym.id, len(faults))
        for fault, props in faults:
            reachable_faults.add(fault.id)
            fp.add("symptom_indicates", sym.id, fault.id, _cf(props))

    # ---------- 4. 故障 → 根因 → 检查 → 方案 ----------
    for fault in store.nodes("Fault"):
        if fault.id not in reachable_faults:
            continue
        fp.add("fault_code", fault.id, fault.props.get("code", ""))
        for comp, _ in _nbrs(store, fault.id, ["OCCURS_IN"], "out", 5):
            fp.add("fault_occurs_in", fault.id, comp.id)
            fp.add("fault_component_code", fault.id, comp.props.get("code", ""))
        for cat, _ in _nbrs(store, fault.id, ["IN_CATEGORY"], "out", 3):
            fp.add("fault_in_category", fault.id, cat.props.get("code", ""))
        for cause, cprops in _nbrs(store, fault.id, ["CAUSED_BY"], "out", 20):
            fp.add("fault_caused_by", fault.id, cause.id, _cf(cprops))
            for chk, _ in _nbrs(store, cause.id, ["CONFIRMED_BY"], "out", 10):
                fp.add("cause_confirmed_by", cause.id, chk.id)
            for sol, sprops in _nbrs(store, cause.id, ["RESOLVED_BY"], "out", 20):
                fp.add("cause_resolved_by", cause.id, sol.id, _cf(sprops))

    # 方案与检查的静态属性
    for sol in store.nodes("Solution"):
        docs = [n for n, _ in _nbrs(store, sol.id, ["DOCUMENTED_IN"], "out", 10)]
        authority = max((int(n.props.get("authority", 3) or 3) for n in docs), default=0)
        fp.add("solution_authority", sol.id, authority)
        fp.add("solution_type", sol.id, sol.props.get("type", ""))
        fp.add("solution_risk", sol.id, sol.props.get("riskLevel", "low"))
        items = [n for n, _ in _nbrs(store, sol.id, ["REQUIRES_CONSUMABLE", "REQUIRES_PART"], "out", 10)]
        for item in items:
            fp.add("solution_requires_item", sol.id, item.id)
        fp.add("solution_part_count", sol.id, len([n for n, _ in _nbrs(store, sol.id, ["REQUIRES_PART"], "out", 10)]))
        fp.add("solution_consumable_count", sol.id,
               len([n for n, _ in _nbrs(store, sol.id, ["REQUIRES_CONSUMABLE"], "out", 10)]))
        for other, _ in _nbrs(store, sol.id, ["PRECEDES"], "out", 10):
            fp.add("solution_precedes", sol.id, other.id)
    for chk in store.nodes("Check"):
        risk = {"low": 0, "medium": 1, "high": 2}.get(str(chk.props.get("riskLevel", "low")), 3)
        fp.add("check_risk_num", chk.id, risk)
        fp.add("check_duration", chk.id, chk.props.get("durationMin", 5) or 5)
        fp.add("check_name", chk.id, chk.props.get("name", ""))
        for fault, _ in _nbrs(store, chk.id, ["DETECTS"], "out", 10):
            fp.add("check_detects", chk.id, fault.id)
    # ---------- 5. 聚合事实：重复报修、历史成功率、重复工单 ----------
    if fp.product:
        for sym in fp.symptoms:
            cutoff = today - timedelta(days=90)
            count = 0
            for ticket in _tickets_of(store, fp.product):
                created = str(ticket.props.get("createdAt") or "")
                try:
                    if created and datetime.fromisoformat(created) < cutoff:
                        continue
                except ValueError:
                    pass
                if any(s.id == sym.id for s, _ in _nbrs(store, ticket.id, ["OBSERVED"], "out", 5)):
                    count += 1
            if count:
                fp.add("repeated_issue", fp.product, sym.id, count)

    if fp.model:
        for sym in fp.symptoms:
            for row in Q.solution_success_rate(store, _model_code(store, fp.model), sym.name):
                fp.add("solution_success", _solution_id(store, row["solution"]), row["rate"], row["n"])
                if row["n"] >= 2:
                    fp.add("historical_solution", _solution_id(store, row["solution"]), row["rate"], row["n"])

    for case_node in store.nodes("Case"):
        for other, _props in _nbrs(store, case_node.id, ["DUPLICATE_OF"], "out", 5):
            fp.add("same_issue_within_24h", case_node.id, other.id)

    # ---------- 6. 用户提供的检查结果 ----------
    for result in check_results or []:
        k = _resolve_node_ref(store, result.get("check"), "Check")
        f = _resolve_node_ref(store, result.get("fault"), "Fault")
        outcome = result.get("result", "positive")
        if k and f:
            fp.add("check_result", k.id, f.id, outcome)

    # ---------- 7. 用户已经做过的动作（P2：事实来自用户自述，不是我们的推荐） ----------
    # 动作事实与「检查结果」分开：用户说「清理过了」并不是一次规范化检查，
    # 但它在图谱里有明确语义（RESOLVED_BY / 方案属性 excludesCauses），由规则消费。
    for ref in actions or []:
        sol = _resolve_solution(store, str(ref))
        if sol is not None:
            fp.add("action_attempted", sol.id)
    for sol in store.nodes("Solution"):
        for cause_code in sol.props.get("excludesCauses") or []:
            cause = _resolve_cause(store, str(cause_code))
            if cause is not None:
                fp.add("solution_effect", sol.id, cause.id, "excludes")
        if sol.props.get("exhaustsSelf"):
            fp.add("solution_effect", sol.id, sol.id, "exhausts")

    return fp


def _resolve_solution(store: GraphStore, ref: str) -> Node | None:
    """动作可以按节点 id、方案 code 或中文名引用，统一解析成 Solution 节点。"""
    node = store.get(ref)
    if node is not None and node.label == "Solution":
        return node
    for sol in store.nodes("Solution"):
        if str(sol.props.get("code", "")).upper() == ref.upper() or sol.name == ref:
            return sol
    return None


def _resolve_cause(store: GraphStore, ref: str) -> Node | None:
    node = store.get(ref)
    if node is not None and node.label == "Cause":
        return node
    for cause in store.nodes("Cause"):
        if str(cause.props.get("code", "")).upper() == ref.upper():
            return cause
    return None


def _resolve_node_ref(store: GraphStore, ref: str | None, label: str) -> Node | None:
    """检查/故障既可能按节点 id 传，也可能按 code（CHK-…/LEFT-WHEEL-STUCK）传，统一归一化。

    归一化很重要：规则里的 CONFIRMED_BY / DETECTS 连接的是节点 id，
    外部传入的 code 若不归一化，check_result 永远连不上 cause_confirmed_by。
    """
    if not ref:
        return None
    node = store.get(ref)
    if node is not None and node.label == label:
        return node
    for cand in store.nodes(label):
        if str(cand.props.get("code", "")).upper() == ref.upper():
            return cand
    return None


def _model_code(store: GraphStore, model_id: str) -> str:
    node = store.get(model_id)
    return str(node.props.get("modelCode", "")) if node else ""


def _solution_id(store: GraphStore, name: str) -> str:
    for sol in store.nodes("Solution"):
        if sol.name == name or sol.props.get("code") == name:
            return sol.id
    return f"SOL:{name}"


def atoms_to_dicts(atoms: Iterable[Atom]) -> list[dict[str, Any]]:
    return [{"pred": a[0], "args": list(a[1:])} for a in atoms]
