"""抽取层测试：证据可回溯、结构映射正确、命名未被排版噪声污染。

这些断言对应 report 里「知识抽取」一节要拿出的证据：
抽出来的每一条都能指回页码与表格行，且映射关系与本体一致。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CAND = ROOT / "data" / "extracted" / "candidates.jsonl"
RAW = ROOT / "data" / "raw"


def _rows() -> list[dict]:
    if not CAND.exists():
        pytest.skip("候选未生成，先运行 python -m backend.app.extract.pipeline")
    return [json.loads(line) for line in CAND.read_text(encoding="utf-8").splitlines() if line.strip()]


def _chunks() -> list[dict]:
    path = RAW / "chunks.jsonl"
    if not path.exists():
        pytest.skip("语料未生成，先运行 python -m backend.app.ingest.build")
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _fault_rows() -> list[dict]:
    return [c for c in _chunks() if c["kind"] == "table_row" and "故障情况" in c["headers"]]


def _cell(text: str, col: str) -> str:
    return text.split(f"{col}: ")[1].split(" | ")[0]


@pytest.fixture(scope="module")
def data() -> tuple[list[dict], list[dict]]:
    rows = _rows()
    return ([r for r in rows if r["kind"] == "node"], [r for r in rows if r["kind"] == "edge"])


def test_every_candidate_has_verbatim_evidence_and_locator(data):
    """每条候选都必须带原文证据与可定位锚点——这是「可追溯」的底线。"""
    nodes, edges = data
    for item in nodes + edges:
        assert item["evidence"].strip(), f"缺少证据：{item.get('key') or item.get('type')}"
        assert item["locator"].strip(), f"缺少定位：{item.get('key') or item.get('type')}"
        assert item["doc_id"], "缺少来源文档"


def test_fault_table_structure_is_preserved(data):
    """故障表的三列关系要完整落成三元组，不能被压平或错配。

    期望值从语料现算（而不是写死条数）：换一份机型手册时，
    这条断言检验的仍是「抽取与表格是否一一对应」，不会因为型号变了就失效。
    """
    nodes, edges = data
    rows = _fault_rows()
    assert rows, "未识别到故障排查表"
    expect_faults = {_cell(c["text"], "故障情况") for c in rows}
    expect_pairs = {
        (_cell(c["text"], "故障情况"), _cell(c["text"], "可能原因")) for c in rows
    }
    expect_solutions = {
        (_cell(c["text"], "可能原因"), _cell(c["text"], "解决方法")) for c in rows
    }

    faults = [n for n in nodes if n["label"] == "Fault"]
    # 部分故障名在归一化上会合并（如尾部句号），只允许少，不允许凭空多
    assert 0 < len(faults) <= len(expect_faults)

    caused = [e for e in edges if e["type"] == "CAUSED_BY"]
    assert len(caused) == len(expect_pairs), (
        f"「故障→原因」应覆盖表格全部 {len(expect_pairs)} 组，实际 {len(caused)}"
    )
    resolved = [e for e in edges if e["type"] == "RESOLVED_BY" and e["head"][0] == "Cause"]
    assert len(resolved) >= len(expect_solutions) * 0.9

    # 每条边两端都必须是真实存在的节点，不能悬空
    fault_names = {n["key"] for n in faults}
    cause_names = {n["key"] for n in nodes if n["label"] == "Cause"}
    for e in caused:
        assert e["head"][1] in fault_names, f"悬空故障：{e['head']}"
        assert e["tail"][1] in cause_names, f"悬空原因：{e['tail']}"


def test_fault_names_are_not_broken_by_wrapping(data):
    """软换行拼合后的实体名不能出现断词。"""
    nodes, _ = data
    names = [n["name"] for n in nodes if n["label"] == "Fault"]
    for bad in ("返、回", "自动、集尘", "跑、偏", "未清扫、完"):
        assert not any(bad in n for n in names), f"实体名被软换行破坏：{bad}"


def test_component_alias_is_canonicalised():
    """同一部件的不同叫法必须归到同一个规范名。

    这是纯单元测试，不依赖语料：手册里「下视传感器」与「下视感应器」并存，
    若不归一，同一实体会被建成两个节点，推理链在这一处分叉。
    """
    from backend.app.extract.extractors import canonical_component

    assert canonical_component("下视传感器") == canonical_component("下视感应器")
    assert canonical_component("拖布") == canonical_component("可水洗拖布")
    assert canonical_component("集尘袋") == canonical_component("尘袋")


def test_component_names_are_canonical(data):
    """部件节点里不能同时出现同一部件的两种写法。"""
    nodes, _ = data
    comps = {n["name"] for n in nodes if n["label"] in ("Component", "Consumable")}
    for a, b in (("下视传感器", "下视感应器"), ("拖布", "可水洗拖布"),
                 ("集尘袋", "尘袋"), ("d-ToF", "d-ToF 激光雷达模块")):
        assert not ({a, b} <= comps), f"同一部件被建成两个节点：{a} / {b}"


def test_every_fault_is_categorized(data):
    """故障必须都归入某个大类，不允许落进「其他」。"""
    nodes, _ = data
    cats = [n["props"].get("category") for n in nodes if n["label"] == "Fault"]
    assert cats and all(c and c != "其他" for c in cats), f"存在未归类故障：{sorted(set(cats))}"


def test_checks_do_not_swallow_following_clause(data):
    """检查项应在句子边界截断，不能把后续处置句吞进来。"""
    nodes, _ = data
    checks = [n["name"] for n in nodes if n["label"] == "Check"]
    assert checks, "未抽出任何检查项"
    for c in checks:
        assert "，" not in c and "," not in c, f"检查项含后续分句：{c}"
        assert len(c) <= 40, f"检查项过长：{c}"


def test_documents_are_linked_to_model(data):
    """手册与型号要连上，否则证据链断在文档层。"""
    _, edges = data
    rels = {e["type"] for e in edges}
    assert "HAS_MANUAL" in rels
    assert "HAS_COMPONENT" in rels
    manual = [e for e in edges if e["type"] == "HAS_MANUAL"]
    assert manual, "至少要把手册挂到型号上"
    assert all(e["head"][0] == "ProductModel" for e in manual)
    assert all(e["tail"][0] == "Document" for e in manual)


def test_series_is_derived_from_model_code(data):
    """型号的系列归属必须由型号名自证，不允许人工拍板或残留「待确认」。

    这条断言防的是「机型挂错系列」——一旦把 X 系列挂到 T 系列，
    后续「同系列部件/耗材是否通用」的推理会全盘出错。
    语料换成任何 T 系列手册时，这条断言都应当照样成立。
    """
    import re

    nodes, edges = data
    models = [n for n in nodes if n["label"] == "ProductModel"]
    assert models
    for model in models:
        code = model["props"]["modelCode"]
        expect = re.match(r"^([A-Za-z]+)\s*\d", code).group(1).upper()
        assert model["props"]["series"] == expect, f"{code} 的系列应为 {expect}"
        # 参数表里登记的认证型号来自原文，不能为空
        assert model["props"]["deviceCodes"], f"{code} 缺少认证型号（产品型号行未抽到）"

    for line in (n for n in nodes if n["label"] == "ProductLine"):
        assert line["props"]["series"]
        assert "系列" in line["name"]
        assert not any("待确认" in str(v) for v in line["props"].values())

    # 每个型号必须恰好挂在一个产品线下
    for model in models:
        links = [e for e in edges if e["type"] == "HAS_MODEL" and e["tail"][1] == model["key"]]
        assert len(links) == 1, f"{model['key']} 的产品线数量应为 1，实际 {len(links)}"
        assert links[0]["head"] == ["ProductLine", f"DEEBOT-{model['props']['series']}"]


# ---------------------------------------------------------------------------
# FAQ 抽取：一问一答 → 症状 / 案例卡 / 有序步骤
# ---------------------------------------------------------------------------


def test_every_faq_becomes_a_symptom_and_case(data):
    """每篇 FAQ 都要落成一个症状入口和一张案例卡。"""
    nodes, edges = data
    docs = _chunks()
    faq_docs = {c["doc_id"] for c in docs if c["doc_type"] == "faq"}
    assert faq_docs, "没有 FAQ 语料"

    symptoms = [n for n in nodes if n["label"] == "Symptom" and n["extractor"] == "faq/symptom"]
    cases = [n for n in nodes if n["label"] == "FAQCase"]
    assert {n["doc_id"] for n in symptoms} == faq_docs, "有 FAQ 没生成症状节点"
    assert len(cases) == len(faq_docs), "案例卡应与 FAQ 一一对应"

    for case in cases:
        assert case["props"]["sourceType"] == "official_faq"
        assert case["props"]["caseNo"] == case["key"]
        assert case["props"]["rawText"], "案例卡必须保留原文"
        links = [e for e in edges if e["type"] == "OBSERVED" and e["head"][1] == case["key"]]
        assert len(links) == 1, f"{case['key']} 应恰好观察到一个症状"


def test_faq_steps_are_ordered_and_traceable(data):
    """步骤必须可执行、可定位，且 PRECEDES 串成一条不回环的链。"""
    nodes, edges = data
    steps = [n for n in nodes if n["label"] == "Solution" and n["extractor"] == "faq/step"]
    assert steps, "未抽到 FAQ 步骤"
    for s in steps:
        assert 0 < s["props"]["stepIndex"] <= 40
        assert len(s["name"]) >= 4
        assert "→" not in s["locator"] or "第" in s["locator"]

    precedes = [e for e in edges if e["type"] == "PRECEDES"]
    assert precedes, "步骤之间没有顺序关系"
    # 同一篇内不能出现环：步号必须严格递增
    idx = {n["key"]: n["props"]["stepIndex"] for n in steps}
    for e in precedes:
        assert idx[e["tail"][1]] > idx[e["head"][1]], f"步骤顺序倒挂：{e['head'][1]} → {e['tail'][1]}"


def test_faq_notes_do_not_become_steps(data):
    """「温馨提示 / 注」里的条件与禁令不能混进操作步骤。

    配网那篇里「路由器不能隐藏 Wi-Fi 信号」是前提条件，
    当成第 N 步会让 Agent 输出一份「照着做也配不上网」的清单。
    """
    nodes, _ = data
    bad = [
        n["name"] for n in nodes
        if n["label"] == "Solution" and n["extractor"] == "faq/step"
        and n["name"].startswith(("注：", "温馨提示", "提示："))
    ]
    assert not bad, f"注释被当成了步骤：{bad[:3]}"


def test_faq_symptoms_align_to_manual_faults(data):
    """FAQ 症状与手册故障的对齐边必须带依据与待核标记。"""
    nodes, edges = data
    aligns = [e for e in edges if e["type"] == "INDICATES"]
    assert aligns, "双源对账没有产出任何对齐关系"
    fault_keys = {n["key"] for n in nodes if n["label"] == "Fault"}
    sym_keys = {n["key"] for n in nodes if n["label"] == "Symptom"}
    for e in aligns:
        assert e["head"][1] in sym_keys and e["tail"][1] in fault_keys
        assert e["props"]["verification"] == "pending", "自动对齐不能冒充已确认事实"
        assert e["props"]["matchMethod"], "缺少对齐依据，无法复核"
        assert 0.6 <= e["confidence"] <= 1.0


def test_alignment_report_is_written():
    """对账报告要落盘，且覆盖率字段可加总回原始条数。"""
    path = ROOT / "data" / "extracted" / "faq_fault_alignment.json"
    if not path.exists():
        pytest.skip("对账报告未生成，先运行 extract.pipeline")
    rep = json.loads(path.read_text(encoding="utf-8"))
    assert rep["manualApplicable"] >= 10
    assert rep["faqTroubleshootCount"] >= 10
    assert 0.0 <= rep["manualCoverage"] <= 1.0
    assert 0.0 <= rep["faqCoverage"] <= 1.0
    assert len(rep["pairs"]) == rep["alignedPairs"]
    assert rep["manualCovered"] + len(rep["manualMissed"]) == rep["manualApplicable"]
    assert rep["faqCovered"] + len(rep["faqMissed"]) == rep["faqTroubleshootCount"]
