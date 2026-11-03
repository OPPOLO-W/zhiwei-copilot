"""图谱规模与质量门槛：课程硬指标 + 自建质量闸门。"""

from __future__ import annotations

from pathlib import Path

from backend.app.kg.model import Graph
from backend.app.kg.ontology import get_ontology
from backend.app.kg.validate import validate

ROOT = Path(__file__).resolve().parents[2]


def test_curated_files_exist():
    assert (ROOT / "data/curated/nodes.jsonl").exists()
    assert (ROOT / "data/curated/edges.jsonl").exists()


def test_scale_exceeds_course_requirements(store):
    stats = store.stats()
    assert stats["entities"] >= 60, "课程要求 ≥60 实体"
    assert stats["relations"] >= 120, "课程要求 ≥120 关系"
    # 项目自身质量闸门更严：150 / 300
    assert stats["entities"] >= 150
    assert stats["relations"] >= 300


def test_validation_passes():
    graph = Graph.read_jsonl(ROOT / "data/curated")
    report = validate(graph, get_ontology())
    assert report.ok, report.errors[:5]
    assert report.evidence_coverage >= 0.99


def test_every_edge_has_evidence(store):
    missing = [
        e.id for e in store.edges()
        if not (e.props or {}).get("evidence") or not (e.props or {}).get("sourceId")
    ]
    assert missing == [], f"{len(missing)} 条边缺少证据溯源"


def test_no_orphan_nodes(store):
    orphans = [n.id for n in store.nodes() if not store.neighbors(n.id, limit=1)]
    assert orphans == [], f"存在孤立节点: {orphans[:5]}"


def test_error_codes_are_model_scoped(store):
    """同一错误码必须按型号展开，否则 E11 会在不同型号间串码。"""
    models = {n.props["modelCode"] for n in store.nodes("ProductModel")}
    e11 = [n for n in store.nodes("ErrorCode") if n.props.get("code") == "E11"]
    # 每个型号各有一个 E11 节点，型号之间不共享
    assert len(e11) == len(models)
    assert {n.props["modelCode"] for n in e11} == models
    assert len({n.id for n in e11}) == len(e11)
    assert all(":" in n.id for n in e11)
