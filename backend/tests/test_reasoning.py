"""推理正确性：四类典型场景 + 三类查询落空。"""

from __future__ import annotations

import pytest

from backend.app.reasoning.service import diagnose
from backend.tests.conftest import current_models


def _names(items):
    return [i["name"] for i in items]


def test_e11_left_wheel_scenario(store, model):
    d = diagnose(store, model=model, code="E11", symptom="左轮不转")
    assert d.faults and d.faults[0]["name"] == "左驱动轮卡滞"
    assert d.faults[0]["confidence"] > 0.5
    assert d.causes and "缠绕" in d.causes[0]["name"]
    assert d.checks, "应给出检查项"
    assert d.solutions
    # 证明链路完整：症状 → 故障 → 根因 → 方案
    assert d.proof_tree and d.graph["nodes"]


def test_network_code_prefers_no_parts(store):
    d = diagnose(store, model="T20 PRO", code="10010")
    assert "network_first" in d.policies
    assert any("2.4GHz" in n for n in _names(d.solutions))
    # 网络类问题不应推荐更换主板等备件
    for sol in d.solutions[:3]:
        assert not any(i["type"] == "SparePart" for i in sol["items"])


def test_safety_scenario_blocks_self_service(store, model):
    d = diagnose(store, model=model, symptom="电池鼓包")
    assert "safety_stop" in d.policies
    assert any("停止使用" in w for w in d.warnings)
    assert d.solutions[0]["name"].startswith("立即停用")


def test_water_scenario_has_ordered_checks(store, model):
    d = diagnose(store, model=model, code="E42")
    assert d.faults
    assert d.checks, "水路问题应给出检查顺序"
    assert [c["rank"] for c in d.checks] == sorted(c["rank"] for c in d.checks)


def test_solution_ranking_separates_confidence_from_rank(store, model):
    d = diagnose(store, model=model, code="E11")
    assert all(0.0 <= f["confidence"] <= 1.0 for f in d.faults)
    assert all(f["rankScore"] >= f["confidence"] - 1e-6 for f in d.faults)


def test_unknown_model_is_degraded_not_crash(store, model):
    """问一个语料里不存在的型号，应给出可解释结论并指回可用型号，而不是异常。

    单机型语料下无法构造「型号存在但该码不适用」，这类场景由
    test_agent_degrades_on_cross_model_code 在编排层覆盖。
    """
    d = diagnose(store, model="NOT-A-MODEL", code="E42")
    assert d.policies[0] in ("unknown_model", "code_not_applicable")
    assert model in d.answer
    assert not d.faults


def test_unknown_code_is_degraded(store, model):
    d = diagnose(store, model=model, code="E99")
    assert "unknown_code" in d.policies
    assert "E99" in d.answer


def test_cross_model_comparison_adds_per_model_meaning(store, model):
    models = current_models(store)
    if len(models) < 2:
        pytest.skip("当前语料只有单一型号，跨型号比较无意义")
    a, b = models[0], models[1]
    d = diagnose(store, model=a, code="E11", question=f"E11 在 {a} 和 {b} 上含义一样吗？")
    assert "cross_model_compare" in d.policies
    assert a in d.answer and b in d.answer


def test_no_signal_returns_guidance(store):
    d = diagnose(store, question="帮我推荐一台吸尘器")
    assert "unknown_intent" in d.policies
    assert "T 系列" in d.answer


def test_evidence_links_to_every_edge(store, model):
    d = diagnose(store, model=model, code="E11")
    assert d.evidence
    assert all(e["triple"] and len(e["triple"]) == 3 for e in d.evidence)
    # 每条边都要能回指到一条证据，前端悬停联动依赖这个约束
    assert all(edge.get("evidenceId") for edge in d.graph["edges"])


def test_faithfulness_of_claims_is_high(store, model):
    d = diagnose(store, model=model, code="E11")
    supported = [c for c in d.claims if c["evidenceIds"]]
    assert len(supported) / len(d.claims) > 0.7


@pytest.mark.parametrize("code", ["E11", "E42", "10010"])
def test_diagnose_is_deterministic(store, model, code):
    a = diagnose(store, model=model, code=code).to_dict()
    b = diagnose(store, model=model, code=code).to_dict()
    assert a["answer"] == b["answer"]
    assert [f["id"] for f in a["faults"]] == [f["id"] for f in b["faults"]]


# ---------------- P2：用户自述事实（actions）进入推理 ----------------
def test_action_excludes_cause_and_reranks_solution(store, model):
    """用户说「毛发已经清理干净」，应排除缠绕根因，方案首位换成更换轮组。"""
    base = diagnose(store, model=model, code="E11")
    d = diagnose(store, model=model, code="E11", actions=["CLEAN-WHEEL"])
    assert "轮轴毛发或线材缠绕" in _names(base.causes)
    assert "轮轴毛发或线材缠绕" not in _names(d.causes)
    assert d.solutions[0]["name"] == "更换轮组总成"
    assert d.solutions[0]["name"] != base.solutions[0]["name"]


def test_two_actions_narrow_to_escalation(store, model):
    """清理 + 换新都做过，只剩电机故障，方案应直接转官方售后。"""
    d = diagnose(store, model=model, code="E11", actions=["CLEAN-WHEEL", "REPLACE-WHEEL"])
    assert _names(d.causes) == ["驱动电机故障"]
    assert d.solutions[0]["name"] == "转官方售后检测"


def test_exhausted_solution_is_dropped(store, model):
    """清洁触点已试过仍充不上：清洁方案不再作为候选，更换触点上升。"""
    d = diagnose(store, model=model, code="E23", actions=["CLEAN-CONTACT"])
    names = _names(d.solutions)
    assert "清洁充电触点" not in names
    assert names[0] == "更换充电触点组件"
    assert [c["name"] for c in d.causes] == ["充电触点氧化"]


def test_excluded_cause_removes_its_fault(store, model):
    """清水箱重装过 → 排除「水箱未卡到位」，只用于该根因的故障也应退出候选。"""
    d = diagnose(store, model=model, code="E33", actions=["RESEAT-WATER-TANK"])
    assert "清水箱缺水" not in _names(d.faults)
    assert "水箱未卡到位" not in _names(d.causes)
    assert d.solutions[0]["name"] == "水泵排气"


def test_negative_check_excludes_cause(store, model):
    """检查结果阴性（轴承间隙正常）应排除轴承磨损根因（R26）。"""
    d = diagnose(
        store,
        model=model,
        code="E11",
        check_results=[{"check": "CHECK-WHEEL-BEARING", "fault": "LEFT-WHEEL-STUCK", "result": "negative"}],
    )
    assert "轮组轴承磨损" not in _names(d.causes)


def test_negative_check_also_removes_its_check(store, model):
    """排除根因后，只为它服务的检查项不能继续排第一（否则追问等于没问）。"""
    d = diagnose(store, model=model, code="E11",
                 check_results=[{"check": "CHK:SPIN-LEFT-WHEEL",
                                 "fault": "FAULT:LEFT-WHEEL-STUCK", "result": "negative"}])
    assert _names(d.causes)[0] == "轮组轴承磨损"
    assert d.checks[0]["name"] == "检查轮组轴承间隙"
    assert "手动转动左轮" not in [c["name"] for c in d.checks]
