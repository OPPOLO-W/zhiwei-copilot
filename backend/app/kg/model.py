"""与存储无关的图数据模型。

Neo4j 与内存后端共享同一套 Node/Edge，保证「一份数据、两种后端、同一行为」。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator

# 所有节点统一携带的通用标签，便于统一约束与计数
ROOT_LABEL = "Entity"


@dataclass(frozen=True, slots=True)
class Node:
    id: str
    label: str                      # 主标签，如 ProductModel
    props: dict[str, Any] = field(default_factory=dict)
    extra_labels: tuple[str, ...] = ()   # 多标签，如 Case 的 FAQCase

    @property
    def labels(self) -> tuple[str, ...]:
        return (ROOT_LABEL, self.label, *self.extra_labels)

    @property
    def name(self) -> str:
        return str(self.props.get("name") or self.props.get("title") or self.id)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "label": self.label, "labels": list(self.labels), "props": self.props}


@dataclass(frozen=True, slots=True)
class Edge:
    type: str
    head: str
    tail: str
    props: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.head, self.type, self.tail)

    @property
    def id(self) -> str:
        return f"{self.head}|{self.type}|{self.tail}"

    @property
    def confidence(self) -> float:
        try:
            return float(self.props.get("confidence", 0.0))
        except (TypeError, ValueError):
            return 0.0

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "head": self.head, "tail": self.tail, "props": self.props}


class Graph:
    """可变的图容器，用于构建种子数据与内存后端。"""

    def __init__(self) -> None:
        self._nodes: dict[str, Node] = {}
        self._edges: dict[tuple[str, str, str], Edge] = {}

    # ---- 写 ----
    def add_node(
        self,
        node_id: str,
        label: str,
        props: dict[str, Any] | None = None,
        extra_labels: Iterable[str] = (),
    ) -> Node:
        props = dict(props or {})
        props.setdefault("id", node_id)
        old = self._nodes.get(node_id)
        if old is not None:
            merged = {**old.props, **props}
            extra = tuple(sorted({*old.extra_labels, *extra_labels}))
            node = Node(node_id, label or old.label, merged, extra)
        else:
            node = Node(node_id, label, props, tuple(sorted(set(extra_labels))))
        self._nodes[node_id] = node
        return node

    def add_edge(self, rel_type: str, head: str, tail: str, props: dict[str, Any] | None = None) -> Edge:
        if head == tail and rel_type != "SIMILAR_TO":
            raise ValueError(f"禁止自环: {head} -[{rel_type}]-> {tail}")
        edge = Edge(rel_type, head, tail, dict(props or {}))
        old = self._edges.get(edge.key)
        if old is not None:  # 幂等：合并属性，保留更高置信度
            merged = {**old.props, **edge.props}
            if old.confidence > edge.confidence:
                merged["confidence"] = old.confidence
            edge = Edge(rel_type, head, tail, merged)
        self._edges[edge.key] = edge
        return edge

    def merge(self, other: "Graph") -> None:
        for node in other._nodes.values():
            self.add_node(node.id, node.label, node.props, node.extra_labels)
        for edge in other._edges.values():
            self.add_edge(edge.type, edge.head, edge.tail, edge.props)

    # ---- 读 ----
    def node(self, node_id: str) -> Node | None:
        return self._nodes.get(node_id)

    def has_node(self, node_id: str) -> bool:
        return node_id in self._nodes

    def nodes(self, label: str | None = None) -> list[Node]:
        if label is None:
            return list(self._nodes.values())
        if label == ROOT_LABEL:
            return list(self._nodes.values())
        return [n for n in self._nodes.values() if label in n.labels]

    def edges(self, rel_type: str | None = None) -> list[Edge]:
        if rel_type is None:
            return list(self._edges.values())
        return [e for e in self._edges.values() if e.type == rel_type]

    def out_edges(self, node_id: str, types: set[str] | None = None) -> list[Edge]:
        return [
            e for e in self._edges.values()
            if e.head == node_id and (types is None or e.type in types)
        ]

    def in_edges(self, node_id: str, types: set[str] | None = None) -> list[Edge]:
        return [
            e for e in self._edges.values()
            if e.tail == node_id and (types is None or e.type in types)
        ]

    def __len__(self) -> int:
        return len(self._nodes)

    def __iter__(self) -> Iterator[Node]:
        return iter(self._nodes.values())

    # ---- 序列化 ----
    def write_jsonl(self, directory: Path) -> tuple[Path, Path]:
        directory.mkdir(parents=True, exist_ok=True)
        nodes_path = directory / "nodes.jsonl"
        edges_path = directory / "edges.jsonl"
        with nodes_path.open("w", encoding="utf-8", newline="\n") as f:
            for node in sorted(self._nodes.values(), key=lambda n: n.id):
                f.write(json.dumps(node.to_dict(), ensure_ascii=False, default=str) + "\n")
        with edges_path.open("w", encoding="utf-8", newline="\n") as f:
            for edge in sorted(self._edges.values(), key=lambda e: e.key):
                f.write(json.dumps(edge.to_dict(), ensure_ascii=False, default=str) + "\n")
        return nodes_path, edges_path

    @classmethod
    def read_jsonl(cls, directory: Path) -> "Graph":
        graph = cls()
        nodes_path, edges_path = directory / "nodes.jsonl", directory / "edges.jsonl"
        if not nodes_path.exists() or not edges_path.exists():
            raise FileNotFoundError(
                f"未找到 {nodes_path} 或 {edges_path}，请先运行: python -m backend.app.kg.build_seed"
            )
        with nodes_path.open(encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                row = json.loads(line)
                labels = [lab for lab in row.get("labels", []) if lab not in (ROOT_LABEL, row["label"])]
                graph.add_node(row["id"], row["label"], row.get("props", {}), labels)
        with edges_path.open(encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                row = json.loads(line)
                graph.add_edge(row["type"], row["head"], row["tail"], row.get("props", {}))
        return graph
