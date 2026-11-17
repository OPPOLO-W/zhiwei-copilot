"""LLM 接入层测试：客户端解析、槽位合并、run_llm 与降级路径。

全部用 fake client（monkeypatch），不真实联网；LLM 失败时断言优雅降级到确定性实现。
"""

from __future__ import annotations

import pytest

from backend.app.agent import llm_agent
from backend.app.agent.slots import Slots, extract_slots
from backend.app.config import Settings
from backend.app.llm.client import LLMClient, LLMError, LLMUnavailable
from backend.app.llm.prompts import SlotExtraction


# ---------------------------------------------------------------------------
# 客户端：纯函数单元测试（不联网）
# ---------------------------------------------------------------------------
def test_extract_json_plain():
    assert LLMClient._extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_markdown_fence():
    assert LLMClient._extract_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_extract_json_embedded_in_text():
    assert LLMClient._extract_json('前缀说明 {"a": 1} 后缀') == {"a": 1}


def test_extract_json_invalid_raises():
    with pytest.raises(LLMError):
        LLMClient._extract_json("这不是 JSON")


def test_parse_llm_result():
    body = {
        "model": "deepseek-chat",
        "choices": [{"message": {"role": "assistant", "content": "ok"}}],
        "usage": {"prompt_tokens": 8, "completion_tokens": 1, "total_tokens": 9},
    }
    r = LLMClient._parse(body)
    assert r.content == "ok"
    assert r.total_tokens == 9


def test_chat_without_key_raises_unavailable():
    client = LLMClient(Settings(llm_api_key=""))
    assert client.enabled is False
    with pytest.raises(LLMUnavailable):
        client.chat([{"role": "user", "content": "hi"}])


# ---------------------------------------------------------------------------
# 槽位合并：LLM 只补召回、不覆盖确定性结论
# ---------------------------------------------------------------------------
def test_merge_slots_llm_never_overrides_model(store, model):
    base = extract_slots(store, f"{model} 报 E11")
    llm = SlotExtraction(model="T99 FAKE", code="E99", symptom=None,
                         intent="troubleshoot", safety_hint=False,
                         replace_intent=False, unresolved=[])
    merged = llm_agent._merge_slots(store, base, llm)
    # 型号/错误码仍以确定性抽取为准，LLM 不得覆盖
    assert merged.model == model
    assert merged.code == "E11"


def test_merge_slots_llm_adds_symptom_only_if_resolves(store, model):
    base = Slots(question="", model=model, code="E11")
    # 能命中真实症状节点：采用其规范名
    hit = llm_agent._merge_slots(store, base, SlotExtraction(
        model=None, code=None, symptom="左轮不转", intent="troubleshoot"))
    assert hit.symptom == "左轮不转"
    # 命不中：不采用（防幻觉）
    base2 = Slots(question="", model=model, code="E11")
    miss = llm_agent._merge_slots(store, base2, SlotExtraction(
        model=None, code=None, symptom="一个不存在的症状xyz", intent="troubleshoot"))
    assert miss.symptom is None


def test_merge_slots_union_safety_and_env(store, model):
    base = Slots(question="", model=model, code="E11")
    merged = llm_agent._merge_slots(store, base, SlotExtraction(
        model=None, code=None, symptom=None, environment="PET", intent="safety",
        safety_hint=True, replace_intent=True, unresolved=[]))
    assert merged.environment == "PET"
    assert merged.safety_hint is True
    assert merged.replace_intent is True
    assert merged.intent == "safety"


# ---------------------------------------------------------------------------
# run_llm：fake client 端到端 + 降级
# ---------------------------------------------------------------------------
class _FakeClient:
    enabled = True
    model = "fake-model"

    def __init__(self, handler=None) -> None:
        self.handler = handler

    def chat_json(self, messages, max_tokens=0, **kw):
        if self.handler is None:
            raise LLMError("no handler")
        return self.handler(messages)


def _happy_handler(messages):
    text = messages[-1]["content"]
    if "抽取故障排查槽位" in text:
        return {"model": None, "code": None, "symptom": None, "environment": None,
                "intent": "troubleshoot", "safety_hint": False,
                "replace_intent": False, "unresolved": []}
    if "选择接下来要调用的工具" in text:
        return {"reasoning": "查相似案例", "calls": [], "done": True}
    if "覆盖度与缺口" in text:
        return {"coverage": {}, "conflicts": [], "missing": [], "decision": "stop", "next_calls": []}
    if "带引用的排查答复" in text:
        return {"answer": "最可能为驱动轮异常，建议先检查再考虑换件。",
                "claims": [{"text": "驱动轮异常", "evidenceIds": ["E1"], "confidence": 0.8}],
                "warnings": []}
    raise AssertionError(f"unexpected prompt: {text[:40]}")


def test_run_llm_produces_answer_with_llm(store, model, monkeypatch):
    monkeypatch.setattr(llm_agent, "get_client", lambda: _FakeClient(_happy_handler))
    run = llm_agent.run_llm(store, question=f"{model} 报 E11，左轮不转")
    assert run.status == "ok"
    assert run.diagnosis.faults, "规则骨干必须仍在跑，LLM 不能替代确定性推理"
    assert "驱动轮异常" in run.answer, "应使用 LLM 生成的答复"
    assert "以上判断仅用于辅助排查" in run.answer, "免责声明必须保留"
    assert run.extra["llm"]["calls"] >= 1


def test_run_llm_falls_back_on_llm_error(store, model, monkeypatch):
    def failing(messages):
        raise LLMError("boom")

    monkeypatch.setattr(llm_agent, "get_client", lambda: _FakeClient(failing))
    run = llm_agent.run_llm(store, question=f"{model} 报 E11，左轮不转")
    assert run.status == "ok"
    assert run.diagnosis.faults, "LLM 全链路失败也必须退回确定性推理"
    assert "以上判断仅用于辅助排查" in run.answer


def test_run_llm_disabled_delegates_to_deterministic(store, model, monkeypatch):
    class Disabled(_FakeClient):
        enabled = False

    monkeypatch.setattr(llm_agent, "get_client", lambda: Disabled())
    run = llm_agent.run_llm(store, question=f"{model} 报 E11，左轮不转")
    assert run.status == "ok"
    assert "llm" not in run.extra, "未启用 LLM 时应走纯确定性路径"
    assert run.diagnosis.faults


def test_run_llm_safety_policy_survives_llm(store, monkeypatch):
    """安全规则（R14）来自规则骨干，LLM 无论写什么都不能丢掉 safety_stop。"""
    monkeypatch.setattr(llm_agent, "get_client", lambda: _FakeClient(_happy_handler))
    run = llm_agent.run_llm(store, question="电池鼓包了还能继续用吗")
    assert "safety_stop" in run.diagnosis.policies
