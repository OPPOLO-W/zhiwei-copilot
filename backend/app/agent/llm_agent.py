"""LLM 驱动的 RefKG-lite 工作流（Week 3 的 LLM 接入）。

与 `graph.run` 的确定性实现同构、同一 `AgentRun` 输出，区别在于四个节点由 LLM 驱动：

  * decompose —— LLM 做查询解耦（槽位抽取），再与确定性抽取「取并」，
    模型/错误码仍以正则+图解析为准（防幻觉），症状/环境/意图可用 LLM 补召回；
  * plan/act —— 规则骨干 `run_rules` 固定先跑（安全/网络规则兜底），
    之后 LLM 从白名单里选补充工具，参数只能取自已识别槽位与已收集证据；
  * reflect —— LLM 按 q1~q5 给覆盖度与缺口，决定 continue/stop；
  * rank —— 规则给定候选故障/方案，LLM 只在候选内排序（不得增删，安全方案锁首位）；
  * finalize —— LLM 生成带 evidenceId 引用的答复，规则告警与免责声明强制保留。

健壮性（文档 8.10）：
  * 每个节点 LLM 失败都降级到确定性等价实现，整条链路绝不因 LLM 崩而崩；
  * 槽位经图校验（型号/症状必须命中实体，否则标 unknown），模型不编造实体；
  * 工具调用白名单 + 预算（agent_max_steps / agent_max_reflections）防死循环。
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any

from ..config import get_settings
from ..kg.store import GraphStore
from ..llm.client import LLMClient, LLMError, get_client
from ..llm.prompts import (
    Claim,
    FinalAnswerOutput,
    PlanOutput,
    RankOutput,
    ReflectionOutput,
    SlotExtraction,
    decompose_prompt,
    finalize_prompt,
    plan_prompt,
    rank_prompt,
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
    llm_out = _decompose(client, store, question or "")
    slots = _merge_slots(store, base, llm_out)
    slots.case_no = case_no or slots.case_no
    if model:
        slots.model = model
        slots.model_unknown = None
    slots.code = code or slots.code
    slots.symptom = symptom or slots.symptom
    step("intake", slots=slots.to_dict(), safetyHint=slots.safety_hint,
         llmSlots=bool(llm_out))

    if not any([slots.model, slots.code, slots.symptom, slots.case_no]):
        return _deterministic._out_of_scope(slots, trace, started)

    subs = sub_questions(slots)
    step("decompose", subQuestions=[s["id"] for s in subs])

    # ---------- facts：用户自述事实过门控后才进规则（P2） ----------
    # 门控是硬要求：quote 必须是用户原话子串、实体必须在图里、检查必须能唯一确定故障。
    # 通不过的事实只是不进推理，不影响其它槽位，避免一句话毁掉整轮对话。
    facts, rejected_facts = _gate_facts(store, question or "", getattr(llm_out, "facts", []) or [])
    actions = [f["nodeId"] for f in facts if f["kind"] == "action"]
    # 只喂阳性检查：P2.1 的检查结果来自自由文本，极性靠模型推断，实测会把
    # 「还是充不上」读成「检查结果阴性」并因此排除整条故障（H04 首选方案变空）。
    # 阴性与「没做过」留给 P2.3 的按钮答案（显式极性）再落规则。
    fact_checks = [
        {"check": f["nodeId"], "fault": f.get("faultId") or "", "result": f["result"]}
        for f in facts if f["kind"] == "check" and f.get("faultId") and f["result"] == "positive"
    ]
    step("facts", accepted=facts, rejected=rejected_facts,
         actions=actions, checks=len(fact_checks))

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
        "case_no": slots.case_no, "actions": actions, "check_results": fact_checks,
    }})
    if use_rules:
        diagnosis = diag_fn(store, case_no=slots.case_no, model=slots.model,
                            code=slots.code, symptom=slots.symptom, question=question,
                            actions=actions, check_results=fact_checks)
    else:
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
        # LLM 返回的 coverage 可能只给部分 qid（schema 不强制全量）。直接整体替换会让
        # 缺失键被算作未覆盖，人为压低指标；而 LLM 也不该有权把确定性已覆盖的子问题
        # 凭空判成 covered。规则：以确定性计算为基线，LLM 只能下调（发现缺口），不能上调。
        coverage = _merge_coverage(coverage, refl.coverage)
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

    # ---------- rank：规则定候选，LLM 只在候选内排序（P1）----------
    # 放在探索之后、finalize 之前：此时候选集与相似案例都已就绪，
    # 重排结果既影响最终答案，也直接进入 diagnosis（下游指标可观测）。
    calls_before_rank = llm_usage["calls"]
    ranking = _rank(client, store, question or "", slots, diagnosis, llm_usage)
    audit = _apply_ranking(diagnosis, ranking)
    audit["llmDriven"] = llm_usage["calls"] > calls_before_rank
    step("rank", **audit)

    # ---------- verify：确定性规则校验（安全/网络/不兼容），不交给 LLM ----------
    issues = _deterministic._verify(diagnosis, similar, compat)
    step("verify", issues=issues, policies=diagnosis.policies)

    # ---------- finalize：LLM 生成带引用答复（失败回退模板）----------
    # _finalize 永远返回字符串，不能用「is not None」判断；改看本轮是否真的成功调用了 LLM
    calls_before = llm_usage["calls"]
    final = _finalize(client, store, question or "", slots, diagnosis,
                      similar, compat, coverage, llm_usage)
    answer = final.text
    claims_source = "llm" if any(c.get("source") == "llm" for c in final.claims) else "rule"
    step("finalize", coverage=coverage, llmAnswer=llm_usage["calls"] > calls_before,
         claims=len(final.claims), claimsSource=claims_source,
         latencyMs=int((time.perf_counter() - started) * 1000))

    return _deterministic.AgentRun(
        diagnosis=diagnosis, slots=slots.to_dict(), sub_questions=subs,
        coverage=coverage, reflections=reflections,
        tool_results=[r.to_dict() for r in results], trace=trace,
        answer=answer, status="ok",
        claims=final.claims, warnings=final.warnings,
        extra={"compatibility": compat, "similarCases": similar[:5], "issues": issues,
               "unknownModel": slots.model_unknown, "llm": llm_usage,
               "claimsSource": claims_source},
    )


# ---------------------------------------------------------------------------
# 各 LLM 节点（每个都「失败返回 None / 回退」，由调用方降级）
# ---------------------------------------------------------------------------

_COVERAGE_RANK = {"covered": 2, "partial": 1, "missing": 0}
_COVERAGE_KEYS = ("q1", "q2", "q3", "q4", "q5")


def _merge_coverage(base: dict[str, str], reported: dict[str, str] | None) -> dict[str, str]:
    """把 LLM 报告的覆盖度合并到确定性基线：只允许下调，不允许上调。

    两个理由：
    * 缺键安全：LLM 只回 `{"q3": "missing"}` 时，其余四项仍按基线，不会被当成未覆盖；
    * 权责一致：覆盖度由工具产出的证据决定，LLM 的职责是「读证据发现缺口」，
      不具备「宣称某子问题已覆盖」的权限。
    """
    rep = reported or {}
    merged: dict[str, str] = {}
    for qid in _COVERAGE_KEYS:
        b = base.get(qid, "missing")
        r = rep.get(qid)
        merged[qid] = r if (r in _COVERAGE_RANK and _COVERAGE_RANK[r] < _COVERAGE_RANK[b]) else b
    return merged


def _decompose(client: LLMClient, store: GraphStore, question: str) -> SlotExtraction | None:
    if not question.strip():
        return None
    try:
        data = client.chat_json(decompose_prompt(store, question), max_tokens=1100)
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


# ---------------------------------------------------------------------------
# P2.1：用户自述事实的门控
# ---------------------------------------------------------------------------

_FACT_LABEL = {"action": "Solution", "check": "Check", "cause": "Cause"}
_QUOTE_STRIP = re.compile(r"[\s，。！？、；：,.!?;:~～\-—…（）()\[\]【】\"'“”‘’]+")
_NEGATIONS = ("没有", "没", "未", "不打算", "不想", "不做", "还没")
_FACT_MAX = 5


def _norm(text: str) -> str:
    """去掉空白与标点，只留可核对的内容字符。"""
    return _QUOTE_STRIP.sub("", text or "")


def _resolve_fact_node(store: GraphStore, ref: str, kind: str):
    """ref 可以是节点 ID、code 或规范名；类型必须与 kind 一致，防止把方案当检查用。"""
    label = _FACT_LABEL.get(kind)
    if label is None:
        return None
    ref = (ref or "").strip()
    if not ref:
        return None
    node = store.get(ref)
    if node is not None and node.label == label:
        return node
    up = ref.upper()
    for n in store.nodes(label):
        code = str(n.props.get("code", "")).upper()
        if (code and code == up) or n.name == ref:
            return n
    return None


def _check_fault(store: GraphStore, check_id: str) -> str | None:
    """检查项唯一覆盖的故障。覆盖多个（如左右轮共用的轴承检查）时不猜，留给追问。"""
    faults = [n.id for _edge, n in store.neighbors(check_id, ["DETECTS"], "out", 10)
              if n.label == "Fault"]
    return faults[0] if len(faults) == 1 else None


def _gate_facts(store: GraphStore, question: str,
                drafts: list[Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """把 LLM 抽的用户事实过门控（P2.1）。

    七道门：实体存在于图谱、quote 逐字来自用户问题、极性一致、检查唯一对应故障、
    result 在枚举内、unknown 不写入、最多采信 `_FACT_MAX` 条。
    通不过只丢这一条，不影响其它槽位与事实——一句话说错不该毁掉整轮对话。
    """
    qn = _norm(question)
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []

    def drop(kind: str, ref: str, reason: str, quote: str = "") -> None:
        rejected.append({"kind": kind, "ref": ref, "reason": reason, "quote": quote})

    for draft in list(drafts)[: _FACT_MAX * 2]:
        kind = str(getattr(draft, "kind", "") or "")
        ref = str(getattr(draft, "ref", "") or "")
        quote = str(getattr(draft, "quote", "") or "")
        result = str(getattr(draft, "result", "positive") or "positive")
        node = _resolve_fact_node(store, ref, kind)
        if node is None:
            drop(kind, ref, "unknown_entity", quote)
            continue
        nq = _norm(quote)
        if len(nq) < 2 or nq not in qn:
            # 模型改写过的 quote 无法核对，一律不采信：这是防幻觉的第一道也是最后一道
            drop(kind, ref, "quote_not_verbatim", quote)
            continue
        negated = any(w in quote for w in _NEGATIONS)
        if kind == "action" and (negated or result == "negative"):
            drop(kind, ref, "negated_action", quote)
            continue
        if kind == "check" and negated and result == "positive":
            drop(kind, ref, "polarity_mismatch", quote)
            continue
        if result not in ("positive", "negative", "unknown") or result == "unknown":
            drop(kind, ref, "result_unknown", quote)
            continue
        item: dict[str, Any] = {"kind": kind, "nodeId": node.id, "name": node.name,
                                "ref": ref, "quote": quote, "result": result}
        if kind == "check":
            fault_id = _check_fault(store, node.id)
            if fault_id is None:
                drop(kind, ref, "check_covers_multiple_faults", quote)
                continue
            item["faultId"] = fault_id
        elif kind == "action":
            item["effect"] = ("excludes" if node.props.get("excludesCauses")
                              else "exhausts" if node.props.get("exhaustsSelf") else "none")
        if len(accepted) >= _FACT_MAX:
            drop(kind, ref, "over_limit", quote)
            continue
        accepted.append(item)
    return accepted, rejected


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


@dataclass
class _Ranking:
    """LLM 给出的候选顺序（nodeId 列表）。

    这里只保留「顺序」而不是整条候选记录：重排的权限仅限于先后，
    候选集合、分数、证据都由规则与图决定，LLM 不得增删改写。
    """

    faults: list[Any]
    solutions: list[Any]
    note: str = ""


_RANK_MAX = 4
# 升级/停用类方案：属于安全边界，规则没要求时 LLM 不得把它们提到首位
_ESCALATION = {"SOL:ESCALATE-SERVICE", "SOL:SAFETY-STOP"}
_ESCALATION_HINTS = ("售后", "停用")


def _is_escalation(item: dict[str, Any]) -> bool:
    return item.get("id") in _ESCALATION or any(
        hint in str(item.get("name", "")) for hint in _ESCALATION_HINTS
    )


def _candidate_evidence(d: Diagnosis, node_id: str) -> list[str]:
    """某候选节点在证据清单里出现的编号，用来约束 LLM 只能引用已有证据。"""
    return [e["id"] for e in d.evidence if node_id in (e.get("triple") or [])]


def _candidates(d: Diagnosis) -> dict[str, list[dict[str, Any]]]:
    """喂给 LLM 的候选清单：只给 id/名称/分数/可用证据编号。"""
    return {
        "faults": [
            {"nodeId": f["id"], "name": f["name"], "confidence": f.get("confidence"),
             "evidenceIds": _candidate_evidence(d, f["id"])}
            for f in d.faults[:_RANK_MAX]
        ],
        "solutions": [
            {"nodeId": s["id"], "name": s["name"], "score": s.get("score"),
             "riskLevel": s.get("riskLevel"), "evidenceIds": _candidate_evidence(d, s["id"])}
            for s in d.solutions[:_RANK_MAX]
        ],
    }


def _rank(client: LLMClient, store: GraphStore, question: str, slots: Slots,
          d: Diagnosis, usage: dict[str, Any]) -> _Ranking | None:
    if os.environ.get("KG_NO_RANK") == "1":
        # 消融开关：只关重排、其余节点不变，用于回答「重排本身贡献了多少」。
        # 生产路径不设这个变量，默认开启。
        return None
    cands = _candidates(d)
    if len(cands["faults"]) < 2 and len(cands["solutions"]) < 2:
        # 只有唯一候选时排序没有信息量，省掉一次调用
        return None
    try:
        data = client.chat_json(
            rank_prompt(store, question, slots.to_dict(), cands,
                        {"warnings": d.warnings, "policies": d.policies}),
            max_tokens=900,
        )
        out = RankOutput.model_validate(data)
        _bump(usage, client)
        return _Ranking(faults=list(out.faults), solutions=list(out.solutions), note=out.note)
    except (LLMError, ValueError, TypeError) as exc:
        log.warning("LLM 候选重排失败，保留规则顺序: %s", exc)
        return None


def _apply_ranking(d: Diagnosis, ranking: _Ranking | None) -> dict[str, Any]:
    """按 LLM 给出的顺序重排 diagnosis 的候选；不增不删，非法项记入 rejected。

    安全边界：`safety_stop` 生效时，规则选出的首选方案被锁在第一位，
    LLM 只能调整其余方案——安全结论不参与「更懂用户」的权衡。
    """
    audit: dict[str, Any] = {
        "driver": "llm" if ranking else "rule",
        "locked": False,
        "rejected": [],
        "evidenceHits": 0,
        "evidenceMiss": 0,
        # 模型给排序的一句话理由，落进 trace 便于人工复核（不参与任何判定）
        "note": (ranking.note if ranking else "") or "",
        "faults": [f["id"] for f in d.faults],
        "solutions": [s["id"] for s in d.solutions],
    }
    if ranking is None:
        return audit

    for field, picks in (("faults", ranking.faults), ("solutions", ranking.solutions)):
        items = getattr(d, field)
        if len(items) < 2 or not picks:
            continue
        index = {it["id"]: it for it in items}
        allowed = {it["id"]: set(_candidate_evidence(d, it["id"])) for it in items}
        ordered: list[dict[str, Any]] = []
        seen: set[str] = set()
        for pick in picks:
            node_id = str(getattr(pick, "nodeId", "") or "")
            if node_id not in index:
                audit["rejected"].append(node_id)
                continue
            if node_id in seen:
                continue
            seen.add(node_id)
            ordered.append(index[node_id])
            for ev in getattr(pick, "evidenceIds", None) or []:
                if ev in allowed[node_id]:
                    audit["evidenceHits"] += 1
                else:
                    audit["evidenceMiss"] += 1
        # LLM 没提到的候选按规则原序接在后面：重排不是删候选
        ordered.extend(it for it in items if it["id"] not in seen)
        if field == "solutions" and "safety_stop" in d.policies:
            head = items[0]
            if ordered[0]["id"] != head["id"]:
                audit["locked"] = True
                audit.setdefault("locks", []).append("safety_stop")
                ordered = [head] + [it for it in ordered if it["id"] != head["id"]]
        if field == "solutions" and _is_escalation(ordered[0]) and not _is_escalation(items[0]):
            # 升级/停用是安全边界，不是排序偏好：规则没要求升级时，LLM 不能自己升级。
            # 实测 H01/H02 会把「转官方售后」提到「更换轮组总成」前面，答案对但首选方案错。
            audit["locked"] = True
            audit.setdefault("locks", []).append("escalation")
            ordered = items[:]
        setattr(d, field, ordered)
        audit[field] = [it["id"] for it in ordered]
    return audit


@dataclass
class _Final:
    """finalize 节点的产物。

    把「答案文本」和「答案自带的结论/告警」分开返回：以前只取 `out.answer`，
    LLM 输出的 claims 被直接丢弃，下游只能拿规则链的 claims 当作答案的引用，
    于是「LLM 引用了什么证据」既无法展示也无法度量。
    """

    text: str
    claims: list[dict[str, Any]]
    warnings: list[str]


def _finalize(client: LLMClient, store: GraphStore, question: str, slots: Slots,
              diagnosis: Diagnosis, similar: list[dict[str, Any]],
              compat: list[dict[str, Any]], coverage: dict[str, str],
              usage: dict[str, Any]) -> _Final:
    """LLM 组答案；失败或产出可疑时回退模板组答案。

    无论 LLM 写了什么，规则告警与免责声明都会被强制保留（安全边界）。
    返回的 claims 标记 `source="llm"`，与规则链 claims（`ruleIds` 非空）可区分。
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
            return _fallback_final(diagnosis, similar, compat, slots, coverage)
        return _Final(
            text=_guard_answer(text, diagnosis),
            claims=[_claim_payload(c) for c in out.claims],
            warnings=_merge_warnings(diagnosis.warnings, out.warnings),
        )
    except (LLMError, ValueError, TypeError) as exc:
        log.warning("LLM 答案生成失败，回退模板组答案: %s", exc)
        return _fallback_final(diagnosis, similar, compat, slots, coverage)


def _fallback_final(diagnosis: Diagnosis, similar, compat, slots: Slots,
                    coverage) -> _Final:
    """模板组答案时，claims 沿用规则链的（模板答案与它们同源）。"""
    return _Final(
        text=_compose_safe(diagnosis, similar, compat, slots, coverage),
        claims=[dict(c) for c in diagnosis.claims],
        warnings=list(diagnosis.warnings),
    )


def _claim_payload(claim: Claim) -> dict[str, Any]:
    """LLM claim → 响应契约：补 `ruleIds=[]` 与 `source="llm"`，前端不必分叉渲染。"""
    return {
        "text": claim.text,
        "evidenceIds": list(claim.evidenceIds),
        "confidence": claim.confidence,
        "ruleIds": [],
        "source": "llm",
    }


def _merge_warnings(*groups: list[str] | None) -> list[str]:
    """多来源告警去重保序：规则告警与 LLM 告警都要保留。"""
    out: list[str] = []
    for group in groups:
        for item in group or []:
            text = str(item).strip()
            if text and text not in out:
                out.append(text)
    return out


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
