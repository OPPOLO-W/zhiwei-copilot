"""把人工审核通过的官方 FAQ 案例写入图谱。

数据流
------
    data/extracted/candidates.jsonl   抽取候选（机器产出，含逐字原文与 URL）
          ↓  人工逐条定论
    data/review/faq_cases.yaml        审核台账（accepted / rejected + 理由）
          ↓  本模块
    data/curated/{nodes,edges}.jsonl  最终图谱

为什么抽取结果不直接进图
------------------------
58 条官方 FAQ 里只有 22 条是「现象 → 处置」的案例卡，其余是操作教程。
教程已经作为 Document 节点存在，再复制成 Case 只会让「案例」这个概念失效。
判定与理由写在台账里，可追溯；本模块只负责把 accepted 的那部分变成节点和边，
不做任何语义判断——判断发生在人审那一步，代码不该替人「补」对齐。

证据等级
--------
节点的 rawText / resolution 是官网原文逐字，四条边（ABOUT / OBSERVED /
RESOLVED_BY / DOCUMENTED_IN）的 evidence 也都从原文截取，因此统一标
`evidenceMode=verbatim`、`verification=verified`，curator 记为 human_review。
概念对齐的可靠程度由 confidence 表达，不靠 evidenceMode 承担。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from ..config import DATA_DIR
from .build_seed import SeedBuilder, SeedError

REVIEW_PATH = DATA_DIR / "review" / "faq_cases.yaml"
CANDIDATES_PATH = DATA_DIR / "extracted" / "candidates.jsonl"

PROV = {
    "dataProvenance": "official_faq",
    "needsReview": False,
    "curator": "human_review",
}


def load_review(path: Path | None = None) -> list[dict[str, Any]]:
    path = path or REVIEW_PATH
    if not path.exists():
        return []
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return [c for c in (doc.get("cases") or []) if (c.get("decision") or "") == "accepted"]


def load_candidates(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """按 caseNo 索引抽取候选，取原文与 URL（台账里不重复存大段正文）。"""
    path = path or CANDIDATES_PATH
    if not path.exists():
        raise SeedError(f"缺少抽取候选：{path}；先跑 python -m backend.app.extract.pipeline")
    out: dict[str, dict[str, Any]] = {}
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            d = json.loads(line)
            if d.get("kind") == "node" and d.get("label") == "FAQCase":
                out[d["key"]] = d["props"]
    return out


def apply_reviewed_cases(
    builder: SeedBuilder,
    review_path: Path | None = None,
    candidates_path: Path | None = None,
) -> dict[str, int]:
    """把 accepted 案例写成节点与边；返回统计，便于构建日志核对。"""
    cases = load_review(review_path)
    stats = {"FAQCase": 0, "ABOUT": 0, "OBSERVED": 0, "RESOLVED_BY": 0, "DOCUMENTED_IN": 0}
    if not cases:
        return stats

    cand = load_candidates(candidates_path)
    model_code = (yaml.safe_load(REVIEW_PATH.read_text(encoding="utf-8")) or {}).get(
        "meta", {}
    ).get("product")
    model_id = next(
        (m["id"] for m in builder.seed.get("models", []) if m.get("modelCode") == model_code),
        None,
    )
    if model_id is None:
        raise SeedError(f"审核台账声明机型 {model_code!r}，但种子中没有该型号")

    for c in cases:
        case_no = c["caseNo"]
        src = cand.get(case_no)
        if src is None:
            raise SeedError(f"{case_no} 在抽取候选中不存在，台账与候选不同步")
        doc_id = c.get("docId") or src["docId"]
        if not builder.graph.has_node(doc_id):
            raise SeedError(f"{case_no} 引用的文档 {doc_id} 不在图谱中")
        cid = f"CASE:{case_no}"

        builder.add_node("FAQCase", cid, {
            "caseNo": case_no,
            "sourceType": "official_faq",
            "title": src["title"],
            "rawText": src.get("rawText", ""),
            "resolution": src.get("resolution", ""),
            "docId": doc_id,
            "url": src.get("url", ""),
            "stepCount": int(src.get("stepCount") or 0),
            "kind": c.get("kind") or "troubleshoot",
            **PROV,
        })
        stats["FAQCase"] += 1

        title = src["title"]
        # 案例针对哪个型号：FAQ 页归属 T20 PRO，属原文直接信息
        builder.rel("ABOUT", cid, model_id, title, doc_id, 1.0,
                    evidence_mode="verbatim", verification="verified", **PROV)
        stats["ABOUT"] += 1

        # 案例卡本身就是该文档的结构化抽取，直接关联回原文
        builder.rel("DOCUMENTED_IN", cid, doc_id, title, doc_id, 1.0,
                    evidence_mode="verbatim", verification="verified", **PROV)
        stats["DOCUMENTED_IN"] += 1

        if c.get("symptom"):
            builder.rel("OBSERVED", cid, f"SYM:{c['symptom']}", title, doc_id, 0.9,
                        evidence_mode="verbatim", verification="verified",
                        alignment="reviewed", **PROV)
            stats["OBSERVED"] += 1

        if c.get("solution"):
            builder.rel("RESOLVED_BY", cid, f"SOL:{c['solution']}",
                        src.get("resolution", title), doc_id, 0.85,
                        evidence_mode="verbatim", verification="verified",
                        successTrue=0.8, alignment="reviewed", **PROV)
            stats["RESOLVED_BY"] += 1

    return stats
