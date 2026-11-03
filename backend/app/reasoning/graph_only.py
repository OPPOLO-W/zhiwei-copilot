"""消融 E 用的「无规则」诊断：纯图检索链，不经规则引擎。

对应开发文档 9.3 的 E 组（RefKG-lite w/o rules）。与 `service.diagnose` 的差别是
**只删规则推理，不删实体链接**：

* 仍做实体链接（型号 / 错误码 / 症状 → 图节点）与查询落空降级——这是检索，不是推理；
* 不做正向链式推理，候选故障/根因/方案直接按图的边与边置信度排序取前若干；
* 不注入任何策略与安全告警（`network_first` / `safety_stop` / `precedence` 都来自规则层），
  因此它在网络歧义题与安全题上的退化是可观测的——这正是消融要证明的东西。

为了让 A/B/C/E 的路径指标可比，输出仍是同一个 `Diagnosis` 结构，
证明子图与证据复用 `service` 的 `_proof_graph` / `_build_evidence`。
"""

from __future__ import annotations

from typing import Any, Iterable

from ..kg.store import GraphStore
from ..rules.facts import FactProjection, build_facts
from . import service
from .service import DISCLAIMER, Diagnosis

MAX_FAULTS = 5
MAX_CAUSES = 8
MAX_SOLUTIONS = 6


def _nbrs(store: GraphStore, node_id: str, rels: Iterable[str], direction: str = "out",
          limit: int = 100) -> list[tuple[Any, dict[str, Any]]]:
    return [(node, edge.props) for edge, node in store.neighbors(node_id, list(rels), direction, limit)]


def _cf(props: dict[str, Any]) -> float:
    try:
        return round(float(props.get("confidence", 0.0)), 4)
    except (TypeError, ValueError):
        return 0.0


def diagnose_graph_only(
    store: GraphStore,
    case_no: str | None = None,
    model: str | None = None,
    code: str | None = None,
    symptom: str | None = None,
    question: str | None = None,
) -> Diagnosis:
    """图形检索诊断。签名与 `service.diagnose` 一致，便于评测里平替。"""
    try:
        fp = build_facts(store, case_no=case_no, model=model, code=code, symptom=symptom)
    except ValueError as exc:
        # 落空降级属于实体链接，不属于规则推理，两组系统保持同一套降级口径
        fallback = service._lookup_fallback(store, str(exc), model=model, code=code)
        if fallback is None:
            raise
        return fallback

    # ---- 候选故障：症状 INDICATES 的边，按边置信度排序 ----
    fault_cf: dict[str, float] = {}
    for sym in fp.symptoms:
        for fault, props in _nbrs(store, sym.id, ["INDICATES"], limit=20):
            fault_cf[fault.id] = max(fault_cf.get(fault.id, 0.0), _cf(props))

    faults: list[dict[str, Any]] = []
    for fid, cf in sorted(fault_cf.items(), key=lambda kv: -kv[1])[:MAX_FAULTS]:
        node = store.get(fid)
        if node is None:
            continue
        comps = [n for n, _ in _nbrs(store, fid, ["OCCURS_IN"], limit=3)]
        cats = [n for n, _ in _nbrs(store, fid, ["IN_CATEGORY"], limit=2)]
        faults.append({
            "id": fid,
            "name": node.name,
            "code": node.props.get("code"),
            "severity": node.props.get("severity"),
            "confidence": round(cf, 3),
            # 没有规则重排，rankScore 与 confidence 同值；前端字段保持一致
            "rankScore": round(cf, 3),
            "componentId": comps[0].id if comps else None,
            "component": comps[0].name if comps else None,
            "category": cats[0].props.get("code") if cats else None,
        })

    # ---- 根因：故障 CAUSED_BY ----
    causes: list[dict[str, Any]] = []
    for fault in faults:
        for cause, props in _nbrs(store, fault["id"], ["CAUSED_BY"], limit=20):
            causes.append({
                "id": cause.id,
                "name": cause.name,
                "faultId": fault["id"],
                "fault": fault["name"],
                "category": cause.props.get("category"),
                "userFixable": cause.props.get("userFixable"),
                "confidence": round(_cf(props), 3),
            })
    causes.sort(key=lambda c: -c["confidence"])
    causes = causes[:MAX_CAUSES]

    # ---- 检查项：根因 CONFIRMED_BY（无规则排序，按边置信度） ----
    checks: list[dict[str, Any]] = []
    seen_checks: set[str] = set()
    for cause in causes:
        for chk, props in _nbrs(store, cause["id"], ["CONFIRMED_BY"], limit=5):
            if chk.id in seen_checks:
                continue
            seen_checks.add(chk.id)
            checks.append({
                "id": chk.id,
                "name": chk.name,
                "method": chk.props.get("method"),
                "riskLevel": chk.props.get("riskLevel"),
                "durationMin": chk.props.get("durationMin"),
                # 鉴别性检查是规则 R17 的结论，无规则版本一律标 False
                "discriminating": False,
                "rank": float(len(checks) + 1),
                "confidence": round(_cf(props), 3),
            })
    checks.sort(key=lambda c: -c["confidence"])

    # ---- 方案：根因 RESOLVED_BY ----
    solutions: list[dict[str, Any]] = []
    seen_solutions: set[str] = set()
    for cause in causes:
        for sol, props in _nbrs(store, cause["id"], ["RESOLVED_BY"], limit=10):
            if sol.id in seen_solutions:
                continue
            seen_solutions.add(sol.id)
            items = [n for n, _ in _nbrs(store, sol.id, ["REQUIRES_CONSUMABLE", "REQUIRES_PART"], limit=5)]
            docs = [n for n, _ in _nbrs(store, sol.id, ["DOCUMENTED_IN"], limit=5)]
            solutions.append({
                "id": sol.id,
                "name": sol.name,
                "type": sol.props.get("type"),
                "riskLevel": sol.props.get("riskLevel"),
                "selfServiceable": sol.props.get("selfServiceable"),
                "estimatedMin": sol.props.get("estimatedMin"),
                "score": round(_cf(props), 3),
                "baseScore": round(_cf(props), 3),
                "reasons": [],
                "items": [{"name": n.name, "sku": n.props.get("sku"), "type": n.label} for n in items],
                "docs": [{"title": n.name, "docId": n.props.get("docId"), "url": n.props.get("url")}
                         for n in docs],
            })
    solutions.sort(key=lambda s: -s["score"])
    solutions = solutions[:MAX_SOLUTIONS]

    # ---- 证明子图 / 证据 ----
    graph = service._proof_graph(store, fp, faults, causes, solutions, checks)
    for edge in graph["edges"]:
        edge["evidenceId"] = None
    evidence = service._build_evidence(graph)
    for edge, ev in zip(graph["edges"], evidence):
        edge["evidenceId"] = ev["id"]

    answer = _compose(faults, causes, solutions, checks)
    claims = _claims(faults, causes, solutions, checks, graph)

    confidence = max([f["confidence"] for f in faults], default=0.0)
    if solutions:
        confidence = max(confidence, solutions[0]["score"])

    return Diagnosis(
        case_id=fp.case_id,
        model=fp.model,
        symptoms=[{"id": s.id, "name": s.name, "severity": s.props.get("severity")} for s in fp.symptoms],
        faults=faults,
        causes=causes,
        solutions=solutions,
        checks=checks,
        warnings=[],
        policies=[],
        confidence=min(confidence, 0.95),
        answer=answer,
        claims=claims,
        evidence=evidence,
        proof_tree=None,
        graph=graph,
        trace={"mode": "graph_only", "factCount": len(fp.atoms), "backend": store.backend},
        notes=fp.notes,
    )


def _compose(faults, causes, solutions, checks) -> str:
    if not faults:
        return "图谱中未找到与该症状匹配的故障模式，证据不足。" + DISCLAIMER
    top = faults[0]
    text = f"最可能的故障是「{top['name']}」（置信度 {top['confidence']:.2f}）"
    text += f"，涉及部件：{top['component']}。" if top.get("component") else "。"
    if len(faults) > 1:
        text += f"其他候选：{'、'.join(f['name'] for f in faults[1:4])}。"
    if causes:
        text += f" 首要根因推断为「{causes[0]['name']}」（{causes[0]['confidence']:.2f}）。"
    if checks:
        steps = "；".join(f"{c['name']}（{c['durationMin']} 分钟，风险 {c['riskLevel']}）"
                         for c in checks[:3])
        text += f" 建议按顺序检查：{steps}。"
    if solutions:
        s = solutions[0]
        part = f"若检查确认，可以尝试「{s['name']}」"
        if s.get("items"):
            part += "，需要：" + "、".join(i["name"] for i in s["items"])
        text += " " + part + "。"
        if len(solutions) > 1:
            text += f" 备选方案：{'、'.join(x['name'] for x in solutions[1:3])}。"
    return text + " " + DISCLAIMER


def _claims(faults, causes, solutions, checks, graph) -> list[dict[str, Any]]:
    """无规则版本没有规则可引用，ruleIds 留空——避免把规则编号当成装饰。"""
    def ids_of(*types: str) -> list[str]:
        return [e["evidenceId"] for e in graph["edges"] if e["type"] in types and e.get("evidenceId")]

    claims: list[dict[str, Any]] = []
    if faults:
        claims.append({"text": f"最可能故障：{faults[0]['name']}（{faults[0]['confidence']:.2f}）",
                       "ruleIds": [], "evidenceIds": ids_of("INDICATES", "OCCURS_IN"),
                       "confidence": faults[0]["confidence"]})
    if causes:
        claims.append({"text": f"首要根因：{causes[0]['name']}（{causes[0]['confidence']:.2f}）",
                       "ruleIds": [], "evidenceIds": ids_of("CAUSED_BY", "CONFIRMED_BY"),
                       "confidence": causes[0]["confidence"]})
    if solutions:
        claims.append({"text": f"推荐方案：{solutions[0]['name']}（{solutions[0]['score']:.2f}）",
                       "ruleIds": [],
                       "evidenceIds": ids_of("RESOLVED_BY", "DOCUMENTED_IN",
                                             "REQUIRES_CONSUMABLE", "REQUIRES_PART"),
                       "confidence": solutions[0]["score"]})
    if checks:
        claims.append({"text": "下一步检查：" + "、".join(c["name"] for c in checks[:3]),
                       "ruleIds": [], "evidenceIds": ids_of("CONFIRMED_BY"), "confidence": 0.8})
    return claims
