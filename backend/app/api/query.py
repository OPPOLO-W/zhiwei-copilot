"""查询与推理相关接口。"""

from __future__ import annotations

import time
import uuid
from collections import OrderedDict
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from ..agent import graph as agent_graph
from ..agent.session import get_session_store
from ..agent.slots import Slots, extract_slots
from ..kg import queries as Q
from ..kg.store import GraphStore
from ..observability.trace import write_trace
from ..reasoning.service import diagnose
from .deps import iter_events, store_dep
from .schemas import QueryRequest, QueryResponse, RuleReasonRequest

router = APIRouter(prefix="/api", tags=["query"])

# 进程内运行缓存：仅用于 SSE 回放与前端刷新，不是持久化存储
RUNS: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
RUNS_MAX = 200


def _remember(run_id: str, payload: dict[str, Any], events: list[dict[str, Any]]) -> None:
    RUNS[run_id] = {"payload": payload, "events": events}
    while len(RUNS) > RUNS_MAX:
        RUNS.popitem(last=False)


def _slots_from_request(store: GraphStore, req: QueryRequest) -> dict[str, Any]:
    if req.question and not (req.model or req.code or req.symptom or req.case_no):
        slots = extract_slots(store, req.question)
        return {"model": slots.model, "code": slots.code, "symptom": slots.symptom,
                "case_no": slots.case_no}
    return {"model": req.model, "code": req.code, "symptom": req.symptom, "case_no": req.case_no}


def _merge_warnings(*groups: list[str] | None) -> list[str]:
    """规则告警与 LLM 告警都要保留：去重保序，不排序（安全提示应当排在最前）。"""
    out: list[str] = []
    for group in groups:
        for item in group or []:
            text = str(item).strip()
            if text and text not in out:
                out.append(text)
    return out


def _final_claims(run, diagnosis) -> list[dict[str, Any]]:
    """优先用答案自带的 claims；没有则回落规则链 claims。

    LLM 路径下 `run.claims` 是模型为这段答案自己标注的引用，而 `diagnosis.claims`
    是规则模板的引用——两者混用会把「模型引用的证据」与「规则产出的证据」算成同一件事。
    """
    claims = getattr(run, "claims", None) or []
    return claims or diagnosis.claims


def _with_doc_evidence(diagnosis, run) -> list[dict[str, Any]]:
    """图谱边证据 + 工具取到的手册正文。

    P3 之后 LLM 的 claim 可以引用 chunkId（如 DOC:T20PRO-MANUAL#p3c2）指向手册原文；
    响应里的 evidence 必须包含这些条目，否则前端点 claim 找不到对应行。
    """
    items = list(diagnosis.evidence)
    seen = {str(e.get("id")) for e in items}
    for item in (getattr(run, "extra", None) or {}).get("docEvidence") or []:
        if item.get("id") and str(item["id"]) not in seen:
            seen.add(str(item["id"]))
            items.append(item)
    return items


def _run_agent_dispatch(store: GraphStore, req: QueryRequest, prior_facts: list[dict] | None = None,
                        inherited: dict[str, Any] | None = None,
                        asked_checks: list[str] | None = None,
                        check_results: list[dict] | None = None):
    """Agent 执行入口：`useLlm` 且已配置 key 时走 LLM 驱动，否则确定性实现。

    `inherited` 是会话里带下来的槽位：追问句（「还是不行」）自己抽不出型号/错误码，
    不继承就会整段降级成「请补充型号」。
    """
    inherited = inherited or {}
    model = req.model or inherited.get("model")
    code = req.code or inherited.get("code")
    symptom = req.symptom or inherited.get("symptom")
    case_no = req.case_no or inherited.get("case_no")
    if req.use_llm:
        from ..agent.llm_agent import run_llm

        # 只问一次：答完这一轮就该给结论，继续追问会变成二十问
        return run_llm(store, question=req.question, model=model, code=code,
                       symptom=symptom, case_no=case_no, prior_facts=prior_facts,
                       check_results=check_results, allow_ask=not asked_checks,
                       asked_checks=asked_checks)
    return agent_graph.run(store, question=req.question, model=model, code=code,
                           symptom=symptom, case_no=case_no, check_results=check_results)


def _divergence(rule_d, agent_d) -> dict[str, Any]:
    """compare 模式的核心产物：规则与 Agent 两条链路的差异摘要。"""

    def top(items: list[dict[str, Any]], key: str) -> str | None:
        return items[0][key] if items else None

    return {
        "topFaultSame": bool(rule_d.faults and agent_d.faults
                             and top(rule_d.faults, "id") == top(agent_d.faults, "id")),
        "topSolutionSame": bool(rule_d.solutions and agent_d.solutions
                                and top(rule_d.solutions, "id") == top(agent_d.solutions, "id")),
        "ruleTopFault": rule_d.faults[0]["name"] if rule_d.faults else None,
        "agentTopFault": agent_d.faults[0]["name"] if agent_d.faults else None,
        "ruleFaultCount": len(rule_d.faults),
        "agentFaultCount": len(agent_d.faults),
        "onlyInRule": sorted(set(rule_d.policies) - set(agent_d.policies)),
        "onlyInAgent": sorted(set(agent_d.policies) - set(rule_d.policies)),
    }


def _slots_payload(store: GraphStore, req: QueryRequest) -> dict[str, Any]:
    """响应里回显的槽位：与 Agent 模式同构，前端不必为两种模式写两套取值。"""
    if req.question and not (req.model or req.code or req.symptom or req.case_no):
        return extract_slots(store, req.question).to_dict()
    return Slots(question=req.question or "", model=req.model, code=req.code,
                 symptom=req.symptom, case_no=req.case_no).to_dict()


@router.post("/query", response_model=QueryResponse)
def query(req: QueryRequest, store: GraphStore = Depends(store_dep)) -> QueryResponse:
    started = time.perf_counter()
    run_id = f"run_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    slots = _slots_from_request(store, req)
    sessions = get_session_store()
    session = sessions.get_or_create(req.session_id, slots)
    sessions.merge_slots(session, slots)
    # rule 模式没有槽位就无法推理；agent/compare 交给 Agent 的 out_of_scope 分支给出引导性回答
    if not any(slots.values()) and not session.slots and not req.check_results and req.mode == "rule":
        raise HTTPException(status_code=422, detail="未能从问题中识别出型号/错误码/症状，请补充结构化参数")
    events: list[dict[str, Any]] = []
    # 本轮按钮答案 + 会话里累积的答案：不合并的话「第二次追问」会忘掉第一次的结论
    check_results = list(session.check_results) + list(req.check_results or [])

    try:
        if req.mode == "agent":
            # 传原始请求字段而不是预抽结果：Agent 会自己抽槽位，
            # 二次注入会让「型号未收录」标记被当成结构化参数覆盖掉。
            run = _run_agent_dispatch(store, req, prior_facts=session.facts,
                                      inherited=session.slots,
                                      asked_checks=session.asked_checks,
                                      check_results=check_results)
            d = run.diagnosis
            sessions.add_facts(session, run.extra.get("facts") or [])
            sessions.add_check_results(session, req.check_results)
            if run.extra.get("askUser"):
                sessions.add_asked(session, [run.extra["askUser"]["checkId"]])
            session.last_run_id = run_id
            payload = QueryResponse(
                runId=run_id, sessionId=session.session_id, mode="agent",
                status=run.status, answer=run.answer,
                askUser=run.extra.get("askUser"),
                confidence=d.confidence, claims=_final_claims(run, d),
                warnings=_merge_warnings(run.warnings, d.warnings),
                graph=d.graph, proof={"mode": "agent", "derivationTree": d.proof_tree},
                trace=run.trace, evidence=_with_doc_evidence(d, run), slots=run.slots,
                detail={
                    "subQuestions": run.sub_questions, "coverage": run.coverage,
                    "reflections": run.reflections, "toolResults": run.tool_results,
                    "compatibility": run.extra.get("compatibility"),
                    "similarCases": run.extra.get("similarCases"),
                    "issues": run.extra.get("issues"),
                    # none = 确定性路径（claims 来自规则链）；llm = 答案自带 claims
                    "claimsSource": run.extra.get("claimsSource"),
                    # 与 rule 模式保持同构，前端面板不必为两种模式写两套取值
                    "faults": d.faults, "causes": d.causes, "solutions": d.solutions,
                    "checks": d.checks, "policies": d.policies, "notes": d.notes,
                    "ruleTrace": d.trace,
                    # 会话：本轮采信的事实与被门控拒绝的事实都要可见，否则「为什么没换方案」无法复盘
                    "session": {"turn": session.turn, "facts": session.facts,
                                "rejectedFacts": run.extra.get("rejectedFacts") or []},
                },
            )
            events = _events_from_trace(run.trace, run_id)

        elif req.mode == "compare":
            rule_d = diagnose(store, case_no=slots["case_no"] or session.slots.get("case_no"),
                              model=slots["model"] or session.slots.get("model"),
                              code=slots["code"] or session.slots.get("code"),
                              symptom=slots["symptom"] or session.slots.get("symptom"),
                              check_results=check_results)
            run = _run_agent_dispatch(store, req, prior_facts=session.facts,
                                      inherited=session.slots,
                                      asked_checks=session.asked_checks,
                                      check_results=check_results)
            agent_d = run.diagnosis
            sessions.add_facts(session, run.extra.get("facts") or [])
            sessions.add_check_results(session, req.check_results)
            if run.extra.get("askUser"):
                sessions.add_asked(session, [run.extra["askUser"]["checkId"]])
            session.last_run_id = run_id
            # to_dict() 里的 answer 是规则模板；LLM 路径下真实答案只存在 run.answer，
            # 不覆盖就会导致「对比」两侧答案逐字相同
            final_answer = run.answer or agent_d.answer
            agent_payload = agent_d.to_dict()
            agent_payload["answer"] = final_answer
            # 对比模式下 Agent 一侧的 claims 同样要指向它自己的答案，而不是规则模板
            agent_payload["claims"] = _final_claims(run, agent_d)
            agent_payload["warnings"] = _merge_warnings(run.warnings, agent_d.warnings)
            payload = QueryResponse(
                runId=run_id, sessionId=session.session_id, mode="compare", status="ok",
                answer=final_answer, confidence=agent_d.confidence,
                claims=agent_payload["claims"],
                warnings=sorted(set(rule_d.warnings + agent_payload["warnings"])),
                graph=agent_d.graph, proof={"mode": "hybrid", "derivationTree": rule_d.proof_tree},
                trace=run.trace, evidence=_with_doc_evidence(agent_d, run), slots=run.slots,
                askUser=run.extra.get("askUser"),
                ruleResult=rule_d.to_dict(), agentResult=agent_payload,
                detail={"coverage": run.coverage, "toolResults": run.tool_results,
                        "reflections": run.reflections,
                        "policies": agent_d.policies,
                        "faults": agent_d.faults, "causes": agent_d.causes,
                        "solutions": agent_d.solutions, "checks": agent_d.checks,
                        "rulePolicies": rule_d.policies,
                        "divergence": _divergence(rule_d, agent_d)},
            )
            events = _events_from_trace(run.trace, run_id)

        else:
            d = diagnose(store, case_no=slots["case_no"] or session.slots.get("case_no"),
                         model=slots["model"] or session.slots.get("model"),
                         code=slots["code"] or session.slots.get("code"),
                         symptom=slots["symptom"] or session.slots.get("symptom"),
                         check_results=check_results)
            sessions.add_check_results(session, req.check_results)
            payload = QueryResponse(
                runId=run_id, sessionId=session.session_id, mode="rule", status="ok", answer=d.answer,
                confidence=d.confidence, claims=d.claims, warnings=d.warnings,
                graph=d.graph, proof={"mode": "rule", "derivationTree": d.proof_tree},
                trace=[{"node": "run_rules", "latencyMs": 0, "firedRules": d.trace.get("firedRules")}],
                evidence=d.evidence, slots=_slots_payload(store, req),
                detail={"faults": d.faults, "causes": d.causes, "solutions": d.solutions,
                        "checks": d.checks, "policies": d.policies, "trace": d.trace},
            )
            events = [{"event": "plan", "data": {"mode": "rule"}},
                      {"event": "done", "data": {"runId": run_id}}]

    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    payload.latencyMs = int((time.perf_counter() - started) * 1000)
    write_trace(run_id, {"question": req.question, "slots": payload.slots, "mode": req.mode,
                         "latencyMs": payload.latencyMs, "status": payload.status,
                         "trace": payload.trace})
    _remember(run_id, payload.model_dump(), events)
    return payload


@router.get("/query/{run_id}/stream")
def stream(run_id: str) -> StreamingResponse:
    record = RUNS.get(run_id)
    if record is None:
        raise HTTPException(status_code=404, detail="run 不存在或已过期")
    return StreamingResponse(iter_events(record["events"]), media_type="text/event-stream")


@router.post("/reason/rules")
def reason_rules(req: RuleReasonRequest, store: GraphStore = Depends(store_dep)) -> dict[str, Any]:
    d = diagnose(store, case_no=req.case_no, model=req.model, code=req.code,
                 symptom=req.symptom, check_results=req.check_results)
    return d.to_dict()


@router.get("/cases/{case_no}")
def case_context(case_no: str, store: GraphStore = Depends(store_dep)) -> dict[str, Any]:
    ctx = Q.case_context(store, case_no)
    if ctx is None:
        raise HTTPException(status_code=404, detail=f"案例 {case_no} 不存在")
    return ctx


def _events_from_trace(trace: list[dict[str, Any]], run_id: str) -> list[dict[str, Any]]:
    # retry = 确定性路径的覆盖度重试；reflect = LLM 路径的语义反思（两者语义不同，不合并）
    mapping = {"decompose": "plan", "act": "tool_call", "retry": "retry",
               "reflect": "reflection", "finalize": "done"}
    events: list[dict[str, Any]] = []
    for step in trace:
        node = step.get("node", "")
        if node == "act":
            events.append({"event": "tool_call", "data": step})
            events.append({"event": "tool_result", "data": {**step, "event": "tool_result"}})
        elif node in mapping:
            events.append({"event": mapping[node], "data": step})
    events.append({"event": "done", "data": {"runId": run_id}})
    return events
