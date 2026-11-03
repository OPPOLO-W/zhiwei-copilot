"""图存储抽象。

两个后端实现同一接口：
  * Neo4jStore  —— 唯一事实源（生产/演示）
  * MemoryStore —— 从 data/curated 加载，用于单元测试、离线演示、CI

原则（文档 2.4）：不让两个后端双写。MemoryStore 只读，数据来自 Neo4j 导入所用的
同一份 curated/jsonl，因此不会产生一致性分叉。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from .model import Edge, Graph, Node
from .ontology import Ontology


@dataclass
class Subgraph:
    nodes: list[Node] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    truncated: bool = False

    def to_payload(self, onto: Ontology) -> dict:
        """转成前端契约（文档 10.4）。"""
        return {
            "nodes": [
                {
                    "id": n.id,
                    "label": n.label,
                    "name": n.name,
                    "props": n.props,
                    "color": onto.classes[n.label].viz.get("color") if n.label in onto.classes else "#64748b",
                    "shape": onto.classes[n.label].viz.get("shape") if n.label in onto.classes else "round-rectangle",
                }
                for n in self.nodes
            ],
            "edges": [
                {
                    "id": e.id,
                    "source": e.head,
                    "target": e.tail,
                    "type": e.type,
                    "confidence": e.confidence,
                    "props": e.props,
                }
                for e in self.edges
            ],
            "meta": {"truncated": self.truncated, "nodeCount": len(self.nodes), "edgeCount": len(self.edges)},
        }


class GraphStore(ABC):
    backend: str = "abstract"

    @abstractmethod
    def get(self, node_id: str) -> Node | None: ...

    @abstractmethod
    def nodes(self, label: str | None = None, limit: int | None = None) -> list[Node]: ...

    @abstractmethod
    def edges(self, rel_type: str | None = None, limit: int | None = None) -> list[Edge]: ...

    @abstractmethod
    def neighbors(
        self,
        node_id: str,
        rel_types: list[str] | None = None,
        direction: str = "both",
        limit: int = 200,
    ) -> list[tuple[Edge, Node]]: ...

    @abstractmethod
    def search(self, text: str, label: str | None = None, limit: int = 10) -> list[Node]: ...

    @abstractmethod
    def stats(self) -> dict: ...

    @abstractmethod
    def full_graph(self, node_limit: int = 5000, edge_limit: int = 20000) -> Graph: ...

    @abstractmethod
    def health(self) -> dict: ...

    def close(self) -> None:  # pragma: no cover - 可选
        return None

    # ---- 通用组合操作（子类无需重写）----
    def subgraph(self, node_ids: list[str], depth: int = 1, rel_types: list[str] | None = None,
                 limit: int = 80) -> Subgraph:
        seen: set[str] = set()
        out_edges: dict[tuple[str, str, str], Edge] = {}
        frontier = [nid for nid in node_ids if self.get(nid)]
        seen.update(frontier)
        truncated = False
        for _ in range(max(0, depth)):
            nxt: list[str] = []
            for nid in frontier:
                for edge, other in self.neighbors(nid, rel_types, limit=limit):
                    out_edges[edge.key] = edge
                    if other.id not in seen:
                        if len(seen) >= limit:
                            truncated = True
                            continue
                        seen.add(other.id)
                        nxt.append(other.id)
            frontier = nxt
            if not frontier:
                break
        nodes = [n for n in (self.get(nid) for nid in seen) if n is not None]
        return Subgraph(nodes=nodes, edges=list(out_edges.values()), truncated=truncated)

    def find_paths(self, start: str, end: str, max_hops: int = 4, limit: int = 10) -> list[list[Edge]]:
        """有界 BFS 枚举路径；返回边序列（节点可从边推导）。"""
        if not self.get(start) or not self.get(end):
            return []
        paths: list[list[Edge]] = []
        queue: list[tuple[str, list[Edge], set[str]]] = [(start, [], {start})]
        while queue and len(paths) < limit:
            current, trail, visited = queue.pop(0)
            if len(trail) >= max_hops:
                continue
            for edge, other in self.neighbors(current, limit=200):
                if other.id in visited:
                    continue
                new_trail = [*trail, edge]
                if other.id == end:
                    paths.append(new_trail)
                    if len(paths) >= limit:
                        break
                else:
                    queue.append((other.id, new_trail, visited | {other.id}))
        return paths
