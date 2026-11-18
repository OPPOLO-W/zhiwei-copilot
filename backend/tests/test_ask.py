"""追问决策（P2.3）：信息增益、歧义门与「问过就不再问」。

这些是纯函数，不需要 LLM：追问是否发生的判据必须可单测，
否则线上「该问却没问 / 不该问却问」只能靠人工看 trace 发现。
"""

from __future__ import annotations

from types import SimpleNamespace

from backend.app.agent.ask import (ambiguity, information_gain, plan_question,
                                   posterior, prior_causes, should_ask)
from backend.app.kg import get_store


def _diag(causes, checks, policies=()):
    return SimpleNamespace(causes=causes, checks=checks, policies=list(policies))


def _cause(cid, conf):
    return {"id": cid, "name": cid, "confidence": conf, "faultId": f"FAULT:{cid}"}


def test_posterior_moves_mass_towards_confirmed_cause():
    prior = [("A", 0.6), ("B", 0.4)]
    pos = dict(posterior(prior, "A", True))
    neg = dict(posterior(prior, "A", False))
    assert pos["A"] > 0.6 and neg["A"] < 0.6
    assert abs(sum(pos.values()) - 1.0) < 1e-9, "后验必须重新归一化"


def test_information_gain_is_larger_for_balanced_prior():
    """先验越接近均匀，一次检查能消除的不确定性越多。"""
    balanced = information_gain([("A", 0.5), ("B", 0.5)], "A")
    skewed = information_gain([("A", 0.95), ("B", 0.05)], "A")
    assert balanced > skewed


def test_prior_is_normalised_and_sorted_by_confidence():
    d = _diag([_cause("A", 0.2), _cause("B", 0.7)], [])
    prior = prior_causes(d)
    assert prior[0][0] == "B"
    assert abs(sum(p for _, p in prior) - 1.0) < 1e-9


def test_ambiguity_requires_low_top_or_small_margin():
    clear = _diag([_cause("A", 0.9), _cause("B", 0.05)], [])
    close = _diag([_cause("A", 0.5), _cause("B", 0.45)], [])
    low = _diag([_cause("A", 0.6), _cause("B", 0.1)], [])
    assert ambiguity(clear)[0] is False
    assert ambiguity(close)[0] is True
    assert ambiguity(low)[0] is True, "首选置信度本身不够高也要追问"


def test_should_ask_respects_policy_and_known_facts():
    d = _diag([_cause("A", 0.5), _cause("B", 0.45)], [{"id": "CHK:X"}])
    assert should_ask(d) == (True, "ambiguous")
    assert should_ask(d, facts=[{"kind": "action", "nodeId": "SOL:CLEAN-WHEEL"}])[0] is False
    d_safety = _diag([_cause("A", 0.5), _cause("B", 0.45)], [{"id": "CHK:X"}],
                     policies=["safety_stop"])
    assert should_ask(d_safety)[0] is False, "安全红线先给结论，不能拖成问答"


def test_plan_question_skips_already_asked_check():
    store = get_store("memory")
    plan = plan_question(store, _diag(
        [{"id": "CAUSE:HAIR-WRAP-WHEEL", "name": "毛发", "confidence": 0.6,
          "faultId": "FAULT:LEFT-WHEEL-STUCK"},
         {"id": "CAUSE:BEARING-WEAR", "name": "轴承", "confidence": 0.5,
          "faultId": "FAULT:LEFT-WHEEL-STUCK"}],
        [{"id": "CHK:SPIN-LEFT-WHEEL", "name": "手动转动左轮", "riskLevel": "low",
          "durationMin": 2},
         {"id": "CHK:CHECK-WHEEL-BEARING", "name": "检查轮组轴承间隙", "riskLevel": "low",
          "durationMin": 5}],
    ))
    assert plan and plan["checkId"] == "CHK:SPIN-LEFT-WHEEL"
    again = plan_question(store, _diag(
        [{"id": "CAUSE:HAIR-WRAP-WHEEL", "name": "毛发", "confidence": 0.6,
          "faultId": "FAULT:LEFT-WHEEL-STUCK"},
         {"id": "CAUSE:BEARING-WEAR", "name": "轴承", "confidence": 0.5,
          "faultId": "FAULT:LEFT-WHEEL-STUCK"}],
        [{"id": "CHK:SPIN-LEFT-WHEEL", "name": "手动转动左轮", "riskLevel": "low",
          "durationMin": 2},
         {"id": "CHK:CHECK-WHEEL-BEARING", "name": "检查轮组轴承间隙", "riskLevel": "low",
          "durationMin": 5}],
    ), asked=[plan["checkId"]])
    assert again is None or again["checkId"] != plan["checkId"], "同一检查不能反复问"
