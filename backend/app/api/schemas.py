"""Pydantic 请求/响应模型（运行时校验，防止前后端契约漂移）。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

Mode = Literal["rule", "agent", "compare"]


class QueryRequest(BaseModel):
    question: str | None = Field(default=None, description="自然语言问题")
    case_no: str | None = Field(default=None, alias="caseNo")
    model: str | None = Field(default=None, description="型号，如 T20 PRO")
    code: str | None = Field(default=None, description="错误码，如 E11 / 10010")
    symptom: str | None = Field(default=None, description="症状名或口语描述")
    check_results: list[dict[str, str]] = Field(default_factory=list, alias="checkResults")
    session_id: str | None = Field(default=None, alias="sessionId",
                                   description="多轮会话 id；首次留空，响应会返回一个")
    mode: Mode = "rule"
    use_llm: bool = Field(default=False, alias="useLlm")

    model_config = {"populate_by_name": True}

    @model_validator(mode="after")
    def _need_signal(self) -> "QueryRequest":
        # 多轮追问（「还是不行」）本身没有槽位，靠 session 继承，不能按无信号拒绝
        # 追问按钮回传：只有 sessionId + checkResults，没有新问句
        if self.session_id and (self.question or self.check_results):
            return self
        if not any([self.question, self.case_no, self.code, self.symptom]):
            raise ValueError("至少提供 question / caseNo / model+code / symptom 之一")
        return self


class QueryResponse(BaseModel):
    runId: str
    sessionId: str | None = None
    mode: str
    status: str = "ok"
    answer: str = ""
    confidence: float = 0.0
    claims: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    graph: dict[str, Any] = Field(default_factory=dict)
    proof: dict[str, Any] = Field(default_factory=dict)
    trace: list[dict[str, Any]] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    slots: dict[str, Any] = Field(default_factory=dict)
    askUser: dict[str, Any] | None = Field(default=None, description="需要用户回答的追问（checkId/问题/选项）")
    latencyMs: int = 0
    ruleResult: dict[str, Any] | None = None
    agentResult: dict[str, Any] | None = None
    detail: dict[str, Any] | None = None


class RuleReasonRequest(BaseModel):
    case_no: str | None = Field(default=None, alias="caseNo")
    model: str | None = None
    code: str | None = None
    symptom: str | None = None
    check_results: list[dict[str, str]] = Field(default_factory=list, alias="checkResults")

    model_config = {"populate_by_name": True}


class SubgraphRequest(BaseModel):
    node_ids: list[str] = Field(alias="nodeIds")
    depth: int = 1
    rel_types: list[str] | None = Field(default=None, alias="relTypes")
    limit: int = 80

    model_config = {"populate_by_name": True}
