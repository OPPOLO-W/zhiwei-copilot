"""摄取层命令行入口。

用法
----
    python -m backend.app.ingest.build                # 全量重建 data/raw
    python -m backend.app.ingest.build --stats        # 只打印统计

产物
----
    data/raw/docs.jsonl     文档元数据
    data/raw/tables.jsonl   表格（保留行列对齐语义）
    data/raw/chunks.jsonl   可检索语料块（带页码/表格行锚点）
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

from .corpus import Doc, read_jsonl, write_jsonl
from .readers import read_markdown, read_pdf

ROOT = Path(__file__).resolve().parents[3]
DOCUMENTS = ROOT / "documents"
OUT = ROOT / "data" / "raw"

# 型号识别不依赖白名单——白名单一旦漏登记，新机型就会静默变成「未知产品」。
# 规则：取文件名里 DEEBOT 之后的型号串，允许跟一个或多个大写后缀。
#   地宝 DEEBOT T20 PRO.pdf       → T20 PRO
#   地宝 DEEBOT T20 PRO 上下水版    → T20 PRO
#   DEEBOT T90S PRO 超薄零嵌款.pdf → T90S PRO
#   T80S MAX上下水款.pdf           → T80S MAX
_MODEL_RE = re.compile(
    r"DEEBOT[\s_\-]*"
    r"((?:[A-Z]{1,2}\d{1,3}[A-Z0-9]*)"
    r"(?:\s+(?:PRO|MAX|PLUS|ULTRA|OMNI|COMBO|TURBO|AI|MINI|COMPLETE))*)"
)

# 没有 DEEBOT 前缀时的兜底（部分 FAQ 目录直接叫「T80S MAX上下水款」）
_MODEL_FALLBACK = re.compile(
    r"(?<![A-Z0-9])"
    r"((?:[A-Z]{1,2}\d{1,3}[A-Z0-9]*)"
    r"(?:\s+(?:PRO|MAX|PLUS|ULTRA|OMNI|COMBO|TURBO|AI|MINI|COMPLETE))*)"
)

# 极少数文件名与规范型号不一致时在此纠正（识别结果 → 规范名）
PRODUCT_ALIASES: dict[str, str] = {}


def _short_hash(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()[:6].upper()


def _infer_product(name: str) -> str:
    upper = name.upper()
    m = _MODEL_RE.search(upper) or _MODEL_FALLBACK.search(upper)
    if not m:
        return ""
    raw = re.sub(r"\s+", " ", m.group(1)).strip()
    return PRODUCT_ALIASES.get(raw, raw)


def _manual_id(path: Path) -> str:
    product = _infer_product(path.stem)
    tag = re.sub(r"[^A-Z0-9]+", "", (product or path.stem).upper())[:10]
    return f"DOC:{tag}-MANUAL"


MANIFEST = ROOT / "data" / "curated" / "faq_manifest.json"
_FAQ_IDS: dict[str, dict] | None = None


def _faq_index() -> dict[str, dict]:
    """FAQ 文件名 → 官网条目（id / url）。

    官网抓取脚本会留下 `faq_manifest.json`，用它把 doc_id 钉在官网问题上
    （`DOC:FAQ-15908`），证据链就能一路点回原页面；没有清单时才退回文件名哈希。
    """
    global _FAQ_IDS
    if _FAQ_IDS is None:
        _FAQ_IDS = {}
        if MANIFEST.exists():
            try:
                data = json.loads(MANIFEST.read_text(encoding="utf-8"))
                for it in data.get("items", []):
                    if it.get("file") and it.get("id"):
                        _FAQ_IDS[it["file"]] = it
            except Exception:  # noqa: BLE001 —— 清单坏了不该阻断摄取
                _FAQ_IDS = {}
    return _FAQ_IDS


def _faq_meta(path: Path) -> tuple[str, str]:
    """返回 (doc_id, 官网链接)。"""
    hit = _faq_index().get(path.name)
    if hit:
        return f"DOC:FAQ-{hit['id']}", str(hit.get("url") or "")
    return f"DOC:FAQ-{_short_hash(path.stem)}", ""


def collect() -> tuple[list[Doc], list, list]:
    docs: list[Doc] = []
    tables: list = []
    chunks: list = []

    manual_dir = DOCUMENTS / "User_Manual"
    for path in sorted(manual_dir.glob("*.pdf")):
        doc, tabs, chks = read_pdf(path, doc_id=_manual_id(path), doc_type="user_manual", authority=5)
        doc.product = _infer_product(path.stem)
        docs.append(doc)
        tables.extend(tabs)
        chunks.extend(chks)

    q_dir = DOCUMENTS / "Questions"
    if q_dir.exists():
        for product_dir in sorted(p for p in q_dir.iterdir() if p.is_dir()):
            product = _infer_product(product_dir.name)
            for path in sorted(product_dir.glob("*.md")):
                doc_id, url = _faq_meta(path)
                doc, tabs, chks = read_markdown(
                    path, doc_id=doc_id, doc_type="faq", authority=4, url=url
                )
                doc.product = product
                docs.append(doc)
                tables.extend(tabs)
                chunks.extend(chks)

    return docs, tables, chunks


def _print_summary(docs: list[Doc], chunks: list, out: Path) -> None:
    """按产品汇总：多机型混放时，先看清单确认型号识别是否正确。"""
    products: dict[str, list[Doc]] = {}
    for d in docs:
        products.setdefault(d.product or "（未识别）", []).append(d)
    print("\n按产品：")
    for name, items in products.items():
        manual = sum(1 for d in items if d.doc_type == "user_manual")
        faq = sum(1 for d in items if d.doc_type == "faq")
        blocks = sum(1 for c in chunks if c.doc_id in {d.doc_id for d in items})
        print(f"  {name:<12} 手册 {manual}  FAQ {faq}  语料块 {blocks}")
    if "（未识别）" in products:
        names = "、".join(d.title for d in products["（未识别）"][:3])
        print(f"\n  ⚠ 以下文档未能识别型号，文件名需含「DEEBOT <型号>」：{names}")

    # 锚点抽样自检：每条都要能读出「哪个文档、第几页/行」
    print("\n锚点抽样：")
    for c in chunks[:2] + chunks[len(chunks) // 2: len(chunks) // 2 + 2]:
        print(f"  [{c.doc_type}] {c.anchor} :: {c.text[:56]}")


def main() -> None:
    ap = argparse.ArgumentParser(description="构建 data/raw 语料")
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--stats", action="store_true", help="只打印现有语料统计")
    ap.add_argument("--dry-run", action="store_true",
                    help="只解析 documents/ 并打印型号识别结果，不覆盖 data/raw")
    args = ap.parse_args()
    out = Path(args.out)

    if args.stats:
        chunks = read_jsonl(out / "chunks.jsonl")
        print(f"chunks: {len(chunks)}")
        return

    docs, tables, chunks = collect()
    if not docs:
        print("documents/ 下没有可用材料。")
        print("  手册：documents/User_Manual/地宝 DEEBOT T20 PRO.pdf")
        print("  FAQ ：documents/Questions/地宝 DEEBOT T20 PRO/*.md")
        return

    if args.dry_run:
        print(f"[预检] 文档 {len(docs)}  表格 {len(tables)}  语料块 {len(chunks)}（未写盘）")
        _print_summary(docs, chunks, out)
        return

    n1 = write_jsonl(out / "docs.jsonl", [d.to_dict() for d in docs])
    n2 = write_jsonl(out / "tables.jsonl", [t.to_dict() for t in tables])
    n3 = write_jsonl(out / "chunks.jsonl", [c.to_dict() for c in chunks])

    print(f"文档   {n1:>5}   ({sum(d.page_count for d in docs)} 页)")
    print(f"表格   {n2:>5}")
    print(f"语料块 {n3:>5}")
    print(f"输出   {out}")

    _print_summary(docs, chunks, out)


if __name__ == "__main__":
    main()
