"""抽取层：把规范化语料转成本体实例候选。

与 ingest 层的分工：
    ingest  只负责「文档长什么样」——切块、保结构、给锚点，不判断语义
    extract 只负责「文本说什么」——映射到本体类与关系，并保留原文证据

两者分开的好处是：换一套本体不必重跑 PDF 解析，换一批文档也不必改抽取规则。
"""

from .candidates import CandidateEdge, CandidateNode, Extraction, NodeRef, summarize

__all__ = ["CandidateEdge", "CandidateNode", "Extraction", "NodeRef", "summarize"]
