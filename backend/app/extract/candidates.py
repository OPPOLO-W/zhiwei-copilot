"""抽取候选：尚未进入图谱的中间产物。

为什么要多一层候选，而不是直接写 nodes/edges：
  1. **可审阅**：抽出来的每条都带原文证据与页码，人能逐条核对后再入库；
  2. **可度量**：与人工标注对比算 P/R，才能证明抽取方法的有效性；
  3. **可回溯**：`evidence` 是原文照抄（verbatim），`locator` 指向页码/表格行，
     任何一条三元组都能被追问「凭什么是这样」。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable


@dataclass(frozen=True)
class NodeRef:
    """节点引用：先用 (类, 业务键) 指代，入库时才解析成最终 id。"""

    label: str
    key: str

    def as_tuple(self) -> tuple[str, str]:
        return (self.label, self.key)


@dataclass
class CandidateNode:
    label: str
    key: str
    name: str
    props: dict[str, Any] = field(default_factory=dict)
    evidence: str = ""
    locator: str = ""
    doc_id: str = ""
    chunk_id: str = ""
    confidence: float = 0.9
    extractor: str = ""
    aliases: list[str] = field(default_factory=list)


@dataclass
class CandidateEdge:
    type: str
    head: NodeRef
    tail: NodeRef
    props: dict[str, Any] = field(default_factory=dict)
    evidence: str = ""
    locator: str = ""
    doc_id: str = ""
    chunk_id: str = ""
    confidence: float = 0.9
    extractor: str = ""


@dataclass
class Extraction:
    """一次抽取的完整结果，便于分阶段统计与落盘。"""

    nodes: list[CandidateNode] = field(default_factory=list)
    edges: list[CandidateEdge] = field(default_factory=list)

    def add_node(self, node: CandidateNode) -> None:
        self.nodes.append(node)

    def add_edge(self, edge: CandidateEdge) -> None:
        self.edges.append(edge)

    def extend(self, other: "Extraction") -> None:
        self.nodes.extend(other.nodes)
        self.edges.extend(other.edges)

    def dedupe(self) -> "Extraction":
        """同 (label, key) 的节点合并，保留首个证据；边按三元组去重。"""
        nodes: dict[tuple[str, str], CandidateNode] = {}
        for n in self.nodes:
            k = (n.label, n.key)
            if k not in nodes:
                nodes[k] = n
            else:
                keep = nodes[k]
                for a in n.aliases:
                    if a not in keep.aliases:
                        keep.aliases.append(a)
                for pk, pv in n.props.items():
                    keep.props.setdefault(pk, pv)

        edges: dict[tuple[str, str, str], CandidateEdge] = {}
        for e in self.edges:
            k = (e.type, e.head.key, e.tail.key)
            if k not in edges:
                edges[k] = e
            else:
                # 同一三元组被多处支持时，保留置信度更高的一条作为代表证据
                if e.confidence > edges[k].confidence:
                    edges[k] = e

        return Extraction(list(nodes.values()), list(edges.values()))

    def write(self, path: Path) -> tuple[int, int]:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="\n") as fh:
            for n in self.nodes:
                fh.write(json.dumps({"kind": "node", **asdict(n)}, ensure_ascii=False, sort_keys=True) + "\n")
            for e in self.edges:
                row = asdict(e)
                row["head"] = list(e.head.as_tuple())
                row["tail"] = list(e.tail.as_tuple())
                fh.write(json.dumps({"kind": "edge", **row}, ensure_ascii=False, sort_keys=True) + "\n")
        return len(self.nodes), len(self.edges)


def read_candidates(path: Path) -> tuple[list[dict], list[dict]]:
    if not path.exists():
        return [], []
    nodes, edges = [], []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            row = json.loads(line)
            (nodes if row.pop("kind") == "node" else edges).append(row)
    return nodes, edges


def summarize(ext: Extraction) -> dict[str, Any]:
    """按类/关系统计，用于抽取质量报告。"""
    by_label: dict[str, int] = {}
    for n in ext.nodes:
        by_label[n.label] = by_label.get(n.label, 0) + 1
    by_rel: dict[str, int] = {}
    for e in ext.edges:
        by_rel[e.type] = by_rel.get(e.type, 0) + 1
    extractors: dict[str, int] = {}
    for item in list(ext.nodes) + list(ext.edges):  # type: ignore[list-item]
        extractors[item.extractor] = extractors.get(item.extractor, 0) + 1
    with_evidence = sum(1 for i in list(ext.nodes) + list(ext.edges) if i.evidence)  # type: ignore[list-item]
    total = len(ext.nodes) + len(ext.edges)
    return {
        "nodes": len(ext.nodes),
        "edges": len(ext.edges),
        "byLabel": dict(sorted(by_label.items(), key=lambda kv: -kv[1])),
        "byRelation": dict(sorted(by_rel.items(), key=lambda kv: -kv[1])),
        "byExtractor": dict(sorted(extractors.items(), key=lambda kv: -kv[1])),
        "evidenceCoverage": round(with_evidence / total, 4) if total else 0.0,
        "withLocator": sum(1 for i in list(ext.nodes) + list(ext.edges) if i.locator),  # type: ignore[list-item]
    }


def iter_evidence(items: Iterable[Any]) -> Iterable[str]:
    for it in items:
        yield getattr(it, "evidence", "")
