"""抽取流水线命令行入口。

用法
----
    python -m backend.app.extract.pipeline            # 抽取并落盘
    python -m backend.app.extract.pipeline --report   # 只看统计

输入：`data/raw/{docs,tables,chunks}.jsonl`（由 ingest 层产出）
输出：`data/extracted/candidates.jsonl`（节点与边混合存放，`kind` 区分）
"""

from __future__ import annotations

import argparse
from pathlib import Path

from .align import alignment_report, print_report, write_report
from .candidates import CandidateEdge, CandidateNode, Extraction, NodeRef, summarize
from .extractors import (
    CONSUMABLE_LABELS,
    components_in,
    extract_component_function_table,
    extract_fault_table,
    extract_maintenance_table,
    extract_param_table,
    series_code,
    verified_components,
)
from .faq import extract_faq_doc
from ..ingest.corpus import read_jsonl

ROOT = Path(__file__).resolve().parents[3]
RAW = ROOT / "data" / "raw"
OUT = ROOT / "data" / "extracted" / "candidates.jsonl"
ALIGN_OUT = ROOT / "data" / "extracted" / "faq_fault_alignment.json"


def _corpus_text(chunks: list[dict]) -> str:
    return "\n".join(c.get("text", "") for c in chunks)


def _group_by_product(docs: list[dict]) -> list[str]:
    """按文档归属的型号去重列出（保持出现顺序）。"""
    seen: list[str] = []
    for d in docs:
        p = (d.get("product") or "").strip()
        if p and p not in seen:
            seen.append(p)
    return seen


def build(product: str | None = None) -> tuple[Extraction, list[dict]]:
    docs = read_jsonl(RAW / "docs.jsonl")
    tables = read_jsonl(RAW / "tables.jsonl")
    chunks = read_jsonl(RAW / "chunks.jsonl")
    if not docs:
        raise SystemExit("缺少 data/raw/docs.jsonl，先运行 python -m backend.app.ingest.build")

    available = _group_by_product(docs)
    if product and product not in available:
        raise SystemExit(
            f"语料里没有型号 {product}（已识别：{'、'.join(available) or '无'}）。"
            "检查文件名是否含「DEEBOT <型号>」，或不传 --product 让流水线自动识别。"
        )
    targets = [product] if product else available
    if not targets:
        raise SystemExit(
            "未能从文件名识别出任何型号。手册请命名为「地宝 DEEBOT T20 PRO.pdf」这类形式，"
            "FAQ 请放在 documents/Questions/地宝 DEEBOT T20 PRO/ 目录下。"
        )

    ext = Extraction()

    # 文档节点与具体型号无关，统一先建（多机型时避免同一文档被重复建节点）
    for d in docs:
        ext.add_node(CandidateNode(
            label="Document", key=d["doc_id"], name=d["title"],
            props={"docType": d["doc_type"], "authority": d["authority"],
                   # FAQ 抓自官网时用原页面链接，PDF 手册才退回落本地文件
                   "url": d.get("url") or f"file://{Path(d['source_path']).as_posix()}",
                   "pageCount": d["page_count"], "sourceDoc": d["doc_id"],
                   "evidenceMode": "verbatim"},
            evidence=d["title"], locator=f"{d['title']} 文档级",
            doc_id=d["doc_id"], confidence=0.99, extractor="manual/struct",
        ))

    for prod in targets:
        ext.extend(_build_product(prod, docs, tables, chunks))

    return ext.dedupe(), docs


def _build_product(product: str, docs: list[dict], tables: list[dict],
                   chunks: list[dict]) -> Extraction:
    """抽取单个型号的知识。

    组件、表格、耗材兼容关系全部**限定在本型号的文档与语料块内**：
    否则 A 机型手册里的部件会被判成 B 机型可用，兼容性推理直接出错。
    """
    ext = Extraction()
    p_docs = [d for d in docs if (d.get("product") or "").strip() == product]
    doc_ids = {d["doc_id"] for d in p_docs}
    p_tables = [t for t in tables if t["doc_id"] in doc_ids]
    p_chunks = [c for c in chunks if c["doc_id"] in doc_ids]
    manual = next((d for d in p_docs if d["doc_type"] == "user_manual"), None)
    manual_id = manual["doc_id"] if manual else ""
    known = verified_components(_corpus_text(p_chunks))

    # 产品线＝系列级（本体定义：共享结构平台与售后体系的一组产品）。
    # 系列代号由型号名推断，并与手册登记的产品型号前缀（如 DDX19 的 DD+X）交叉印证。
    series = series_code(product)
    line_key = f"DEEBOT-{series}" if series else "DEEBOT"
    line_name = f"DEEBOT {series} 系列" if series else "地宝 DEEBOT"
    ext.add_node(CandidateNode(
        label="ProductLine", key=line_key, name=line_name,
        props={**({"series": series} if series else {}),
               "category": "地宝 DEEBOT",
               "sourceDoc": manual_id,
               "note": "系列由型号名首字母推断，并由手册登记的认证型号前缀交叉印证"},
        evidence=f"地宝 DEEBOT {product}", locator="手册封面",
        doc_id=manual_id, confidence=0.85, extractor="manual/struct",
    ))
    ext.add_edge(CandidateEdge(
        type="HAS_MODEL", head=NodeRef("ProductLine", line_key),
        tail=NodeRef("ProductModel", product),
        evidence=f"地宝 DEEBOT {product}", locator="手册封面",
        doc_id=manual_id, confidence=0.9, extractor="manual/struct",
    ))

    for d in p_docs:
        if d["doc_type"] != "user_manual":
            continue
        ext.add_edge(CandidateEdge(
            type="HAS_MANUAL", head=NodeRef("ProductModel", product),
            tail=NodeRef("Document", d["doc_id"]),
            evidence=d["title"], locator="文档级",
            doc_id=d["doc_id"], confidence=0.99, extractor="manual/struct",
        ))

    # 表格按表头分派到对应适配器（只看本型号的文档）
    for table in p_tables:
        headers = table.get("headers") or []
        doc_title = next((d["title"] for d in p_docs if d["doc_id"] == table["doc_id"]), "")
        if "故障情况" in headers:
            ext.extend(extract_fault_table(table, doc_title, product, known))
        elif "维护频率" in headers:
            ext.extend(extract_maintenance_table(table, doc_title, product, known))
        elif "功能描述" in headers:
            ext.extend(extract_component_function_table(table, doc_title, product, known))
        elif "产品型号" in headers:
            ext.extend(extract_param_table(table, doc_title, product))

    # FAQ：一问一答 → 症状 / 案例卡 / 有序步骤链。
    # 依赖手册故障表先抽出来——FAQ 标题要跟这些故障名做对齐（双源对账）。
    fault_names = sorted({n.key for n in ext.nodes if n.label == "Fault"})
    by_doc: dict[str, list[dict]] = {}
    for c in p_chunks:
        by_doc.setdefault(c["doc_id"], []).append(c)
    for d in p_docs:
        if d["doc_type"] != "faq":
            continue
        ext.extend(extract_faq_doc(
            d, by_doc.get(d["doc_id"], []), product, fault_names, known
        ))

    # 耗材/备件与型号的兼容关系：只对本型号语料中出现过的组件建立
    for canon, label, _hit in known:
        if label in CONSUMABLE_LABELS or label == "SparePart":
            ext.add_edge(CandidateEdge(
                type="COMPATIBLE_WITH", head=NodeRef(label, canon),
                tail=NodeRef("ProductModel", product),
                props={"evidenceMode": "inferred",
                       "note": f"组件在 {product} 手册中出现，据此判定为该型号可用"},
                evidence=f"{canon} 在 {product} 手册中作为该型号部件出现",
                locator="手册部件章节", doc_id=manual_id,
                confidence=0.7, extractor="table/maintenance",
            ))

    return ext


def main() -> None:
    ap = argparse.ArgumentParser(description="从 data/raw 抽取本体实例候选")
    ap.add_argument("--product", default=None,
                    help="只抽某个型号；默认自动识别语料中出现的全部型号")
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--align-out", default=str(ALIGN_OUT))
    ap.add_argument("--report", action="store_true", help="只打印统计，不写盘")
    args = ap.parse_args()

    ext, docs = build(args.product)
    stats = summarize(ext)

    picked = [args.product] if args.product else _group_by_product(docs)
    print(f"产品      {'、'.join(picked) or '（未识别）'}")
    print(f"文档      {len(docs)}")
    print(f"节点      {stats['nodes']}   边 {stats['edges']}")
    print(f"证据覆盖  {stats['evidenceCoverage'] * 100:.1f}%   带定位 {stats['withLocator']}")
    print("\n按类：")
    for k, v in stats["byLabel"].items():
        print(f"  {k:<20} {v}")
    print("\n按关系：")
    for k, v in stats["byRelation"].items():
        print(f"  {k:<22} {v}")
    print("\n按抽取器：")
    for k, v in stats["byExtractor"].items():
        print(f"  {k:<22} {v}")

    for prod in picked:
        rep = alignment_report(ext, prod)
        print_report(rep)
        if not args.report:
            write_report(rep, Path(args.align_out))

    if not args.report:
        path = Path(args.out)
        n, m = ext.write(path)
        print(f"\n写出 {n} 节点 / {m} 边 → {path}")
        print(f"对账报告 → {args.align_out}")


if __name__ == "__main__":
    main()
