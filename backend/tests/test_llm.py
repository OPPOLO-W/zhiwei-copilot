"""LLM 接入层测试：客户端解析、槽位合并、run_llm 与降级路径。

全部用 fake client（monkeypatch），不真实联网；LLM 失败时断言优雅降级到确定性实现。
"""

from __future__ import annotations

import json

import pytest

from backend.app.agent import graph, llm_agent
from backend.app.agent.slots import Slots, extract_slots
from backend.app.config import Settings
from backend.app.llm.client import LLMClient, LLMError, LLMUnavailable
from backend.app.llm.prompts import RankPick, SlotExtraction
from backend.app.reasoning.service import Diagnosis


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
    if "在候选【内部】排序" in text:
        # 默认不改变顺序：只回空列表，验证「无主张时保持规则原序」
        return {"faults": [], "solutions": [], "note": ""}
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


def test_run_llm_keeps_llm_claims(store, model, monkeypatch):
    """答案自带 claims 必须回填，否则「这段话引用了什么」只能从字符串反推。"""
    monkeypatch.setattr(llm_agent, "get_client", lambda: _FakeClient(_happy_handler))
    run = llm_agent.run_llm(store, question=f"{model} 报 E11，左轮不转")
    assert run.claims, "LLM 答案的 claims 必须保留"
    assert run.claims[0]["source"] == "llm"
    assert run.claims[0]["evidenceIds"] == ["E1"]
    assert run.claims[0]["ruleIds"] == []
    assert run.extra["claimsSource"] == "llm"


def test_run_llm_merges_llm_warnings_with_rule_warnings(store, monkeypatch):
    """LLM 追加的告警要保留，规则告警也不能被模型丢掉。"""

    def handler(messages):
        if "带引用的排查答复" in messages[-1]["content"]:
            return {"answer": "电池鼓包请立即停用并联系售后。", "claims": [],
                    "warnings": ["请勿自行拆解电池。"]}
        return _happy_handler(messages)

    monkeypatch.setattr(llm_agent, "get_client", lambda: _FakeClient(handler))
    run = llm_agent.run_llm(store, question="电池鼓包了还能继续用吗")
    assert "请勿自行拆解电池。" in run.warnings
    assert set(run.diagnosis.warnings).issubset(set(run.warnings))


def test_run_llm_falls_back_on_llm_error(store, model, monkeypatch):
    def failing(messages):
        raise LLMError("boom")

    monkeypatch.setattr(llm_agent, "get_client", lambda: _FakeClient(failing))
    run = llm_agent.run_llm(store, question=f"{model} 报 E11，左轮不转")
    assert run.status == "ok"
    assert run.diagnosis.faults, "LLM 全链路失败也必须退回确定性推理"
    assert "以上判断仅用于辅助排查" in run.answer
    assert run.extra["claimsSource"] == "rule"
    assert all(c.get("source") != "llm" for c in run.claims)
    assert run.claims == run.diagnosis.claims, "模板答案与规则 claims 同源"


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


# ---------------------------------------------------------------------------
# 覆盖度合并：LLM 只能下调，不能上调（曾导致 LLM 组 coverage 虚低 13pp）
# ---------------------------------------------------------------------------
def test_merge_coverage_missing_keys_fall_back_to_base():
    """LLM 只回部分 qid 时，其余按基线，不得被当成未覆盖。"""
    base = {"q1": "covered", "q2": "covered", "q3": "covered",
            "q4": "covered", "q5": "covered"}
    merged = llm_agent._merge_coverage(base, {"q3": "missing"})
    assert merged["q3"] == "missing"
    for qid in ("q1", "q2", "q4", "q5"):
        assert merged[qid] == "covered", f"{qid} 缺键时应回落基线"


def test_merge_coverage_cannot_upgrade():
    """LLM 无权把确定性已覆盖的子问题宣称成 covered。"""
    base = {"q1": "covered", "q2": "partial", "q3": "missing",
            "q4": "covered", "q5": "covered"}
    merged = llm_agent._merge_coverage(base, {"q3": "covered", "q2": "covered"})
    assert merged["q3"] == "missing", "不得上调 missing → covered"
    assert merged["q2"] == "partial", "不得上调 partial → covered"


def test_merge_coverage_empty_report_keeps_base():
    base = {"q1": "covered", "q2": "partial", "q3": "missing",
            "q4": "covered", "q5": "covered"}
    assert llm_agent._merge_coverage(base, None) == base
    assert llm_agent._merge_coverage(base, {}) == base


def test_merge_coverage_downgrade_is_accepted():
    """LLM 读证据发现缺口时，下调必须被采纳。"""
    base = {q: "covered" for q in ("q1", "q2", "q3", "q4", "q5")}
    merged = llm_agent._merge_coverage(base, {"q3": "partial", "q4": "missing"})
    assert merged["q3"] == "partial"
    assert merged["q4"] == "missing"


# ---------------------------------------------------------------------------
# 候选重排（P1）：规则定候选，LLM 只决定先后，非法输入被拒
# ---------------------------------------------------------------------------
def _mk_diag(fault_ids, solution_ids, policies=(), warnings=()) -> Diagnosis:
    evidence = [{"id": f"E{i}", "triple": [nid, "INDICATES", "PART:x"]}
                for i, nid in enumerate(list(fault_ids) + list(solution_ids), start=1)]
    return Diagnosis(
        case_id="CASE:1", model="T20 PRO", symptoms=[],
        faults=[{"id": nid, "name": f"故障{nid}"} for nid in fault_ids],
        causes=[],
        solutions=[{"id": nid, "name": f"方案{nid}"} for nid in solution_ids],
        checks=[], warnings=list(warnings), policies=list(policies),
        confidence=0.5, answer="", claims=[], evidence=evidence,
        proof_tree=None, graph={}, trace={},
    )


def test_apply_ranking_reorders_only_within_candidates():
    d = _mk_diag(["F1", "F2", "F3"], ["S1", "S2"])
    audit = llm_agent._apply_ranking(d, llm_agent._Ranking(
        faults=[RankPick(nodeId="F3"), RankPick(nodeId="F1"), RankPick(nodeId="F2")],
        solutions=[RankPick(nodeId="S2"), RankPick(nodeId="S1")],
        note="test"))
    assert [f["id"] for f in d.faults] == ["F3", "F1", "F2"]
    assert [s["id"] for s in d.solutions] == ["S2", "S1"]
    assert audit["driver"] == "llm" and audit["rejected"] == []


def test_apply_ranking_rejects_unknown_ids_and_keeps_all_candidates():
    d = _mk_diag(["F1", "F2"], ["S1", "S2"])
    audit = llm_agent._apply_ranking(d, llm_agent._Ranking(
        faults=[RankPick(nodeId="FAKE"), RankPick(nodeId="F2")],
        solutions=[RankPick(nodeId="S2")],
        note=""))
    assert audit["rejected"] == ["FAKE"]
    # 重排不是删候选：LLM 没提到的 F1 / S1 仍按原序保留在末尾
    assert [f["id"] for f in d.faults] == ["F2", "F1"]
    assert [s["id"] for s in d.solutions] == ["S2", "S1"]


def test_apply_ranking_locks_safety_solution():
    d = _mk_diag(["F1"], ["停用并送修", "清洁滤网"], policies=["safety_stop"],
                 warnings=["电池鼓包请立即停用"])
    audit = llm_agent._apply_ranking(d, llm_agent._Ranking(
        faults=[], solutions=[RankPick(nodeId="清洁滤网"), RankPick(nodeId="停用并送修")],
        note=""))
    assert d.solutions[0]["id"] == "停用并送修", "安全方案必须锁在首位"
    assert audit["locked"] is True


def test_apply_ranking_no_ranking_keeps_rule_order():
    d = _mk_diag(["F1", "F2"], ["S1", "S2"])
    audit = llm_agent._apply_ranking(d, None)
    assert audit["driver"] == "rule"
    assert [f["id"] for f in d.faults] == ["F1", "F2"]


def _reverse_rank_handler(messages):
    text = messages[-1]["content"]
    if "在候选【内部】排序" not in text:
        return _happy_handler(messages)
    raw = text.split("<candidates>", 1)[1].split("</candidates>", 1)[0]
    cands = json.loads(raw)
    return {
        "faults": [{"nodeId": c["nodeId"], "evidenceIds": [], "reason": "test"}
                   for c in reversed(cands["faults"])],
        "solutions": [{"nodeId": c["nodeId"], "evidenceIds": [], "reason": "test"}
                      for c in reversed(cands["solutions"])],
        "note": "reverse",
    }


def test_run_llm_applies_llm_ranking(store, model, monkeypatch):
    """重排结果必须进入 diagnosis，否则下游指标仍然看不见 LLM 的选择。"""
    q = f"{model} 报 E11，左轮不转"
    baseline = graph.run(store, question=q).diagnosis

    monkeypatch.setattr(llm_agent, "get_client", lambda: _FakeClient(_reverse_rank_handler))
    run = llm_agent.run_llm(store, question=q)

    rank_steps = [t for t in run.trace if t["node"] == "rank"]
    assert rank_steps and rank_steps[0]["driver"] == "llm"
    if len(baseline.faults) >= 2:
        assert run.diagnosis.faults[0]["id"] == baseline.faults[-1]["id"]
        assert run.diagnosis.faults[-1]["id"] == baseline.faults[0]["id"]
    if len(baseline.solutions) >= 2:
        assert run.diagnosis.solutions[0]["id"] == baseline.solutions[-1]["id"]


def test_run_llm_skips_ranking_when_ablation_flag_set(store, model, monkeypatch):
    """KG_NO_RANK=1 只关重排：rank 步仍在 trace 里，但 driver 必须是 rule。

    这样「重排贡献了多少」才能用同一份代码、同一批题做 A/B，而不是靠版本对比。
    """
    q = f"{model} 报 E11，左轮不转"
    baseline = graph.run(store, question=q).diagnosis

    monkeypatch.setenv("KG_NO_RANK", "1")
    monkeypatch.setattr(llm_agent, "get_client", lambda: _FakeClient(_reverse_rank_handler))
    run = llm_agent.run_llm(store, question=q)

    rank_steps = [t for t in run.trace if t["node"] == "rank"]
    assert rank_steps and rank_steps[0]["driver"] == "rule"
    assert [f["id"] for f in run.diagnosis.faults] == [f["id"] for f in baseline.faults]
    assert [s["id"] for s in run.diagnosis.solutions] == [s["id"] for s in baseline.solutions]
