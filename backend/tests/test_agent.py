"""Agent 层：槽位抽取、编排、覆盖度与降级路径。"""

from __future__ import annotations

import pytest

from backend.app.agent import graph as agent_graph
from backend.app.agent.slots import extract_slots, sub_questions


def test_slot_extraction_of_error_codes(store, model, other_model):
    s1 = extract_slots(store, f"{model} 报 E11，左轮不转")
    assert (s1.model, s1.code, s1.symptom) == (model, "E11", "左轮不转")

    s2 = extract_slots(store, "T20 PRO 报 10010 配网一直失败")
    assert s2.code == "10010"
    assert s2.intent == "network"

    # 中文紧邻字母时 \b 会失效，这里回归测试 lookaround 写法
    s3 = extract_slots(store, f"{other_model}的E42")
    assert s3.code == "E42"


def test_safety_and_replace_intent(store, model):
    s = extract_slots(store, f"{model} 电池鼓包，我能自己拆机换电池吗")
    assert s.safety_hint is True
    assert s.replace_intent is True
    assert s.intent == "safety"


def test_sub_questions_cover_five_aspects(store, model):
    s = extract_slots(store, f"{model} 报 E11")
    subs = sub_questions(s)
    assert [x["id"] for x in subs] == ["q1", "q2", "q3", "q4", "q5"]
    for item in subs:
        assert item["requiredEvidence"] and item["toolHint"]


def test_agent_run_produces_tool_trace_and_coverage(store, model):
    run = agent_graph.run(store, question=f"{model} 报 E11，左轮不转")
    assert run.status == "ok"
    assert run.diagnosis.faults
    assert run.coverage["q1"] == "covered"
    nodes = [t["node"] for t in run.trace]
    assert nodes[0] == "intake" and nodes[-1] == "finalize"
    assert any(t.get("tool") == "run_rules" for t in run.trace)


def test_agent_dedupes_repeated_tool_calls(store, model):
    run = agent_graph.run(store, question=f"{model} 报 E11，左轮不转")
    keys = [(t["tool"], str(t["args"])) for t in run.tool_results]
    # 允许同一工具换参数重试（扩大时间窗），但不允许完全相同参数的重复调用
    assert len(keys) == len(set(keys))


def test_agent_reports_case_context(store, first_case_no):
    run = agent_graph.run(store, case_no=first_case_no)
    assert run.status == "ok"
    assert run.diagnosis.faults


def test_agent_out_of_scope(store):
    run = agent_graph.run(store, question="今天天气怎么样")
    assert run.status == "out_of_scope"
    assert "T 系列" in run.answer
    assert run.tool_results == []


def test_agent_degrades_on_cross_model_code(store, other_model):
    """型号存在、但该错误码不适用于它时应降级。

    这需要至少两个型号才能构造（A 有该码、B 没有）。当前语料只含 T20 PRO，
    等接入第二个型号后本用例自动生效。
    """
    from backend.tests.conftest import current_models

    if len(current_models(store)) < 2:
        pytest.skip("需要多机型语料才能构造「型号存在但码不适用」")
    run = agent_graph.run(store, question=f"{other_model} 报 E42 水路异常")
    assert run.status == "degraded"
    assert run.extra["reason"] in ("code_not_applicable", "unknown_model")
    # 落空时不应再浪费工具调用
    assert len(run.tool_results) == 1


# ---------------------------------------------------------------------------
# 消融开关（文档 9.3 的 D / E 组）
# ---------------------------------------------------------------------------
def test_ablation_without_reflection_skips_reflect_node(store, model):
    """D 组：关掉反思后不应出现 reflect 节点，但结论链必须保持完整。"""
    q = f"{model} 报 E11，左轮不转"
    full = agent_graph.run(store, question=q)
    ablated = agent_graph.run(store, question=q, use_reflection=False)

    assert any(step["node"] == "reflect" for step in full.trace) or not full.reflections
    assert ablated.reflections == []
    assert not any(step["node"] == "reflect" for step in ablated.trace)
    # 兜底计划本身就能拿到规则结论，所以关掉反思不应影响首要故障
    assert ablated.diagnosis.faults[0]["id"] == full.diagnosis.faults[0]["id"]


def test_ablation_without_rules_uses_graph_chain(store, model):
    """E 组：换成纯图检索链，工具名与证据链都应变化。"""
    q = f"{model} 报 E11，左轮不转"
    full = agent_graph.run(store, question=q)
    ablated = agent_graph.run(store, question=q, use_rules=False)

    assert full.tool_results[0]["tool"] == "run_rules"
    assert ablated.tool_results[0]["tool"] == "retrieve_chain"
    # 没有规则就没有规则派生的策略与安全告警
    assert ablated.diagnosis.policies == []
    assert ablated.diagnosis.proof_tree is None
    assert ablated.diagnosis.evidence, "无规则版本仍须给出可追溯证据"


def test_ablation_without_rules_drops_safety_policy(store, model):
    """E 组在安全题上的退化：不再注入 safety_stop。"""
    q = "电池鼓包了还能继续用吗"
    full = agent_graph.run(store, question=q)
    ablated = agent_graph.run(store, question=q, use_rules=False)

    assert "safety_stop" in full.diagnosis.policies
    assert "safety_stop" not in ablated.diagnosis.policies
    assert "⚠️" not in ablated.answer


# ---------------------------------------------------------------------------
# 真实官方 FAQ 案例（保养/操作类，无故障症状）
# ---------------------------------------------------------------------------
def _case_without_symptom(store) -> str:
    for n in store.nodes("FAQCase"):
        if n.props.get("sourceType") != "official_faq":
            continue
        if not store.neighbors(n.id, ["OBSERVED"], "out", 1):
            return n.props["caseNo"]
    return ""


def test_agent_case_detail_without_symptom(store):
    """保养类案例卡没有故障症状，仍须给出原文与处置，而不是「请补充型号」。

    这是接入真实官方 FAQ 后暴露的降级缺陷：案例里没有 OBSERVED 边时，
    事实层曾直接抛「无法确定症状」，把可回答的问题答成输入不足。
    """
    case_no = _case_without_symptom(store)
    if not case_no:
        pytest.skip("当前图谱没有无观察症状的官方 FAQ 案例")

    run = agent_graph.run(store, case_no=case_no)
    assert run.status == "ok"
    assert "case_detail" in run.diagnosis.policies
    assert run.diagnosis.faults == []
    assert run.diagnosis.solutions, "保养卡应有对应处置动作"
    # 答案必须落到原文与出处，否则用户拿不到可执行信息
    assert case_no in run.answer
    assert "DOC:FAQ-" in run.answer
    assert run.diagnosis.evidence, "案例详情也要有可追溯证据"


def test_official_faq_cases_are_verbatim(store):
    """人工审核通过的官方 FAQ 案例，其边必须是逐字证据且已核对。"""
    official = [n for n in store.nodes("FAQCase") if n.props.get("sourceType") == "official_faq"]
    if not official:
        pytest.skip("当前图谱没有官方 FAQ 案例")

    for node in official:
        assert node.props.get("docId"), f"{node.id} 缺少原文 docId"
        for rel in ("ABOUT", "DOCUMENTED_IN"):
            edges = store.neighbors(node.id, [rel], "out", 5)
            assert edges, f"{node.id} 缺少 {rel}"
            for edge, _ in edges:
                assert edge.props.get("evidenceMode") == "verbatim"
                assert edge.props.get("verification") == "verified"
