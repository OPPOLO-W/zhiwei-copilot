"""查询与规则诊断接口（w2：规则模式）。

范围说明：本阶段只提供结构化参数（model / code / symptom / caseNo）驱动的规则诊断。
自然语言槽位抽取、Agent 模式、compare 模式与 SSE 事件流依赖 `backend.app.agent`，
随 w3 一并引入；在此之前，仅带 question 的请求会返回 422 并说明原因。
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from ..kg import queries as Q
from ..kg.store import GraphStore
from ..reasoning.service import diagnose
from .deps import store_dep
from .schemas import QueryRequest, QueryResponse, RuleReasonRequest

router = APIRouter(prefix="/api", tags=["query"])


def _slots(req: QueryRequest) -> dict[str, Any]:
    return {"model": req.model, "code": req.code, "symptom": req.symptom, "case_no": req.case_no}


@router.post("/query", response_model=QueryResponse)
def query(req: QueryRequest, store: GraphStore = Depends(store_dep)) -> QueryResponse:
    started = time.perf_counter()
    run_id = f"run_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    slots = _slots(req)
    if not any(slots.values()) and not req.check_results:
        raise HTTPException(
            status_code=422,
            detail="本阶段只支持结构化参数定位（model / code / symptom / caseNo）；"
                   "自然语言槽位抽取随 w3 的 Agent 引入",
        )

    try:
        d = diagnose(store, case_no=req.case_no, model=req.model, code=req.code,
                     symptom=req.symptom, check_results=req.check_results)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    payload = QueryResponse(
        runId=run_id, mode="rule", status="ok", answer=d.answer,
        confidence=d.confidence, claims=d.claims, warnings=d.warnings,
        graph=d.graph, proof={"mode": "rule", "derivationTree": d.proof_tree},
        trace=[{"node": "run_rules", "latencyMs": 0, "firedRules": d.trace.get("firedRules")}],
        evidence=d.evidence, slots=slots,
        detail={"faults": d.faults, "causes": d.causes, "solutions": d.solutions,
                "checks": d.checks, "policies": d.policies, "trace": d.trace},
    )
    payload.latencyMs = int((time.perf_counter() - started) * 1000)
    return payload


@router.post("/reason/rules")
def reason_rules(req: RuleReasonRequest, store: GraphStore = Depends(store_dep)) -> dict[str, Any]:
    """规则推理的完整返回（含证明树与证据），供调试与报告引用。"""
    try:
        d = diagnose(store, case_no=req.case_no, model=req.model, code=req.code,
                     symptom=req.symptom, check_results=req.check_results)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return d.to_dict()


@router.get("/cases/{case_no}")
def case_context(case_no: str, store: GraphStore = Depends(store_dep)) -> dict[str, Any]:
    ctx = Q.case_context(store, case_no)
    if ctx is None:
        raise HTTPException(status_code=404, detail=f"案例 {case_no} 不存在")
    return ctx
