"""LLM 驱动的 RefKG-lite 工作流（Week 3 的 LLM 接入）。

与 `graph.run` 的确定性实现同构、同一 `AgentRun` 输出，区别在于四个节点由 LLM 驱动：

  * decompose —— LLM 做查询解耦（槽位抽取），再与确定性抽取「取并」，
    模型/错误码仍以正则+图解析为准（防幻觉），症状/环境/意图可用 LLM 补召回；
  * plan/act —— 规则骨干 `run_rules` 固定先跑（安全/网络规则兜底），
    之后 LLM 从白名单里选补充工具，参数只能取自已识别槽位与已收集证据；
  * reflect —— LLM 按 q1~q5 给覆盖度与缺口，决定 continue/stop；
  * finalize —— LLM 生成带 evidenceId 引用的答复，规则告警与免责声明强制保留。

健壮性（文档 8.10）：
  * 每个节点 LLM 失败都降级到确定性等价实现，整条链路绝不因 LLM 崩而崩；
  * 槽位经图校验（型号/症状必须命中实体，否则标 unknown），模型不编造实体；
  * 工具调用白名单 + 预算（agent_max_steps / agent_max_reflections）防死循环。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from ..config import get_settings
from ..kg.store import GraphStore
from ..llm.client import LLMClient, LLMError, get_client
from ..llm.prompts import (
    FinalAnswerOutput,
    PlanOutput,
    ReflectionOutput,
    SlotExtraction,
    decompose_prompt,
    finalize_prompt,
    plan_prompt,
    reflect_prompt,
)
from ..reasoning.graph_only import diagnose_graph_only
from ..reasoning.service import DISCLAIMER, LOOKUP_POLICIES, Diagnosis, diagnose
from . import graph as _deterministic
from .slots import Slots, extract_slots, sub_questions
from .tools import ToolResult, Tools

log = logging.getLogger("zhiwei.agent.llm")

# 补充探索工具：规则骨干已跑过 run_rules/retrieve_chain，LLM 只能选这些
_EXPLORE_TOOLS = {
    "search_similar_cases", "expand_subgraph", "check_compatibility",
    "resolve_entity", "get_document_span", "find_paths",
}


def run_llm(
    store: GraphStore,
    question: str | None = None,
    case_no: str | None = None,
    model: str | None = None,
    code: str | None = None,
    symptom: str | None = None,
    use_reflection: bool = True,
    use_rules: bool = True,
) -> "_deterministic.AgentRun":
    """LLM 驱动版本；`use_reflection` / `use_rules` 语义与 graph.run 一致（消融开关）。"""
    client = get_client()
    if not client.enabled:
        # 没配 key：整体退回确定性实现（与 .env.example 的「Agent 走规则兜底」一致）
        return _deterministic.run(store, question=question, case_no=case_no, model=model,
                                  code=code, symptom=symptom,
                                  use_reflection=use_reflection, use_rules=use_rules)

    settings = get_settings()
    started = time.perf_counter()
    trace: list[dict[str, Any]] = []
    tools = Tools(store, use_rules=use_rules)
    diag_fn = diagnose if use_rules else diagnose_graph_only
    diag_tool = "run_rules" if use_rules else "retrieve_chain"
    llm_usage = {"model": client.model, "calls": 0, "tokens": 0}

    def step(node: str, **payload: Any) -> None:
        trace.append({"node": node,
                      "latencyMs": int((time.perf_counter() - started) * 1000), **payload})

    # ---------- intake / decompose：确定性抽取 + LLM 解耦取并 ----------
    base = extract_slots(store, question or "")
    llm_slots = _decompose(client, store, question or "")
    slots = _merge_slots(store, base, llm_slots)
    slots.case_no = case_no or slots.case_no
    if model:
        slots.model = model
        slots.model_unknown = None
    slots.code = code or slots.code
    slots.symptom = symptom or slots.symptom
    step("intake", slots=slots.to_dict(), safetyHint=slots.safety_hint,
         llmSlots=bool(llm_slots))

    if not any([slots.model, slots.code, slots.symptom, slots.case_no]):
        return _deterministic._out_of_scope(slots, trace, started)

    subs = sub_questions(slots)
    step("decompose", subQuestions=[s["id"] for s in subs])

    # ---------- 规则骨干：永远先跑，安全/网络规则不可被 LLM 跳过 ----------
    results: list[ToolResult] = []
    steps_used = 0

    def execute(action: dict[str, Any]) -> ToolResult:
        nonlocal steps_used
        steps_used += 1
        result = tools.call(action["tool"], **action["args"])
        results.append(result)
        step("act", tool=result.tool, args=action["args"], ok=result.ok,
             latencyMs=result.latencyMs, summary=result.summary, error=result.error)
        return result

    backbone = execute({"tool": diag_tool, "args": {
        "model": slots.model, "code": slots.code, "symptom": slots.symptom,
        "case_no": slots.case_no,
    }})
    diagnosis = diag_fn(store, case_no=slots.case_no, model=slots.model,
                        code=slots.code, symptom=slots.symptom, question=question)

    # 查询落空：与 graph.run 同口径，直接给可解释结论，不做无谓探索
    if set(diagnosis.policies) & LOOKUP_POLICIES:
        coverage = {qid: "missing" for qid in ("q1", "q2", "q3", "q4", "q5")}
        step("verify", issues=[], policies=diagnosis.policies)
        step("finalize", coverage=coverage, status="lookup_miss",
             latencyMs=int((time.perf_counter() - started) * 1000))
        return _deterministic.AgentRun(
            diagnosis=diagnosis, slots=slots.to_dict(), sub_questions=subs,
            coverage=coverage, reflections=[], tool_results=[r.to_dict() for r in results],
            trace=trace, answer=diagnosis.answer, status="degraded",
            extra={"compatibility": [], "similarCases": [], "issues": [],
                   "unknownModel": slots.model_unknown,
                   "reason": sorted(set(diagnosis.policies) & LOOKUP_POLICIES)[0],
                   "llm": llm_usage},
        )

    similar: list[dict[str, Any]] = []
    compat: list[dict[str, Any]] = []

    # ---------- plan/act：LLM 选补充工具（失败则退回确定性计划）----------
    plan = _plan(client, store, question or "", slots, diagnosis, similar, compat,
                 settings.agent_max_steps, llm_usage)
    # 先记下「LLM 是否真的给了计划」再兜底：否则兜底后 plan 必非 None，标记恒为 True
    llm_plan = plan is not None
    if plan is None:
        plan = _fallback_plan(slots)
    step("plan", actions=[p["tool"] for p in plan], llmDriven=llm_plan)

    attempted: set[tuple[str, str]] = {(backbone.tool, str(backbone.args))}
    for action in plan:
        if steps_used >= settings.agent_max_steps:
            break
        key = (action["tool"], str(action["args"]))
        if key in attempted:
            continue
        attempted.add(key)
        result = execute(action)
        if result.tool == "search_similar_cases" and result.ok:
            similar = result.data or []
        elif result.tool == "check_compatibility" and result.ok and result.data:
            compat.append(result.data)

    # ---------- reflect：LLM 判定覆盖度/缺口，可再补一轮 ----------
    coverage = _deterministic._coverage(diagnosis, similar)
    reflections: list[dict[str, Any]] = []
    rounds = 0
    while (use_reflection and rounds < settings.agent_max_reflections
           and steps_used < settings.agent_max_steps):
        summary = _evidence_summary(diagnosis, similar, compat)
        refl = _reflect(client, store, question or "", slots, summary, llm_usage)
        driver = "llm"
        if refl is None:
            # LLM 反思失败 → 用确定性覆盖度做停止判据（此时已不是语义反思）
            driver = "heuristic"
            refl = ReflectionOutput(
                coverage=coverage,
                missing=[q for q, s in coverage.items() if s != "covered"],
                decision="continue" if coverage["q3"] == "missing" and slots.model else "stop",
            )
        coverage = {k: v for k, v in (refl.coverage or {}).items()} or coverage
        reflections.append({
            "round": rounds + 1, "coverage": dict(coverage),
            "conflicts": list(refl.conflicts),
            "missing": list(refl.missing),
            "decision": refl.decision,
            "nextCalls": [c.tool for c in refl.next_calls],
        })
        step("reflect", round=rounds + 1, coverage=dict(coverage),
             decision=refl.decision, missing=list(refl.missing), driver=driver)
        rounds += 1
        if refl.decision != "continue" or not refl.next_calls:
            break
        for call in refl.next_calls:
            if steps_used >= settings.agent_max_steps:
                break
            if call.tool not in _EXPLORE_TOOLS:
                continue
            key = (call.tool, str(call.args))
            if key in attempted:
                continue
            attempted.add(key)
            result = execute({"tool": call.tool, "args": call.args})
            if result.tool == "search_similar_cases" and result.ok:
                similar = result.data or []
            elif result.tool == "check_compatibility" and result.ok and result.data:
                compat.append(result.data)
        coverage = _deterministic._coverage(diagnosis, similar)

    # ---------- verify：确定性规则校验（安全/网络/不兼容），不交给 LLM ----------
    issues = _deterministic._verify(diagnosis, similar, compat)
    step("verify", issues=issues, policies=diagnosis.policies)

    # ---------- finalize：LLM 生成带引用答复（失败回退模板）----------
    # _finalize 永远返回字符串，不能用「is not None」判断；改看本轮是否真的成功调用了 LLM
    calls_before = llm_usage["calls"]
    answer = _finalize(client, store, question or "", slots, diagnosis,
                       similar, compat, coverage, llm_usage)
    step("finalize", coverage=coverage, llmAnswer=llm_usage["calls"] > calls_before,
         latencyMs=int((time.perf_counter() - started) * 1000))

    return _deterministic.AgentRun(
        diagnosis=diagnosis, slots=slots.to_dict(), sub_questions=subs,
        coverage=coverage, reflections=reflections,
        tool_results=[r.to_dict() for r in results], trace=trace,
        answer=answer, status="ok",
        extra={"compatibility": compat, "similarCases": similar[:5], "issues": issues,
               "unknownModel": slots.model_unknown, "llm": llm_usage},
    )


# ---------------------------------------------------------------------------
# 各 LLM 节点（每个都「失败返回 None / 回退」，由调用方降级）
# ---------------------------------------------------------------------------

def _decompose(client: LLMClient, store: GraphStore, question: str) -> SlotExtraction | None:
    if not question.strip():
        return None
    try:
        data = client.chat_json(decompose_prompt(store, question), max_tokens=800)
        return SlotExtraction.model_validate(data)
    except (LLMError, ValueError, TypeError) as exc:
        log.warning("LLM 槽位抽取失败，回退确定性抽取: %s", exc)
        return None


def _merge_slots(store: GraphStore, base: Slots, llm: SlotExtraction | None) -> Slots:
    """LLM 只补召回、不覆盖确定性结论；模型/错误码始终以正则+图解析为准。"""
    if llm is None:
        return base
    # 症状：LLM 给的口语症状必须能命中真实节点，才采用其规范名
    if base.symptom is None and llm.symptom:
        hit = store.search(llm.symptom, label="Symptom", limit=1)
        if hit:
            base.symptom = hit[0].name
    # 环境：LLM 补词典之外的使用环境（限定合法取值）
    if base.environment is None and llm.environment in {
        "PET", "LONG-HAIR", "CARPET", "DUPLEX", "LARGE-HOME", "BALCONY",
    }:
        base.environment = llm.environment
    # 意图：network/safety 优先级更高，LLM 可升级 troubleshoot
    if base.intent == "troubleshoot" and llm.intent in ("network", "safety"):
        base.intent = llm.intent
    # 安全/换件意图取并集：漏报安全是错误，宁多勿少
    base.safety_hint = base.safety_hint or llm.safety_hint
    base.replace_intent = base.replace_intent or llm.replace_intent
    for item in llm.unresolved:
        if item and item not in base.unresolved:
            base.unresolved.append(item)
    return base


def _plan(client: LLMClient, store: GraphStore, question: str, slots: Slots,
          diagnosis: Diagnosis, similar: list[dict[str, Any]],
          compat: list[dict[str, Any]], budget: int,
          usage: dict[str, Any]) -> list[dict[str, Any]] | None:
    try:
        summary = _evidence_summary(diagnosis, similar, compat)
        data = client.chat_json(plan_prompt(store, question, slots.to_dict(), summary, budget),
                                max_tokens=900)
        out = PlanOutput.model_validate(data)
        _bump(usage, client)
        if out.done:
            return []
        calls = [c for c in out.calls if c.tool in _EXPLORE_TOOLS]
        return [{"tool": c.tool, "args": c.args} for c in calls][:3]
    except (LLMError, ValueError, TypeError) as exc:
        log.warning("LLM 探索计划失败，回退确定性计划: %s", exc)
        return None


def _reflect(client: LLMClient, store: GraphStore, question: str, slots: Slots,
             summary: str, usage: dict[str, Any]) -> ReflectionOutput | None:
    try:
        data = client.chat_json(reflect_prompt(store, question, slots.to_dict(), summary),
                                max_tokens=900)
        out = ReflectionOutput.model_validate(data)
        _bump(usage, client)
        return out
    except (LLMError, ValueError, TypeError) as exc:
        log.warning("LLM 反思失败，回退确定性覆盖度: %s", exc)
        return None


def _finalize(client: LLMClient, store: GraphStore, question: str, slots: Slots,
              diagnosis: Diagnosis, similar: list[dict[str, Any]],
              compat: list[dict[str, Any]], coverage: dict[str, str],
              usage: dict[str, Any]) -> str:
    """LLM 组答案；失败或产出可疑时回退模板组答案。

    无论 LLM 写了什么，规则告警与免责声明都会被强制保留（安全边界）。
    """
    try:
        data = client.chat_json(
            finalize_prompt(store, question, slots.to_dict(), diagnosis.to_dict(),
                            diagnosis.evidence, similar, compat),
            max_tokens=1600,
        )
        out = FinalAnswerOutput.model_validate(data)
        _bump(usage, client)
        text = (out.answer or "").strip()
        if not text:
            return _compose_safe(diagnosis, similar, compat, slots, coverage)
        return _guard_answer(text, diagnosis)
    except (LLMError, ValueError, TypeError) as exc:
        log.warning("LLM 答案生成失败，回退模板组答案: %s", exc)
        return _compose_safe(diagnosis, similar, compat, slots, coverage)


def _compose_safe(d: Diagnosis, similar, compat, slots: Slots, coverage) -> str:
    """确定性模板答案（复用 graph._compose），保证无 LLM 时行为一致。"""
    return _deterministic._compose(d, similar, compat, slots, coverage)


def _guard_answer(text: str, d: Diagnosis) -> str:
    """强制保留规则告警与免责声明，防止 LLM 丢弃安全边界。"""
    if not text.endswith(DISCLAIMER):
        text = text + " " + DISCLAIMER
    for w in d.warnings:
        if w and w not in text:
            text = text + " " + w
    return text


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

def _bump(usage: dict[str, Any], client: LLMClient) -> None:
    usage["calls"] += 1
    u = getattr(client, "last_usage", {}) or {}
    usage["tokens"] = int(usage.get("tokens", 0)) + int(u.get("totalTokens", 0))


def _fallback_plan(slots: Slots) -> list[dict[str, Any]]:
    """确定性探索计划（与 graph.run 首轮一致）。"""
    plan: list[dict[str, Any]] = []
    if slots.model and not slots.model_unknown:
        plan.append({"tool": "search_similar_cases",
                     "args": {"model": slots.model, "symptom": slots.symptom or "", "days": 365}})
    return plan


def _evidence_summary(d: Diagnosis, similar: list[dict[str, Any]],
                      compat: list[dict[str, Any]]) -> str:
    """裁剪后的证据摘要，控制 LLM 上下文体积。"""
    parts: list[str] = []
    if d.faults:
        parts.append("候选故障：" + "、".join(
            f"{f['name']}({f['confidence']})" for f in d.faults[:3]))
    if d.causes:
        parts.append("候选根因：" + "、".join(
            f"{c['name']}({c['confidence']})" for c in d.causes[:3]))
    if d.solutions:
        parts.append("候选方案：" + "、".join(s["name"] for s in d.solutions[:3]))
    if d.checks:
        parts.append("检查项：" + "、".join(c["name"] for c in d.checks[:4]))
    if d.warnings:
        parts.append("告警：" + "；".join(d.warnings[:3]))
    if d.policies:
        parts.append("策略：" + "、".join(d.policies))
    if similar:
        parts.append(f"相似案例 {len(similar)} 条：" + "、".join(
            str(s.get("caseNo")) for s in similar[:5]))
    if compat:
        parts.append("兼容性：" + "、".join(
            f"{c.get('item')}{'兼容' if c.get('compatible') else '不兼容'}" for c in compat[:4]))
    return "\n".join(parts) or "（尚无证据）"
