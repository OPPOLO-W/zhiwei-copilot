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
from backend.app.llm.prompts import FactDraft, RankPick, SlotExtraction
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


def test_evidence_summary_carries_ids_evidence_and_docs(store):
    """P3.0：摘要必须带 nodeId / docId / 证据原文，否则工具无从取参。"""
    d = _mk_diag(["FAULT:LEFT-WHEEL-STUCK"], ["SOL:REPLACE-WHEEL"])
    d.faults[0]["confidence"] = 0.82
    d.evidence[0] = {
        "id": "E1", "source": "DOC:T20PRO-MANUAL",
        "text": "左轮不转提示左驱动轮卡滞：机器人清扫中停住，APP 提示左轮异常",
    }
    text = llm_agent._evidence_summary(d, [], [], store)
    assert "FAULT:LEFT-WHEEL-STUCK" in text
    assert "SOL:REPLACE-WHEEL" in text
    assert "DOC:T20PRO-MANUAL" in text
    assert "E1" in text
    assert "左轮不转提示" in text


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
    """P5.1 起排序并入 finalize：从同一份 prompt 的 <candidates> 里读候选再反转序。"""
    text = messages[-1]["content"]
    if "<candidates>" not in text:
        return _happy_handler(messages)
    raw = text.split("<candidates>", 1)[1].split("</candidates>", 1)[0]
    cands = json.loads(raw)
    out = dict(_happy_handler(messages))
    out["faults"] = [{"nodeId": c["nodeId"], "evidenceIds": [], "reason": "test"}
                     for c in reversed(cands["faults"])]
    out["solutions"] = [{"nodeId": c["nodeId"], "evidenceIds": [], "reason": "test"}
                        for c in reversed(cands["solutions"])]
    out["note"] = "reverse"
    return out


def test_apply_ranking_refuses_to_promote_escalation():
    """升级/停用是安全边界，不是排序偏好：规则没要求升级时，LLM 不能自己升级。"""
    d = _mk_diag(["F1"], ["更换轮组总成", "转官方售后检测"])
    audit = llm_agent._apply_ranking(d, llm_agent._Ranking(
        faults=[], solutions=[RankPick(nodeId="转官方售后检测"),
                              RankPick(nodeId="更换轮组总成")], note=""))
    assert [s["id"] for s in d.solutions] == ["更换轮组总成", "转官方售后检测"]
    assert audit["locked"] is True
    assert "escalation" in audit["locks"]


def test_run_llm_applies_llm_ranking(store, model, monkeypatch):
    """重排结果必须进入 diagnosis，否则下游指标仍然看不见 LLM 的选择。"""
    # 用不含升级类方案的题：E33 的候选是 重新安装水箱/水泵排气/水路除垢
    q = f"{model} 报 E33，清水箱缺水"
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


# ---------------------------------------------------------------------------
# P2.1：用户自述事实的门控
# ---------------------------------------------------------------------------
def test_gate_facts_keeps_only_verbatim_action(store):
    """quote 必须是用户原话子串：模型改成「轮组」就核对不上，宁可不采信。"""
    q = "T20 PRO 报 E11，左轮的毛发我已经清理干净了，还是卡"
    good = FactDraft(kind="action", ref="CLEAN-WHEEL", quote="左轮的毛发我已经清理干净了")
    bad = FactDraft(kind="action", ref="CLEAN-WHEEL", quote="轮组的毛发我已经清理干净了")
    accepted, rejected = llm_agent._gate_facts(store, q, [good, bad])
    assert [f["nodeId"] for f in accepted] == ["SOL:CLEAN-WHEEL"]
    assert accepted[0]["effect"] == "excludes"
    assert [r["reason"] for r in rejected] == ["quote_not_verbatim"]


def test_gate_facts_rejects_negated_action_and_unknown_entity(store):
    q = "T20 PRO 报 E11，毛发的清理还没做，轮组也没换"
    drafts = [
        FactDraft(kind="action", ref="CLEAN-WHEEL", quote="毛发的清理还没做"),
        FactDraft(kind="action", ref="CLEAN-MOON", quote="轮组也没换"),
    ]
    accepted, rejected = llm_agent._gate_facts(store, q, drafts)
    assert accepted == []
    assert {r["reason"] for r in rejected} == {"negated_action", "unknown_entity"}


def test_gate_facts_rejects_check_covering_multiple_faults(store):
    """轴承检查同时覆盖左右轮：跨侧误排除比不排除更糟，直接不采信。"""
    q = "T20 PRO 报 E11，我检查了轮组轴承间隙，还是有旷量"
    draft = FactDraft(kind="check", ref="CHECK-WHEEL-BEARING", quote="检查了轮组轴承间隙",
                      result="positive")
    accepted, rejected = llm_agent._gate_facts(store, q, [draft])
    assert accepted == []
    assert rejected[0]["reason"] == "check_covers_multiple_faults"


def test_run_llm_applies_user_action_facts(store, model, monkeypatch):
    """端到端：用户说「毛发清理过了」，首选方案从清理轮组换成更换轮组总成。"""

    def handler(messages):
        if "抽取故障排查槽位" in messages[-1]["content"]:
            return {"model": None, "code": None, "symptom": None, "environment": None,
                    "intent": "troubleshoot", "safety_hint": False, "replace_intent": False,
                    "facts": [{"kind": "action", "ref": "CLEAN-WHEEL",
                               "result": "positive", "quote": "轮组的毛发我已经清理干净了"}],
                    "unresolved": []}
        return _happy_handler(messages)

    q = f"{model} 报 E11，左轮不转。轮组的毛发我已经清理干净了，还是卡"
    baseline = graph.run(store, question=q).diagnosis
    assert baseline.solutions and baseline.solutions[0]["name"] == "清理轮组缠绕物"

    monkeypatch.setattr(llm_agent, "get_client", lambda: _FakeClient(handler))
    run = llm_agent.run_llm(store, question=q)

    facts_steps = [t for t in run.trace if t["node"] == "facts"]
    assert facts_steps and facts_steps[0]["accepted"][0]["nodeId"] == "SOL:CLEAN-WHEEL"
    assert run.diagnosis.solutions[0]["name"] == "更换轮组总成"


def test_run_llm_asks_when_ambiguous(store, model, monkeypatch):
    """E11 三个根因并存：开启追问时应给出一个检查项，且状态是 needs_info。"""
    monkeypatch.setattr(llm_agent, "get_client", lambda: _FakeClient(_happy_handler))
    run = llm_agent.run_llm(store, question=f"{model} 报 E11，左轮不转", allow_ask=True)
    assert run.status == "needs_info"
    ask = run.extra["askUser"]
    assert ask and ask["checkId"].startswith("CHK:")
    assert ask["reason"]["ig"] > 0
    assert [o["value"] for o in ask["options"]] == ["positive", "negative", "skip"]
    assert any(t["node"] == "ask_user" for t in run.trace)


def test_run_llm_does_not_ask_by_default(store, model, monkeypatch):
    """单轮评测必须保持「不问也答」：默认关闭追问，否则历史指标全部失真。"""
    monkeypatch.setattr(llm_agent, "get_client", lambda: _FakeClient(_happy_handler))
    run = llm_agent.run_llm(store, question=f"{model} 报 E11，左轮不转")
    assert run.status == "ok"
    assert run.extra.get("askUser") is None


def test_run_llm_records_per_step_latency(store, model, monkeypatch):
    """每一步都要有 ms，否则「一次查询 10 秒花在哪」只能靠猜。"""
    monkeypatch.setattr(llm_agent, "get_client", lambda: _FakeClient(_happy_handler))
    run = llm_agent.run_llm(store, question=f"{model} 报 E11，左轮不转")
    assert run.trace
    assert all(isinstance(s.get("ms"), int) for s in run.trace), run.trace
    assert run.trace[-1]["ms"] >= 0


def test_run_llm_stops_reflecting_when_budget_exhausted(store, model, monkeypatch):
    """预算见底时必须停探索：宁可少一轮反思，也要把额度留给 finalize。"""
    monkeypatch.setattr(llm_agent, "get_client", lambda: _FakeClient(_happy_handler))
    real = llm_agent.get_settings()

    class _Tight:
        # 0 预算 → 任何已用 token（含 0）都超过 80% 阈值；fake client 不报 usage，
        # 用正数预算反而永远触发不了护栏
        agent_token_budget = 0
        agent_max_steps = real.agent_max_steps
        agent_max_reflections = real.agent_max_reflections

    monkeypatch.setattr(llm_agent, "get_settings", lambda: _Tight)
    run = llm_agent.run_llm(store, question=f"{model} 报 E11，左轮不转")
    assert run.answer, "省调用不能省答案"
    assert not [s for s in run.trace
                if s.get("node") == "reflect" and s.get("driver") == "llm"], run.trace
    assert any(s.get("skipped") == "budget" for s in run.trace)


def test_run_llm_counts_every_llm_phase(store, model, monkeypatch):
    """成本账要覆盖全部阶段：decompose/plan/reflect/finalize 少记一个，
    报告里的「每题几次调用、多少 token」就会系统性偏低。"""
    monkeypatch.setattr(llm_agent, "get_client", lambda: _FakeClient(_happy_handler))
    run = llm_agent.run_llm(store, question=f"{model} 报 E11，左轮不转")
    phases = [s for s in run.trace if s.get("node") in {"act", "plan", "rank", "finalize"}]
    assert phases
    # decompose + plan + 至少一轮 reflect + finalize（P5.1 起排序并入 finalize，不再单独一次）
    assert run.extra["llm"]["calls"] >= 4, run.extra["llm"]
    rank = [s for s in run.trace if s.get("node") == "rank"]
    assert rank and rank[0].get("merged") is True
