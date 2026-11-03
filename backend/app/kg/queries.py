"""多跳查询模板（与后端无关）。

对应开发文档 6.2 的 Q1–Q5，以及 API/Agent 复用的通用查询。
全部通过 GraphStore 的基本原语实现，因此 Neo4j 与 Memory 后端行为一致；
等价的 Cypher 见 docs/cypher.md（便于报告与答辩展示）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from .model import Node
from .store import GraphStore


def _out(store: GraphStore, node_id: str, rel: str) -> list[tuple[Node, dict[str, Any]]]:
    """出边：返回 (目标节点, 关系属性)。"""
    return [
        (node, edge.props)
        for edge, node in store.neighbors(node_id, [rel], direction="out", limit=500)
    ]


def _in(store: GraphStore, node_id: str, rel: str) -> list[tuple[Node, dict[str, Any]]]:
    return [
        (node, edge.props)
        for edge, node in store.neighbors(node_id, [rel], direction="in", limit=500)
    ]


def _by_code(store: GraphStore, label: str, code: str) -> Node | None:
    for node in store.nodes(label):
        if node.props.get("code") == code or node.props.get("modelCode") == code:
            return node
    return None


def resolve_model(store: GraphStore, model_code: str) -> Node | None:
    norm = model_code.strip().upper().replace("_", " ")
    for node in store.nodes("ProductModel"):
        if str(node.props.get("modelCode", "")).upper() == norm:
            return node
    return None


def find_error_code(store: GraphStore, code: str, model_id: str | None = None) -> list[Node]:
    out = []
    for node in store.nodes("ErrorCode"):
        if str(node.props.get("code")) != str(code):
            continue
        if model_id and not _is_about_model(store, node, model_id):
            continue
        out.append(node)
    return out


def _is_about_model(store: GraphStore, code_node: Node, model_id: str) -> bool:
    return any(n.id == model_id for n, _ in _in(store, code_node.id, "HAS_ERROR_CODE"))


# ---------------------------------------------------------------------------
# Q1 错误码 → 症状 → 故障 → 部件 → 根因 → 检查 → 方案 → 耗材/备件/文档（4~5 跳）
# ---------------------------------------------------------------------------
@dataclass
class DiagnoseRow:
    error_code: str
    symptom: str
    fault: str
    component: str
    cause: str
    checks: list[str] = field(default_factory=list)
    solutions: list[str] = field(default_factory=list)
    items: list[str] = field(default_factory=list)
    docs: list[str] = field(default_factory=list)
    confidence: float = 0.0
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "errorCode": self.error_code,
            "symptom": self.symptom,
            "fault": self.fault,
            "component": self.component,
            "cause": self.cause,
            "checks": self.checks,
            "solutions": self.solutions,
            "items": self.items,
            "docs": self.docs,
            "confidence": round(self.confidence, 3),
            "evidence": self.evidence,
        }


def diagnose_rows(store: GraphStore, code_node: Node) -> list[DiagnoseRow]:
    rows: list[DiagnoseRow] = []
    symptoms = _in(store, code_node.id, "HAS_CODE")
    for symptom, s_props in symptoms:
        for fault, i_props in _out(store, symptom.id, "INDICATES"):
            for comp, _ in _out(store, fault.id, "OCCURS_IN"):
                for cause, cb_props in _out(store, fault.id, "CAUSED_BY"):
                    checks = [n.name for n, _ in _out(store, cause.id, "CONFIRMED_BY")]
                    solutions, items, docs = [], [], []
                    for sol, _ in _out(store, cause.id, "RESOLVED_BY"):
                        solutions.append(sol.name)
                        items.extend(n.name for n, _ in _out(store, sol.id, "REQUIRES_CONSUMABLE"))
                        items.extend(n.name for n, _ in _out(store, sol.id, "REQUIRES_PART"))
                        docs.extend(n.name for n, _ in _out(store, sol.id, "DOCUMENTED_IN"))
                    rows.append(DiagnoseRow(
                        error_code=str(code_node.props.get("code")),
                        symptom=symptom.name,
                        fault=fault.name,
                        component=comp.name,
                        cause=cause.name,
                        checks=checks,
                        solutions=solutions,
                        items=items,
                        docs=list(dict.fromkeys(docs)),
                        confidence=round(float(s_props.get("confidence", 0)) * float(cb_props.get("confidence", 0)), 3),
                        evidence=[str(cb_props.get("evidence", ""))],
                    ))
    rows.sort(key=lambda r: -r.confidence)
    return rows


# ---------------------------------------------------------------------------
# Q2 某型号高频故障及涉及部件（统计工单）
# ---------------------------------------------------------------------------
def model_top_faults(store: GraphStore, model_code: str, limit: int = 10) -> list[dict[str, Any]]:
    model = resolve_model(store, model_code)
    if not model:
        return []
    counter: dict[str, dict[str, Any]] = {}
    for product, _ in _in(store, model.id, "INSTANCE_OF"):
        for ticket, _ in _in(store, product.id, "ABOUT"):
            for symptom, _ in _out(store, ticket.id, "OBSERVED"):
                for fault, _ in _out(store, symptom.id, "INDICATES"):
                    entry = counter.setdefault(fault.id, {
                        "fault": fault.name, "component": "", "caseCount": 0, "symptoms": set(),
                    })
                    entry["caseCount"] += 1
                    entry["symptoms"].add(symptom.name)
                    for comp, _ in _out(store, fault.id, "OCCURS_IN"):
                        entry["component"] = comp.name
    rows = sorted(counter.values(), key=lambda r: -r["caseCount"])[:limit]
    for r in rows:
        r["symptoms"] = sorted(r["symptoms"])[:5]
    return rows


# ---------------------------------------------------------------------------
# Q3 某客户近 N 天重复故障
# ---------------------------------------------------------------------------
def customer_repeats(store: GraphStore, customer_id: str, days: int = 90) -> list[dict[str, Any]]:
    cutoff = datetime.now() - timedelta(days=days)
    grouped: dict[tuple[str, str], list[str]] = {}
    for product, _ in _out(store, customer_id, "OWNS"):
        for ticket, t_props in _in(store, product.id, "ABOUT"):
            created = t_props.get("createdAt") or ticket.props.get("createdAt")
            if created:
                try:
                    if datetime.fromisoformat(str(created)) < cutoff:
                        continue
                except ValueError:
                    pass
            for symptom, _ in _out(store, ticket.id, "OBSERVED"):
                grouped.setdefault((product.name, symptom.name), []).append(
                    str(ticket.props.get("caseNo"))
                )
    rows = [
        {"product": p, "symptom": s, "count": len(cases), "cases": cases}
        for (p, s), cases in grouped.items() if len(cases) >= 2
    ]
    rows.sort(key=lambda r: -r["count"])
    return rows


# ---------------------------------------------------------------------------
# Q4 两个候选故障的鉴别性检查
# ---------------------------------------------------------------------------
def discriminating_checks(store: GraphStore, fault_a: str, fault_b: str) -> list[dict[str, Any]]:
    a = _by_code(store, "Fault", fault_a)
    b = _by_code(store, "Fault", fault_b)
    if not a or not b:
        return []
    b_detectors = {n.id for n, _ in _in(store, b.id, "DETECTS")}
    rows = []
    for chk, _ in _in(store, a.id, "DETECTS"):
        if chk.id in b_detectors:
            continue
        rows.append({
            "check": chk.name,
            "risk": chk.props.get("riskLevel", "low"),
            "durationMin": chk.props.get("durationMin", 0),
            "method": chk.props.get("method", ""),
        })
    order = {"low": 0, "medium": 1, "high": 2}
    rows.sort(key=lambda r: (order.get(r["risk"], 3), r["durationMin"]))
    return rows


# ---------------------------------------------------------------------------
# Q5 同一错误码在不同型号下的含义
# ---------------------------------------------------------------------------
def code_across_models(store: GraphStore, code: str) -> list[dict[str, Any]]:
    rows = []
    for node in find_error_code(store, code):
        for model, props in _in(store, node.id, "HAS_ERROR_CODE"):
            rows.append({
                "model": model.props.get("modelCode"),
                "code": node.props.get("code"),
                "meaning": props.get("meaning") or node.props.get("meaning"),
                "severity": props.get("severity") or node.props.get("severity"),
                "modelCount": len(_in(store, node.id, "HAS_ERROR_CODE")),
            })
    return sorted(rows, key=lambda r: str(r["model"]))


# ---------------------------------------------------------------------------
# 案例上下文与相似案例
# ---------------------------------------------------------------------------
def case_context(store: GraphStore, case_no: str) -> dict[str, Any] | None:
    case = next((n for n in store.nodes("Case") if n.props.get("caseNo") == case_no), None)
    if not case:
        return None
    symptoms = [n for n, _ in _out(store, case.id, "OBSERVED")]
    about = [n for n, _ in _out(store, case.id, "ABOUT")]
    model = None
    product = None
    for node, _ in _out(store, case.id, "ABOUT"):
        if node.label == "Product":
            product = node
            parent = _out(store, node.id, "INSTANCE_OF")
            model = parent[0][0] if parent else None
        elif node.label == "ProductModel":
            model = node
    customer = _out(store, case.id, "REPORTED_BY")
    solutions = [n for n, _ in _out(store, case.id, "RESOLVED_BY")]
    return {
        "caseNo": case_no,
        "caseType": case.label,
        "sourceType": case.props.get("sourceType"),
        "createdAt": case.props.get("createdAt"),
        "rawText": case.props.get("rawText"),
        "model": model.props.get("modelCode") if model else None,
        "product": product.props.get("serialNo") if product else None,
        "customer": customer[0][0].props.get("customerId") if customer else None,
        "symptoms": [s.name for s in symptoms],
        "solutions": [s.name for s in solutions],
        "evidence": [str(p.get("evidence", "")) for _, p in _out(store, case.id, "OBSERVED")],
    }


def similar_cases(store: GraphStore, model_code: str, symptom_name: str,
                  days: int = 90, limit: int = 10) -> list[dict[str, Any]]:
    model = resolve_model(store, model_code)
    if not model:
        return []
    cutoff = datetime.now() - timedelta(days=days)
    rows = []
    for product, _ in _in(store, model.id, "INSTANCE_OF"):
        for case, _ in _in(store, product.id, "ABOUT"):
            symptoms = [n.name for n, _ in _out(store, case.id, "OBSERVED")]
            if not any(symptom_name in s or s in symptom_name for s in symptoms):
                continue
            created = str(case.props.get("createdAt") or "")
            if created:
                try:
                    if datetime.fromisoformat(created) < cutoff:
                        continue
                except ValueError:
                    pass
            solutions = [n.name for n, _ in _out(store, case.id, "RESOLVED_BY")]
            rows.append({
                "caseNo": case.props.get("caseNo"),
                "caseType": case.label,
                "sourceType": case.props.get("sourceType"),
                "symptoms": symptoms,
                "solutions": solutions,
                "createdAt": created,
            })
    rows.sort(key=lambda r: r["createdAt"], reverse=True)
    return rows[:limit]


def solution_success_rate(store: GraphStore, model_code: str, symptom_name: str) -> list[dict[str, Any]]:
    """从工单统计「同型号同症状」下各方案的历史成功率（R18 使用）。"""
    stats: dict[str, dict[str, int]] = {}
    for case in similar_cases(store, model_code, symptom_name, days=365, limit=200):
        if case["caseType"] != "Ticket":
            continue
        for sol in case["solutions"]:
            entry = stats.setdefault(sol, {"success": 0, "total": 0})
            entry["total"] += 1
            entry["success"] += 1 if case.get("resolved", True) else 0
    return [
        {"solution": k, "rate": round(v["success"] / v["total"], 3), "n": v["total"]}
        for k, v in sorted(stats.items(), key=lambda kv: -kv[1]["total"])
        if v["total"] >= 1
    ]


def upcoming_window(days: int) -> date:
    return date.today() + timedelta(days=days)
