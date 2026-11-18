"""诊断服务：事实投影 → 规则推理 → 排序 → 证据与证明子图。

规则模式（mode=rule）完全确定性、不调用 LLM；Agent 模式在此基础上做查询解耦与覆盖度重试。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from ..config import get_settings
from ..kg import queries as Q
from ..kg.model import Edge
from ..kg.ontology import get_ontology
from ..kg.store import GraphStore
from ..rules.engine import Engine, InferenceResult
from ..rules.facts import FactProjection, build_facts
from ..rules.loader import load_rules

SAFETY_WARNING = (
    "涉及电池鼓包/漏液/过热/进水/冒烟等情况，请立即停止使用与充电，远离可燃物，"
    "并联系科沃斯官方售后；切勿自行拆解电池或短接保护。"
)
DISCLAIMER = "以上判断仅用于辅助排查，最终以官方手册与售后检测为准。"
# 这些策略表示「查询本身落空」，不是推理结论，Agent 应直接返回而不再重试
LOOKUP_POLICIES = {"code_not_applicable", "unknown_code", "unknown_model", "unknown_intent"}
# 用户显式做跨型号比较时的措辞（同一错误码在不同型号下含义可能不同）
CROSS_MODEL_HINTS = ("含义", "一样吗", "是否一样", "不同型号", "两个型号", "跨型号", "都报", "分别")


def _nbrs(store, node_id: str, rels, direction: str = "out", limit: int = 100):
    """store.neighbors 返回 (edge, node)；这里统一转成 (node, edge.props)，更贴合业务代码。"""
    return [(node, edge.props) for edge, node in store.neighbors(node_id, rels, direction, limit)]


@dataclass
class Diagnosis:
    case_id: str
    model: str | None
    symptoms: list[dict[str, Any]]
    faults: list[dict[str, Any]]
    causes: list[dict[str, Any]]
    solutions: list[dict[str, Any]]
    checks: list[dict[str, Any]]
    warnings: list[str]
    policies: list[str]
    confidence: float
    answer: str
    claims: list[dict[str, Any]]
    evidence: list[dict[str, Any]]
    proof_tree: dict[str, Any] | None
    graph: dict[str, Any]
    trace: dict[str, Any]
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "caseId": self.case_id,
            "model": self.model,
            "symptoms": self.symptoms,
            "faults": self.faults,
            "causes": self.causes,
            "solutions": self.solutions,
            "checks": self.checks,
            "warnings": self.warnings,
            "policies": self.policies,
            "confidence": round(self.confidence, 3),
            "answer": self.answer,
            "claims": self.claims,
            "evidence": self.evidence,
            "proof": {"mode": "rule", "derivationTree": self.proof_tree},
            "graph": self.graph,
            "trace": self.trace,
            "notes": self.notes,
        }


_engine: Engine | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        s = get_settings()
        _engine = Engine(load_rules(), s.rule_max_iterations, s.rule_max_depth)
    return _engine


def diagnose(
    store: GraphStore,
    case_no: str | None = None,
    model: str | None = None,
    code: str | None = None,
    symptom: str | None = None,
    check_results: Iterable[dict[str, str]] | None = None,
    question: str | None = None,
    actions: Iterable[str] | None = None,
) -> Diagnosis:
    """question 仅用于识别「跨型号比较」这类问法，不参与推理。

    actions 是用户自述已做过的方案（P2 多轮事实），按图谱 Solution.excludesCauses 参与排除。
    """
    try:
        fp = build_facts(store, case_no=case_no, model=model, code=code,
                         symptom=symptom, check_results=check_results, actions=actions)
    except ValueError as exc:
        fallback = _lookup_fallback(store, str(exc), model=model, code=code)
        if fallback is None:
            raise
        return fallback
    # fp.case_id 默认是哨兵值 CASE:QUERY，必须确认该节点真实存在才走案例分支
    if fp.case_id and not fp.symptoms and store.get(fp.case_id) is not None:
        return _case_detail(store, fp)
    result = get_engine().run(fp.atoms)
    d = _assemble(store, fp, result)
    if question and code and any(h in question for h in CROSS_MODEL_HINTS):
        note = _cross_model_note(store, code)
        if note:
            d.answer = (d.answer[: -len(DISCLAIMER)] + " " + note + " " + DISCLAIMER
                        if d.answer.endswith(DISCLAIMER) else d.answer + " " + note)
            d.warnings.append(note)
            d.policies = sorted(set(d.policies) | {"cross_model_compare"})
    return d


def _cross_model_note(store: GraphStore, code: str) -> str | None:
    """同一错误码在多个型号下都有定义时，给出逐型号含义。"""
    rows = Q.code_across_models(store, code)
    if len(rows) < 2:
        return None
    parts = [f"{r['model']}：{r.get('meaning') or '未标注含义'}" for r in rows]
    return "同名错误码按型号解读（" + "；".join(parts) + "），跨型号不可直接套用同一处置。"


# ---------------------------------------------------------------------------
# 案例卡：没有症状也要答得出
# ---------------------------------------------------------------------------
CASE_KIND_LABEL = {
    "maintenance": "官方保养/操作步骤",
    "usage": "官方使用说明",
    "troubleshoot": "官方排查案例",
}


def _case_detail(store: GraphStore, fp: FactProjection) -> Diagnosis:
    """案例卡没有观察症状时，直接按案例详情作答。

    官方 FAQ 里相当一部分是保养/操作类（「如何清理边刷」），只记步骤、不记故障。
    这类卡进图后若仍走规则推理，会因缺症状被判成「输入不足」，
    把本可回答的问题答成「请补充型号」——这正是接入真实案例后暴露的缺陷。
    """
    case = store.get(fp.case_id)
    if case is None:
        raise ValueError(f"未找到案例 {fp.case_id}")
    onto = get_ontology()
    kind = str(case.props.get("kind") or "troubleshoot")
    kind_label = CASE_KIND_LABEL.get(kind, "官方案例")
    case_no = case.props.get("caseNo", case.id)

    doc_nodes: list[Any] = []
    edges: list[Edge] = []
    for doc, props in _nbrs(store, case.id, ["DOCUMENTED_IN"], "out", 2):
        doc_nodes.append(doc)
        edges.append(Edge("DOCUMENTED_IN", case.id, doc.id, props))

    solutions: list[dict[str, Any]] = []
    for sol, props in _nbrs(store, case.id, ["RESOLVED_BY"], "out", 5):
        items = [n for n, _ in _nbrs(store, sol.id, ["REQUIRES_CONSUMABLE", "REQUIRES_PART"], "out", 5)]
        score = round(float(props.get("confidence", 0.8)), 3)
        solutions.append({
            "id": sol.id,
            "name": sol.name,
            "type": sol.props.get("type"),
            "riskLevel": sol.props.get("riskLevel"),
            "selfServiceable": sol.props.get("selfServiceable"),
            "estimatedMin": sol.props.get("estimatedMin"),
            "score": score,
            "baseScore": score,
            "reasons": ["case_resolved_by"],
            "items": [{"name": n.name, "sku": n.props.get("sku"), "type": n.label} for n in items],
            "docs": [{"title": d.name, "docId": d.props.get("docId"), "url": d.props.get("url")}
                     for d in doc_nodes],
        })
        edges.append(Edge("RESOLVED_BY", case.id, sol.id, props))

    # 证明子图：案例 → 处置动作 → 原文出处（与推理路径同构，前端可复用同一套渲染）
    nodes: list[dict[str, Any]] = []
    for step, node in [(0, case),
                       *[(1, store.get(s["id"])) for s in solutions],
                       *[(2, d) for d in doc_nodes]]:
        if node is None:
            continue
        cls = onto.classes.get(node.label)
        nodes.append({
            "id": node.id,
            "label": node.label,
            "name": node.name,
            "risk": node.props.get("riskLevel"),
            "step": step,
            "isProof": True,
            "color": cls.viz.get("color") if cls else "#64748b",
            "shape": cls.viz.get("shape") if cls else "round-rectangle",
        })
    nodes.sort(key=lambda n: (n["step"], n["id"]))
    graph: dict[str, Any] = {
        "nodes": nodes,
        "edges": [{
            "id": e.id, "source": e.head, "target": e.tail, "type": e.type,
            "confidence": e.confidence, "derived": False, "step": 1 if e.type == "RESOLVED_BY" else 2,
            "evidenceId": None, "props": e.props,
        } for e in edges],
        "meta": {"layout": "dagre", "proofMode": "case", "truncated": False},
    }
    evidence = _build_evidence(graph)
    for edge, ev in zip(graph["edges"], evidence):
        edge["evidenceId"] = ev["id"]

    raw = " ".join(str(case.props.get("rawText") or "").split())
    if len(raw) > 160:
        raw = raw[:160] + "…"
    text = (f"案例 {case_no}《{case.name}》是{kind_label}"
            f"（{case.props.get('stepCount', 0)} 步）")
    if solutions:
        text += f"，对应处置动作「{solutions[0]['name']}」"
    text += "。该案例未记录故障症状，因此不做故障诊断，按原文步骤执行即可。"
    if raw:
        text += f" 原文要点：{raw}"
    if doc_nodes:
        text += f" 出处：{doc_nodes[0].props.get('docId')}。"

    claims = []
    if solutions:
        claims.append({
            "text": f"该案例的处置动作：{solutions[0]['name']}",
            "ruleIds": [],
            "evidenceIds": [e["evidenceId"] for e in graph["edges"]
                            if e["type"] == "RESOLVED_BY" and e.get("evidenceId")],
            "confidence": solutions[0]["score"],
        })
    claims.append({
        "text": f"案例 {case_no} 出自官方 FAQ，原文共 {case.props.get('stepCount', 0)} 步",
        "ruleIds": [],
        "evidenceIds": [e["evidenceId"] for e in graph["edges"]
                        if e["type"] == "DOCUMENTED_IN" and e.get("evidenceId")],
        "confidence": 1.0,
    })

    return Diagnosis(
        case_id=case.id,
        model=fp.model,
        symptoms=[],
        faults=[], causes=[], checks=[],
        solutions=solutions,
        warnings=[],
        policies=["case_detail"],
        confidence=0.0,
        answer=text + DISCLAIMER,
        claims=claims,
        evidence=evidence,
        proof_tree=None,
        graph=graph,
        trace={"iterations": 0, "factCount": 0, "firedRules": [],
               "ruleCount": len(load_rules()), "backend": store.backend,
               "note": "case_detail"},
        notes=fp.notes,
    )


# ---------------------------------------------------------------------------
# 查询落空时的优雅降级
# ---------------------------------------------------------------------------
def _empty_diagnosis(
    *,
    answer: str,
    warnings: list[str],
    policies: list[str],
    notes: list[str],
    model: str | None = None,
    trace_note: str = "lookup_failed",
    symptoms: list[dict[str, Any]] | None = None,
) -> Diagnosis:
    return Diagnosis(
        case_id="CASE:QUERY",
        model=model,
        symptoms=symptoms or [],
        faults=[], causes=[], solutions=[], checks=[],
        warnings=warnings,
        policies=policies,
        confidence=0.0,
        answer=answer,
        claims=[], evidence=[], proof_tree=None,
        graph={"nodes": [], "edges": [],
               "meta": {"layout": "dagre", "proofMode": "none", "truncated": False}},
        trace={"note": trace_note, "iterations": 0, "factCount": 0,
               "firedRules": [], "backend": "n/a"},
        notes=notes,
    )


def _lookup_fallback(store: GraphStore, message: str, model: str | None,
                     code: str | None) -> Diagnosis | None:
    """把「错误码/型号查不到」从异常变成可解释的结论。

    最典型的是跨型号串码：同一个 E42（水路异常）只存在于部分带水箱机型上，
    用户在另一型号上问 E42 时，正确行为是提示型号与错误码不匹配，
    而不是抛 422 —— 这本身就是知识图谱「按型号展开错误码」的价值体现。
    """
    if code and "未找到错误码" in message:
        rows = Q.code_across_models(store, code)
        model_label = model or "该型号"
        if rows:
            owners = sorted({str(r["model"]) for r in rows if r.get("model")})
            return _empty_diagnosis(
                answer=(
                    f"图谱中 {code} 未定义在 {model_label} 上，它属于：{'、'.join(owners)}。"
                    f"请先核对机身铭牌型号是否看错；若设备确实报 {code}，"
                    f"请以屏幕提示的错误码表为准重新提问。 " + DISCLAIMER
                ),
                warnings=[f"错误码 {code} 与型号 {model_label} 不匹配，已停止推理以避免跨型号串码。"]
                + [f"{code} 在 {r['model']} 的含义：{r.get('meaning') or '未标注'}" for r in rows],
                policies=["code_not_applicable"],
                notes=[f"{code} 在 {len(rows)} 条「型号 × 错误码」记录中有定义"],
                model=Q.resolve_model(store, model).id if model and Q.resolve_model(store, model) else None,
                trace_note="code_not_applicable",
            )
        return _empty_diagnosis(
            answer=f"图谱中未定义错误码 {code}，请核对屏幕提示或说明书错误码表后重新提问。 " + DISCLAIMER,
            warnings=[f"错误码 {code} 不在本体覆盖范围内。"],
            policies=["unknown_code"],
            notes=["错误码未命中任何型号"],
            trace_note="unknown_code",
        )

    if "无法确定症状" in message:
        return _empty_diagnosis(
            answer=(
                "没能从问题中识别出型号、错误码或症状。请补充型号（如 T20 PRO）、"
                "错误码（如 E11）或具体症状（如「左轮不转」）；本助手只覆盖科沃斯 "
                "DEEBOT T 系列（当前语料为 T20 PRO）。 " + DISCLAIMER
            ),
            warnings=["输入信息不足，未触发任何推理规则。"],
            policies=["unknown_intent"],
            notes=["缺少 model / code / symptom"],
            trace_note="unknown_intent",
        )

    if model and "未找到型号" in message:
        codes = sorted({str(n.props.get("modelCode")) for n in store.nodes("ProductModel")})
        return _empty_diagnosis(
            answer=(
                f"图谱中未收录型号「{model}」。当前覆盖：{'、'.join(codes)}。"
                "请提供正确的型号后重试。 " + DISCLAIMER
            ),
            warnings=[f"型号「{model}」不在图谱覆盖范围内。"],
            policies=["unknown_model"],
            notes=["可用型号见提示"],
            trace_note="unknown_model",
        )
    return None


# ---------------------------------------------------------------------------
# 结果组装
# ---------------------------------------------------------------------------
def _assemble(store: GraphStore, fp: FactProjection, result: InferenceResult) -> Diagnosis:
    warnings: list[str] = []
    policies: list[str] = []

    # ---- 症状与候选故障 ----
    symptoms = [{"id": s.id, "name": s.name, "severity": s.props.get("severity")} for s in fp.symptoms]

    fault_cf: dict[str, float] = {}
    for atom, info in result.of("suspect_fault"):
        fault_cf[atom[2]] = max(fault_cf.get(atom[2], 0.0), info.cf)
    # 规则给出的额外权重（部件一致性、固件已知缺陷）；不与 suspect_fault 自引用，避免无限上调
    boost: dict[str, float] = {}
    for atom, _ in result.of("fault_boost"):
        try:
            boost[atom[2]] = boost.get(atom[2], 0.0) + float(atom[3])
        except ValueError:
            continue
    excluded = {a[2] for a, _ in result.of("excluded_fault")}
    # P2：用户自述已做过的动作/已得阳性的检查，可能排除某些根因。
    # 排除根因必须向下传导：故障的全部根因被排除时，整条故障也退出候选。
    excluded_causes = {a[1] for a, _ in result.of("excluded_cause")}
    fault_causes: dict[str, set[str]] = {}
    for atom, _ in result.of("fault_caused_by"):
        fault_causes.setdefault(atom[1], set()).add(atom[2])
    faults: list[dict[str, Any]] = []
    for fid, cf in fault_cf.items():
        if fid in excluded:
            continue
        known_causes = fault_causes.get(fid)
        if known_causes and known_causes <= excluded_causes:
            continue
        node = store.get(fid)
        if node is None:
            continue
        comps = [n for n, _ in _nbrs(store, fid, ["OCCURS_IN"], "out", 3)]
        cats = [n for n, _ in _nbrs(store, fid, ["IN_CATEGORY"], "out", 2)]
        # confidence 是推理得到的置信度（下游根因/方案都由它连乘），
        # rankScore 额外叠加规则权重，只用于候选排序，避免与证明树中的数值不一致。
        faults.append({
            "id": fid,
            "name": node.name,
            "code": node.props.get("code"),
            "severity": node.props.get("severity"),
            "confidence": round(cf, 3),
            "rankScore": round(min(0.99, cf + boost.get(fid, 0.0)), 3),
            "componentId": comps[0].id if comps else None,
            "component": comps[0].name if comps else None,
            "category": cats[0].props.get("code") if cats else None,
        })
    faults.sort(key=lambda f: (-f["rankScore"], -f["confidence"]))

    # ---- 根因 ----
    cause_cf: dict[tuple[str, str], float] = {}
    for atom, info in result.of("suspect_cause"):
        key = (atom[2], atom[3])
        cause_cf[key] = max(cause_cf.get(key, 0.0), info.cf)
    causes: list[dict[str, Any]] = []
    for (fid, cid), cf in sorted(cause_cf.items(), key=lambda kv: -kv[1])[:8]:
        node = store.get(cid)
        if node is None:
            continue
        if fid in excluded or cid in excluded_causes:
            continue
        causes.append({
            "id": cid,
            "name": node.name,
            "faultId": fid,
            "fault": store.get(fid).name if store.get(fid) else fid,
            "category": node.props.get("category"),
            "userFixable": node.props.get("userFixable"),
            "confidence": round(cf, 3),
        })

    # ---- 方案 ----
    rejected = {a[2]: a[3] for a, _ in result.of("reject_solution")}
    prefer_consumable = {a[2] for a, _ in result.of("prefer_consumable")}
    suppress = {a[2] for a, _ in result.of("suppress_parts")}
    base: dict[str, float] = {}
    score: dict[str, float] = {}
    reasons: dict[str, list[str]] = {}
    for atom, info in result.of("candidate_solution"):
        sid = atom[3]
        base[sid] = max(base.get(sid, 0.0), info.cf)
        score[sid] = max(score.get(sid, 0.0), info.cf)
    for atom, info in result.of("solution_score"):
        sid = atom[2]
        score[sid] = max(score.get(sid, 0.0), info.cf)
        reasons.setdefault(sid, []).append(atom[3])
    for sid in prefer_consumable:
        score[sid] = min(0.99, score.get(sid, 0.0) + 0.05)
        reasons.setdefault(sid, []).append("consumable_first")

    solutions: list[dict[str, Any]] = []
    support: dict[str, set[str]] = {}
    for atom, _ in result.of("candidate_solution"):
        support.setdefault(atom[3], set()).add(atom[2])
    for sid, s in score.items():
        node = store.get(sid)
        if node is None or sid in rejected:
            continue
        # 方案只服务于被排除的根因时，不再作为候选（其余根因仍支持则保留）
        if support.get(sid) and support[sid] <= excluded_causes:
            continue
        penalty = 0.0
        if "network" in suppress:
            if list(store.neighbors(sid, ["REQUIRES_PART"], "out", 5)):
                penalty = 0.3
                reasons.setdefault(sid, []).append("network_no_parts")
        items = [n for n, _ in _nbrs(store, sid, ["REQUIRES_CONSUMABLE", "REQUIRES_PART"], "out", 5)]
        docs = [n for n, _ in _nbrs(store, sid, ["DOCUMENTED_IN"], "out", 5)]
        solutions.append({
            "id": sid,
            "name": node.name,
            "type": node.props.get("type"),
            "riskLevel": node.props.get("riskLevel"),
            "selfServiceable": node.props.get("selfServiceable"),
            "estimatedMin": node.props.get("estimatedMin"),
            "score": round(max(0.0, min(0.99, s - penalty)), 3),
            "baseScore": round(base.get(sid, 0.0), 3),
            "reasons": sorted(set(reasons.get(sid, []))),
            "items": [{"name": n.name, "sku": n.props.get("sku"), "type": n.label} for n in items],
            "docs": [{"title": n.name, "docId": n.props.get("docId"), "url": n.props.get("url")} for n in docs],
        })
    solutions.sort(key=lambda r: -r["score"])
    solutions = solutions[:6]

    # ---- 检查项 ----
    next_checks = {a[2] for a, _ in result.of("next_check")}
    rank: dict[str, float] = {}
    for atom, _ in result.of("ordered_check"):
        rank[atom[2]] = float(atom[3])
    checks: list[dict[str, Any]] = []
    for cid, r in sorted(rank.items(), key=lambda kv: kv[1]):
        node = store.get(cid)
        if node is None:
            continue
        checks.append({
            "id": cid,
            "name": node.name,
            "method": node.props.get("method"),
            "riskLevel": node.props.get("riskLevel"),
            "durationMin": node.props.get("durationMin"),
            "discriminating": cid in next_checks,
            "rank": round(r, 1),
        })

    # ---- 策略与告警 ----
    safety = bool(result.query("safety_stop"))
    if safety:
        warnings.insert(0, SAFETY_WARNING)
        policies.append("safety_stop")
    if result.query("prefer_official"):
        policies.append("official_service_first")
        warnings.append("设备仍在保修期内，建议优先联系官方售后，不建议自行拆机或更换主板/电池。")
    for atom, _ in result.of("chronic_issue"):
        policies.append("chronic_issue")
        warnings.append(f"同一设备 90 天内同症状已报修 {atom[2]} 次，建议转官方售后评估是否需要换机。")
    for atom, _ in result.of("firmware_hint"):
        policies.append("firmware_hint")
        fw = store.get(atom[2])
        warnings.append(
            f"当前固件 {fw.props.get('version') if fw else ''} 存在已知缺陷，建议在稳定网络下评估升级。"
        )
        break
    if result.query("network_issue"):
        policies.append("network_first")
        warnings.append("该错误码属于网络/连接类，优先排查 2.4GHz 频段、密码、配网模式与固件版本，不需要更换主板等备件。")
    if result.query("conflicting_causes"):
        policies.append("conflict")
        warnings.append("存在多个高置信根因且缺少鉴别性检查，不建议直接换件；请先完成下方检查。")
    if result.query("insufficient_evidence") and not solutions:
        policies.append("insufficient_evidence")
        warnings.append("图谱中没有足够证据支撑具体方案，请补充型号/错误码/症状，或联系官方售后。")
    if result.query("duplicate_of"):
        policies.append("duplicate_ticket")
    if result.query("water_sequence"):
        policies.append("water_check_order")

    # ---- 证明子图与证据 ----
    graph = _proof_graph(store, fp, faults, causes, solutions, checks)
    for edge in graph["edges"]:
        edge["evidenceId"] = None
    evidence = _build_evidence(graph)
    for edge, ev in zip(graph["edges"], evidence):
        edge["evidenceId"] = ev["id"]

    # ---- 置信度 ----
    confidence = max([f["confidence"] for f in faults], default=0.0)
    if solutions:
        confidence = max(confidence, solutions[0]["score"])
    if "insufficient_evidence" in policies or "conflict" in policies:
        confidence = min(confidence, 0.6)
    confidence = min(confidence, 0.95)

    answer = _compose_answer(faults, causes, solutions, checks, warnings, safety)
    claims = _compose_claims(faults, causes, solutions, checks, graph, safety)

    return Diagnosis(
        case_id=fp.case_id,
        model=fp.model,
        symptoms=symptoms,
        faults=faults,
        causes=causes,
        solutions=solutions,
        checks=checks,
        warnings=warnings,
        policies=sorted(set(policies)),
        confidence=confidence,
        answer=answer,
        claims=claims,
        evidence=evidence,
        proof_tree=_proof_tree(result, solutions, causes, faults),
        graph=graph,
        trace={
            "iterations": result.iterations,
            "factCount": len(result.facts),
            "firedRules": result.fired_rules,
            "ruleCount": len(load_rules()),
            "backend": store.backend,
        },
        notes=fp.notes,
    )


def _proof_tree(result: InferenceResult, solutions, causes, faults) -> dict[str, Any] | None:
    engine = get_engine()
    for pred, arg_index, target in (
        ("candidate_solution", 3, solutions[0]["id"] if solutions else None),
        ("suspect_cause", 2, causes[0]["id"] if causes else None),
        ("suspect_fault", 2, faults[0]["id"] if faults else None),
    ):
        if not target:
            continue
        atom = next((a for a in result.query(pred) if a[arg_index] == target), None)
        if atom:
            return engine.proof_tree(result, atom)
    return None


def _proof_graph(store: GraphStore, fp: FactProjection, faults, causes, solutions, checks) -> dict[str, Any]:
    """最小证明子图：症状 → 故障 → 部件/根因 → 检查/方案 → 耗材/备件/文档。"""
    onto = get_ontology()
    keep: dict[str, int] = {}
    collected: list[tuple[Edge, int]] = []

    def touch(nid: str, step: int) -> None:
        if store.get(nid):
            keep[nid] = min(keep.get(nid, 99), step)

    def link(edge: Edge, step: int) -> None:
        touch(edge.head, step)
        touch(edge.tail, step)
        collected.append((edge, step))

    top_fault = faults[0]["id"] if faults else None
    top_cause = causes[0]["id"] if causes else None
    top_solution = solutions[0]["id"] if solutions else None

    for sym in fp.symptoms:
        touch(sym.id, 0)
        for fault, props in _nbrs(store, sym.id, ["INDICATES"], "out", 20):
            if top_fault and fault.id != top_fault:
                continue
            link(Edge("INDICATES", sym.id, fault.id, props), 1)

    for fault in faults[:2]:
        for comp, props in _nbrs(store, fault["id"], ["OCCURS_IN"], "out", 3):
            link(Edge("OCCURS_IN", fault["id"], comp.id, props), 2)
        for cause, props in _nbrs(store, fault["id"], ["CAUSED_BY"], "out", 20):
            if top_cause and cause.id != top_cause:
                continue
            link(Edge("CAUSED_BY", fault["id"], cause.id, props), 3)
            for chk, cprops in _nbrs(store, cause.id, ["CONFIRMED_BY"], "out", 3):
                link(Edge("CONFIRMED_BY", cause.id, chk.id,
                          {**cprops, "evidence": "检查项用于确认该根因"}), 4)
            for sol, sprops in _nbrs(store, cause.id, ["RESOLVED_BY"], "out", 10):
                if top_solution and sol.id != top_solution:
                    continue
                link(Edge("RESOLVED_BY", cause.id, sol.id, sprops), 5)
                for item, iprops in _nbrs(store, sol.id, ["REQUIRES_CONSUMABLE", "REQUIRES_PART"], "out", 5):
                    kind = "REQUIRES_PART" if item.label == "SparePart" else "REQUIRES_CONSUMABLE"
                    link(Edge(kind, sol.id, item.id, iprops), 6)
                for doc, dprops in _nbrs(store, sol.id, ["DOCUMENTED_IN"], "out", 3):
                    link(Edge("DOCUMENTED_IN", sol.id, doc.id, dprops), 6)

    nodes = []
    for nid, step in keep.items():
        node = store.get(nid)
        if node is None:
            continue
        cls = onto.classes.get(node.label)
        nodes.append({
            "id": node.id,
            "label": node.label,
            "name": node.name,
            "risk": node.props.get("riskLevel"),
            "step": step,
            "isProof": True,
            "color": cls.viz.get("color") if cls else "#64748b",
            "shape": cls.viz.get("shape") if cls else "round-rectangle",
        })
    nodes.sort(key=lambda n: (n["step"], n["id"]))

    edges = []
    for edge, step in collected:
        edges.append({
            "id": edge.id,
            "source": edge.head,
            "target": edge.tail,
            "type": edge.type,
            "confidence": edge.confidence,
            "derived": False,
            "step": step,
            "evidenceId": None,
            "props": edge.props,
        })
    return {
        "nodes": nodes,
        "edges": edges,
        "meta": {"layout": "dagre", "proofMode": "minimal", "truncated": False},
    }


def _build_evidence(graph: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    node_evidence: dict[str, list[str]] = {}
    for i, edge in enumerate(graph["edges"], start=1):
        props = edge.get("props") or {}
        item = {
            "id": f"E{i}",
            "triple": [edge["source"], edge["type"], edge["target"]],
            "text": str(props.get("evidence", "")),
            "source": str(props.get("sourceId", "")),
            "confidence": edge.get("confidence", 0.0),
            "derived": False,
        }
        out.append(item)
        node_evidence.setdefault(edge["source"], []).append(item["id"])
        node_evidence.setdefault(edge["target"], []).append(item["id"])
    for node in graph["nodes"]:
        node["evidenceIds"] = sorted(set(node_evidence.get(node["id"], [])))
    return out


def _compose_answer(faults, causes, solutions, checks, warnings, safety) -> str:
    if not faults:
        return "图谱中未找到与该症状匹配的故障模式，证据不足。" + DISCLAIMER
    top = faults[0]
    text = f"最可能的故障是「{top['name']}」（置信度 {top['confidence']:.2f}）"
    text += f"，涉及部件：{top['component']}。" if top.get("component") else "。"
    if len(faults) > 1:
        text += f"其他候选：{'、'.join(f['name'] for f in faults[1:4])}。"

    if causes:
        text += f" 首要根因推断为「{causes[0]['name']}」（{causes[0]['confidence']:.2f}）。"

    if safety:
        text += " ⚠️ 该症状属于安全风险，请先停止使用并断开充电，不要自行拆解。"
    elif checks:
        steps = "；".join(f"{c['name']}（{c['durationMin']} 分钟，风险 {c['riskLevel']}）" for c in checks[:3])
        prefix = "建议先做鉴别性检查：" if any(c["discriminating"] for c in checks[:3]) else "建议按顺序检查："
        text += f" {prefix}{steps}。"

    if solutions:
        s = solutions[0]
        part = f"若检查确认，优先执行「{s['name']}」"
        if s.get("estimatedMin"):
            part += f"（约 {s['estimatedMin']} 分钟，风险 {s.get('riskLevel')}）"
        if s.get("items"):
            part += "，需要：" + "、".join(i["name"] for i in s["items"])
        text += " " + part + "。"
        if len(solutions) > 1:
            text += f" 备选方案：{'、'.join(x['name'] for x in solutions[1:3])}。"

    for w in warnings:
        if not w.startswith("设备仍在保修期内") and w not in text:
            text += " " + w
    return text + " " + DISCLAIMER


def _compose_claims(faults, causes, solutions, checks, graph, safety) -> list[dict[str, Any]]:
    def ids_of(*types: str) -> list[str]:
        return [e["evidenceId"] for e in graph["edges"] if e["type"] in types and e.get("evidenceId")]

    claims: list[dict[str, Any]] = []
    if faults:
        claims.append({
            "text": f"最可能故障：{faults[0]['name']}（{faults[0]['confidence']:.2f}）",
            "ruleIds": ["R03", "R04"],
            "evidenceIds": ids_of("INDICATES", "OCCURS_IN"),
            "confidence": faults[0]["confidence"],
        })
    if causes:
        claims.append({
            "text": f"首要根因：{causes[0]['name']}（{causes[0]['confidence']:.2f}）",
            "ruleIds": ["R05"],
            "evidenceIds": ids_of("CAUSED_BY", "CONFIRMED_BY"),
            "confidence": causes[0]["confidence"],
        })
    if solutions:
        claims.append({
            "text": f"推荐方案：{solutions[0]['name']}（{solutions[0]['score']:.2f}）",
            "ruleIds": ["R06", "R07"] + (["R09"] if "consumable_first" in solutions[0]["reasons"] else []),
            "evidenceIds": ids_of("RESOLVED_BY", "DOCUMENTED_IN", "REQUIRES_CONSUMABLE", "REQUIRES_PART"),
            "confidence": solutions[0]["score"],
        })
    if checks:
        claims.append({
            "text": "下一步检查：" + "、".join(c["name"] for c in checks[:3]),
            "ruleIds": ["R17"] + (["R16"] if any(c["discriminating"] for c in checks) else []),
            "evidenceIds": ids_of("CONFIRMED_BY"),
            "confidence": 0.8,
        })
    if safety:
        claims.append({
            "text": "安全拦截：高风险症状，禁止自行处置",
            "ruleIds": ["R14"],
            "evidenceIds": [],
            "confidence": 1.0,
        })
    return claims
