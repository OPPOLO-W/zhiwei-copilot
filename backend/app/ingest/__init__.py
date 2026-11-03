"""语料摄取层：原始文档 → 带定位锚点的规范化语料。

对外只暴露三件事：
    collect()   扫描 documents/ 下的 PDF 与 FAQ Markdown
    readers     单文档读取（read_pdf / read_markdown）
    corpus      Chu​nk / Table / Doc 数据结构与 jsonl 读写
"""

from .corpus import Chunk, Doc, Table, clean_cell, clean_text, read_jsonl, write_jsonl

__all__ = ["Chunk", "Doc", "Table", "clean_cell", "clean_text", "read_jsonl", "write_jsonl"]
