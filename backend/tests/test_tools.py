"""工具层测试：文档正文、文档检索、运行时引用校验（P3）。

此前 `get_document_span` 只回元数据、工具返回内容也不进上下文，这三条用例锁住行为，
避免以后又把「能取到的证据」退化成「只报计数」。
"""

from __future__ import annotations

from backend.app.agent.llm_agent import _evidence_summary, _verify_claims
from backend.app.agent.tools import Tools
from backend.app.llm.prompts import Claim
from backend.app.reasoning.service import Diagnosis


def _diag() -> Diagnosis:
    return Diagnosis(
        case_id="CASE:1", model="T20 PRO", symptoms=[],
        faults=[{"id": "FAULT:LEFT-WHEEL-STUCK", "name": "左驱动轮卡滞", "confidence": 0.82}],
        causes=[], solutions=[{"id": "SOL:CLEAN-WHEEL", "name": "清理轮组缠绕物"}],
        checks=[], warnings=[], policies=[], confidence=0.8, answer="", claims=[],
        evidence=[{"id": "E1", "source": "DOC:T20PRO-MANUAL", "text": "左轮不转提示左驱动轮卡滞"}],
        proof_tree=None, graph={}, trace={},
    )


def test_get_document_span_returns_text(store):
    tools = Tools(store)
    data = tools.get_document_span("DOC:T20PRO-MANUAL", limit=3)
    assert data is not None
    assert data["chunkCount"] > 0
    assert data["passages"], "文档工具必须返回正文段落"
    first = data["passages"][0]
    assert first["chunkId"].startswith("DOC:T20PRO-MANUAL#")
    assert first["text"]


def test_get_document_span_unknown_doc_returns_none(store):
    assert Tools(store).get_document_span("DOC:NOT-EXISTS") is None


def test_search_documents_returns_chunk_ids(store):
    hits = Tools(store).search_documents("清理轮组缠绕物", k=3)
    assert hits, "手册/FAQ 里存在该动作的说明"
    assert all(h["chunkId"].startswith("DOC:") for h in hits)
    assert all(h["text"] for h in hits)


def test_verify_claims_drops_fabricated_ids():
    """悬空证据：命中部分保留、全悬空整条丢弃并计数。"""
    claims = [
        Claim(text="左驱动轮卡滞", evidenceIds=["E1", "E999"], confidence=0.8),
        Claim(text="编造的结论", evidenceIds=["E404"], confidence=0.9),
        Claim(text="无引用但保留", evidenceIds=[], confidence=0.5),
    ]
    kept, dropped = _verify_claims(claims, [{"id": "E1", "text": "…"}])
    assert dropped == 1
    assert [c["text"] for c in kept] == ["左驱动轮卡滞", "无引用但保留"]
    assert kept[0]["evidenceIds"] == ["E1"]


def test_evidence_summary_states_tool_findings(store):
    diag = _diag()
    text = _evidence_summary(diag, [], [], store, ["文档片段：DOC:T20PRO-MANUAL p3c2 清理轮组缠绕物"])
    assert "工具取证" in text
    assert "DOC:T20PRO-MANUAL" in text
    assert "FAULT:LEFT-WHEEL-STUCK" in text
