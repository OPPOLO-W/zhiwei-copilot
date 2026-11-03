"""内存图后端：从 data/curated 的 jsonl 加载，供测试与离线演示。"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from ..config import CURATED_DIR
from .model import Edge, Graph, Node
from .store import GraphStore


class MemoryStore(GraphStore):
    backend = "memory"

    def __init__(self, graph: Graph) -> None:
        self._g = graph
        self._out: dict[str, list[tuple[Edge, Node]]] = {}
        self._in: dict[str, list[tuple[Edge, Node]]] = {}
        self._index()

    def _index(self) -> None:
        for edge in self._g.edges():
            head, tail = self._g.node(edge.head), self._g.node(edge.tail)
            if head is None or tail is None:
                continue
            self._out.setdefault(head.id, []).append((edge, tail))
            self._in.setdefault(tail.id, []).append((edge, head))

    @classmethod
    def from_dir(cls, directory: Path | None = None) -> "MemoryStore":
        return cls(Graph.read_jsonl(directory or CURATED_DIR))

    def get(self, node_id: str) -> Node | None:
        return self._g.node(node_id)

    def nodes(self, label: str | None = None, limit: int | None = None) -> list[Node]:
        items = self._g.nodes(label)
        return items[:limit] if limit else items

    def edges(self, rel_type: str | None = None, limit: int | None = None) -> list[Edge]:
        items = self._g.edges(rel_type)
        return items[:limit] if limit else items

    def neighbors(
        self,
        node_id: str,
        rel_types: list[str] | None = None,
        direction: str = "both",
        limit: int = 200,
    ) -> list[tuple[Edge, Node]]:
        # 容错：单个关系名也接受字符串，避免 set("HAS_CODE") 被拆成字符集
        types = ({rel_types} if isinstance(rel_types, str) else set(rel_types)) if rel_types else None
        out: list[tuple[Edge, Node]] = []
        if direction in ("out", "both"):
            out.extend(self._out.get(node_id, []))
        if direction in ("in", "both"):
            out.extend(self._in.get(node_id, []))
        if types:
            out = [(e, n) for e, n in out if e.type in types]
        return out[:limit]

    def search(self, text: str, label: str | None = None, limit: int = 10) -> list[Node]:
        q = text.strip().lower()
        if not q:
            return []
        scored: list[tuple[float, Node]] = []
        for node in self.nodes(label):
            score = 0.0
            name = str(node.props.get("name") or node.props.get("title") or "").lower()
            code = str(node.props.get("code") or "").lower()
            model = str(node.props.get("modelCode") or "").lower()
            aliases = str(node.props.get("aliasesText") or "").lower()
            if node.id.lower() == q or code == q or model == q:
                score = 100
            elif name == q:
                score = 90
            elif q in name:
                score = 70
            elif aliases and q in aliases:
                score = 60
            elif code and code in q:
                score = 55
            elif q in node.id.lower():
                score = 40
            if score:
                scored.append((score, node))
        scored.sort(key=lambda x: (-x[0], x[1].id))
        return [n for _, n in scored[:limit]]

    def stats(self) -> dict:
        by_label: dict[str, int] = {}
        for node in self._g.nodes():
            by_label[node.label] = by_label.get(node.label, 0) + 1
        by_rel: dict[str, int] = {}
        for edge in self._g.edges():
            by_rel[edge.type] = by_rel.get(edge.type, 0) + 1
        return {
            "backend": self.backend,
            "entities": len(self._g.nodes()),
            "relations": len(self._g.edges()),
            "byLabel": dict(sorted(by_label.items(), key=lambda kv: -kv[1])),
            "byRelation": dict(sorted(by_rel.items(), key=lambda kv: -kv[1])),
        }

    def full_graph(self, node_limit: int = 5000, edge_limit: int = 20000) -> Graph:
        return self._g

    def health(self) -> dict:
        return {"ok": True, "backend": self.backend,
                "entities": len(self._g.nodes()), "relations": len(self._g.edges())}


def load_graph(directory: Path | None = None) -> Graph:
    return Graph.read_jsonl(directory or CURATED_DIR)


def iter_nodes(nodes: Iterable[Node], label: str) -> list[Node]:
    return [n for n in nodes if label in n.labels]
