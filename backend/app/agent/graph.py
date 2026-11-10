"""RefKG-lite 工作流编排。

对应论文四段式：Query Decoupling → LLM-Driven KG Exploration →
Inference with Knowledge Reconstruction（+ 反思重试）。

工程实现取舍
------------
* 不依赖 LangGraph 也能跑（内联状态机），节点划分与文档 8.4 一致，便于后续替换；
* 无 LLM 时用确定性槽位抽取与工具计划，因此 Agent 的「行为」可复现、可评测；
* 工具是窄接口（见 tools.py），LLM 只能选择工具与参数，不能生成 Cypher。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from ..config import get_settings
from ..kg import queries as Q
from ..kg.store import GraphStore
from ..reasoning.graph_only import diagnose_graph_only
from ..reasoning.service import LOOKUP_POLICIES, Diagnosis, diagnose
from .slots import Slots, extract_slots, sub_questions
from .tools import ToolResult, Tools


@dataclass
class AgentRun:
    diagnosis: Diagnosis
    slots: dict[str, Any]
    sub_questions: list[dict[str, Any]]
    coverage: dict[str, str]
    reflections: list[dict[str, Any]]
    tool_results: list[dict[str, Any]]
    trace: list[dict[str, Any]]
    answer: str
    status: str = "ok"
    extra: dict[str, Any] = field(default_factory=dict)


def run(
    store: GraphStore,
    question: str | None = None,
    case_no: str | None = None,
    model: str | None = None,
    code: str | None = None,
    symptom: str | None = None,
    use_reflection: bool = True,
    use_rules: bool = True,
) -> AgentRun:
    """use_reflection / use_rules 是消融开关（文档 9.3 的 D / E 组），默认全开。

    * use_reflection=False：跳过 reflect 节点，即不再按子问题覆盖度补检索；
    * use_rules=False：把 `run_rules` 换成 `retrieve_chain`，只走图检索链，
      不带规则推理与规则派生的策略（网络优先、安全拦截、检查排序）。
    """
    settings = get_settings()
    started = time.perf_counter()
    trace: list[dict[str, Any]] = []
    tools = Tools(store, use_rules=use_rules)
    diag_fn = diagnose if use_rules else diagnose_graph_only
    diag_tool = "run_rules" if use_rules else "retrieve_chain"

    def step(node: str, **payload: Any) -> None:
        trace.append({"node": node, "latencyMs": int((time.perf_counter() - started) * 1000), **payload})

    # ---------- intake ----------
    slots = extract_slots(store, question or "")
    slots.case_no = case_no or slots.case_no
    if model:  # 结构化参数优先，覆盖问题里抽出的型号，同时清掉「未收录」标记
        slots.model = model
        slots.model_unknown = None
    slots.code = code or slots.code
    slots.symptom = symptom or slots.symptom
    step("intake", slots=slots.to_dict(), safetyHint=slots.safety_hint)

    if not any([slots.model, slots.code, slots.symptom, slots.case_no]):
        return _out_of_scope(slots, trace, started)

    # ---------- decompose ----------
    subs = sub_questions(slots)
    step("decompose", subQuestions=[s["id"] for s in subs])

    # ---------- plan ----------
    plan: list[dict[str, Any]] = [{"tool": diag_tool, "args": {
        "model": slots.model, "code": slots.code, "symptom": slots.symptom, "case_no": slots.case_no,
    }}]
    if slots.model and not slots.model_unknown:
        plan.append({"tool": "search_similar_cases", "args": {
            # 首次检索直接覆盖一年：售后案例密度低，30/90 天窗口经常为空，
            # 空结果再触发一轮反思纯属浪费一次工具调用。
            "model": slots.model, "symptom": slots.symptom or "", "days": 365,
        }})
    step("plan", actions=[p["tool"] for p in plan])

    # ---------- act ----------
    results: list[ToolResult] = []
    diagnosis: Diagnosis | None = None
    similar: list[dict[str, Any]] = []
    steps_used = 0

    def execute(action: dict[str, Any]) -> ToolResult:
        nonlocal steps_used
        steps_used += 1
        result = tools.call(action["tool"], **action["args"])
        results.append(result)
        step("act", tool=result.tool, args=action["args"], ok=result.ok,
             latencyMs=result.latencyMs, summary=result.summary, error=result.error)
        return result

    for action in plan:
        if steps_used >= settings.agent_max_steps:
            break
        result = execute(action)
        if result.tool == diag_tool and result.ok:
            diagnosis = diag_fn(store, case_no=slots.case_no, model=slots.model,
                                code=slots.code, symptom=slots.symptom, question=question)
            # 规则层已判定查询落空，后续工具调用没有意义，提前结束规划
            if set(diagnosis.policies) & LOOKUP_POLICIES:
                break
        elif result.tool == "search_similar_cases" and result.ok:
            similar = result.data or []

    if diagnosis is None:
        diagnosis = diag_fn(store, case_no=slots.case_no, model=slots.model,
                            code=slots.code, symptom=slots.symptom, question=question)

    # 查询落空（跨型号串码 / 未知错误码 / 未知型号）：直接给出可解释结论，
    # 不做兼容性核查与反思重试，否则会为「查不到」再花两三次工具调用。
    if set(diagnosis.policies) & LOOKUP_POLICIES:
        coverage = {qid: "missing" for qid in ("q1", "q2", "q3", "q4", "q5")}
        step("verify", issues=[], policies=diagnosis.policies)
        step("finalize", coverage=coverage, status="lookup_miss",
             latencyMs=int((time.perf_counter() - started) * 1000))
        return AgentRun(
            diagnosis=diagnosis,
            slots=slots.to_dict(),
            sub_questions=subs,
            coverage=coverage,
            reflections=[],
            tool_results=[r.to_dict() for r in results],
            trace=trace,
            answer=diagnosis.answer,
            status="degraded",
            extra={"compatibility": [], "similarCases": [], "issues": [],
                   "unknownModel": slots.model_unknown,
                   "reason": sorted(set(diagnosis.policies) & LOOKUP_POLICIES)[0]},
        )

    # 兼容性核查：只在确有备件/耗材建议时调用，避免无意义工具调用
    compat: list[dict[str, Any]] = []
    if diagnosis.solutions and steps_used < settings.agent_max_steps:
        for item in diagnosis.solutions[0]["items"][:2]:
            if slots.model and steps_used < settings.agent_max_steps:
                r = execute({"tool": "check_compatibility",
                             "args": {"item": item["name"], "model": slots.model}})
                if r.ok and r.data:
                    compat.append(r.data)

    # ---------- reflect ----------
    coverage = _coverage(diagnosis, similar)
    reflections: list[dict[str, Any]] = []
    attempted: set[tuple[str, str]] = {(r.tool, str(r.args)) for r in results}
    rounds = 0
    while (use_reflection
           and rounds < settings.agent_max_reflections
           and steps_used < settings.agent_max_steps):
        missing = [qid for qid, state in coverage.items() if state == "missing"]
        partial = [qid for qid, state in coverage.items() if state == "partial"]
        if not missing and not partial:
            break
        rounds += 1
        actions: list[dict[str, Any]] = []
        if "q3" in missing and slots.model:
            # 逐级放宽：先按「同型号 + 同症状」扩大时间窗，仍无结果再退化为
            # 「同型号历史案例」（去掉症状条件）——比反复扩大时间窗更有信息增益。
            # 方案里的首次检索已覆盖「同症状 + 365 天」，这里直接退化为同型号全症状
            candidates: list[dict[str, Any]] = [
                {"model": slots.model, "symptom": "", "days": 180},
            ]
            for args in candidates:
                if ("search_similar_cases", str(args)) not in attempted:
                    actions.append({"tool": "search_similar_cases", "args": args})
                    break
        if "q2" in (missing + partial) and diagnosis.faults:
            actions.append({"tool": "expand_subgraph",
                            "args": {"node_ids": [diagnosis.faults[0]["id"]], "depth": 2}})
        actions = [a for a in actions if (a["tool"], str(a["args"])) not in attempted]
        reflections.append({
            "round": rounds,
            "coverage": dict(coverage),
            "missing": missing + partial,
            "decision": "retry" if actions else "stop",
            "nextActions": [a["tool"] for a in actions],
        })
        step("reflect", round=rounds, coverage=dict(coverage),
             decision=reflections[-1]["decision"], missing=missing + partial)
        if not actions:
            break
        for action in actions:
            if steps_used >= settings.agent_max_steps:
                break
            attempted.add((action["tool"], str(action["args"])))
            result = execute(action)
            if result.tool == "search_similar_cases" and result.ok and result.data:
                similar = result.data
        coverage = _coverage(diagnosis, similar)

    # ---------- verify ----------
    issues = _verify(diagnosis, similar, compat)
    step("verify", issues=issues, policies=diagnosis.policies)

    # ---------- finalize ----------
    answer = _compose(diagnosis, similar, compat, slots, coverage)
    step("finalize", coverage=coverage, latencyMs=int((time.perf_counter() - started) * 1000))

    return AgentRun(
        diagnosis=diagnosis,
        slots=slots.to_dict(),
        sub_questions=subs,
        coverage=coverage,
        reflections=reflections,
        tool_results=[r.to_dict() for r in results],
        trace=trace,
        answer=answer,
        status="ok",
        extra={"compatibility": compat, "similarCases": similar[:5], "issues": issues,
               "unknownModel": slots.model_unknown},
    )


def _coverage(d: Diagnosis, similar: list[dict[str, Any]]) -> dict[str, str]:
    return {
        "q1": "covered" if d.faults else "missing",
        "q2": "covered" if d.causes and d.checks else ("partial" if d.causes else "missing"),
        "q3": "covered" if similar else "missing",
        "q4": "covered" if d.solutions else "partial",
        "q5": "covered" if (d.policies or d.warnings or d.solutions) else "missing",
    }


def _verify(d: Diagnosis, similar: list[dict[str, Any]], compat: list[dict[str, Any]]) -> list[str]:
    issues: list[str] = []
    if not d.solutions and not d.warnings:
        issues.append("no_solution_without_warning")
    if "network_first" in d.policies:
        for sol in d.solutions[:3]:
            if any(i["type"] == "SparePart" for i in sol["items"]):
                issues.append(f"network_case_suggests_part:{sol['name']}")
    if d.solutions and not d.evidence:
        issues.append("missing_evidence")
    if any(c.get("compatible") is False for c in compat):
        issues.append("incompatible_recommendation")
    if "safety_stop" in d.policies and any(
        s["riskLevel"] == "high" and s["selfServiceable"] for s in d.solutions
    ):
        issues.append("safety_case_suggests_risky_self_service")
    return issues


def _compose(d: Diagnosis, similar, compat, slots: Slots, coverage) -> str:
    """在推理答案后追加「案例/兼容性/换件」补充说明。

    补充说明必须插在免责声明之前，且只在证据确实相关时才提处置方案——
    把「同型号其它症状」的处置方案说成相似案例的方案，是典型的误导。
    """
    from ..reasoning.service import DISCLAIMER

    extra: list[str] = []
    if similar:
        tickets = [s for s in similar if s.get("caseType") == "Ticket"]
        same = [
            s for s in similar
            if slots.symptom and any(slots.symptom in x or x in slots.symptom
                                     for x in (s.get("symptoms") or []))
        ]
        if same:
            wins = sorted({s["solutions"][0] for s in same if s.get("solutions")})
            note = f"同型号同症状历史工单 {len(same)} 条"
            note += f"，处置方案包括：{'、'.join(wins[:3])}。" if wins else "。"
            extra.append(note)
        elif tickets:
            extra.append(
                f"该症状暂无历史工单；同型号其它症状工单 {len(tickets)} 条仅供参考，处置方案不可直接套用。"
            )
    if compat:
        ok = [c for c in compat if c.get("compatible")]
        if ok:
            extra.append("所需耗材/备件与型号兼容：" + "、".join(c["item"] for c in ok) + "。")
    if coverage.get("q3") == "missing" and slots.model:
        extra.append("图谱中未检索到同型号的相似案例，结论仅基于知识链与规则。")
    if slots.replace_intent and d.solutions:
        extra.append("是否更换备件取决于检查结果：先完成上述检查，确认部件损坏后再换件。")

    text = d.answer
    if not extra:
        return text
    tail = " " + " ".join(extra)
    if text.endswith(DISCLAIMER):
        return text[: -len(DISCLAIMER)] + tail + " " + DISCLAIMER
    return text + tail


def _out_of_scope(slots: Slots, trace: list[dict[str, Any]], started: float) -> AgentRun:
    diagnosis = Diagnosis(
        case_id="CASE:QUERY", model=None, symptoms=[], faults=[], causes=[], solutions=[],
        checks=[], warnings=["未识别到 DEEBOT T 系列的型号、错误码或症状，暂无法给出排查建议。"],
        policies=["out_of_scope"], confidence=0.0,
        answer="请补充型号（如 T20 PRO）、错误码（如 E11）或具体症状（如「左轮不转」）。"
               "本助手目前覆盖科沃斯 DEEBOT T 系列中的 T20 PRO。",
        claims=[], evidence=[], proof_tree=None,
        graph={"nodes": [], "edges": [], "meta": {}},
        trace={"note": "out_of_scope"},
    )
    trace.append({"node": "finalize", "latencyMs": int((time.perf_counter() - started) * 1000),
                  "status": "out_of_scope"})
    return AgentRun(diagnosis=diagnosis, slots=slots.to_dict(), sub_questions=[], coverage={},
                    reflections=[], tool_results=[], trace=trace, answer=diagnosis.answer,
                    status="out_of_scope")
