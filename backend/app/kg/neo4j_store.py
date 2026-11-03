"""Neo4j 后端（只读语义）。

Agent 与 API 一律通过本类访问图，不直接拼 Cypher；关系类型与标签来自本体白名单。
"""

from __future__ import annotations

from typing import Any

from neo4j import Driver, GraphDatabase

from ..config import get_settings
from .model import Edge, Graph, Node
from .store import GraphStore

ROOT_LABEL = "Entity"


def _to_node(raw: dict[str, Any]) -> Node:
    props = dict(raw.get("props") or {})
    labels = list(raw.get("labels") or [])
    stored = props.pop("_label", None)
    label = stored or next((lab for lab in labels if lab != ROOT_LABEL), ROOT_LABEL)
    extra = tuple(sorted(lab for lab in labels if lab not in (ROOT_LABEL, label)))
    props.pop("id", None)
    return Node(raw.get("id") or props.get("id", ""), label, props, extra)


def _to_edge(raw: dict[str, Any]) -> Edge:
    return Edge(raw["type"], raw["head"], raw["tail"], dict(raw.get("props") or {}))


class Neo4jStore(GraphStore):
    backend = "neo4j"

    def __init__(self, uri: str | None = None, user: str | None = None,
                 password: str | None = None, database: str | None = None) -> None:
        s = get_settings()
        self.database = database or s.neo4j_database
        self._driver: Driver = GraphDatabase.driver(
            uri or s.neo4j_uri, auth=(user or s.neo4j_user, password or s.neo4j_password)
        )
        self._cache: Graph | None = None

    # ---- 底层 ----
    def _run(self, cypher: str, **params: Any) -> list[dict[str, Any]]:
        with self._driver.session(database=self.database) as session:
            return [dict(rec) for rec in session.run(cypher, **params)]

    def health(self) -> dict:
        try:
            self._run("RETURN 1 AS ok")
            counts = self._run(
                "MATCH (n:Entity) WITH count(n) AS e "
                "MATCH ()-[r]->() RETURN e, count(r) AS r"
            )
            row = counts[0] if counts else {"e": 0, "r": 0}
            return {"ok": True, "backend": self.backend, "entities": row["e"], "relations": row["r"]}
        except Exception as exc:  # noqa: BLE001 - 健康检查需要吞掉所有异常并回报
            return {"ok": False, "backend": self.backend, "error": str(exc)}

    def close(self) -> None:
        self._driver.close()

    # ---- 读 ----
    def get(self, node_id: str) -> Node | None:
        rows = self._run(
            "MATCH (n:Entity {id:$id}) RETURN n.id AS id, labels(n) AS labels, properties(n) AS props",
            id=node_id,
        )
        return _to_node(rows[0]) if rows else None

    def nodes(self, label: str | None = None, limit: int | None = None) -> list[Node]:
        cypher = "MATCH (n:Entity) "
        params: dict[str, Any] = {}
        if label and label != ROOT_LABEL:
            cypher += "WHERE $label IN labels(n) "
            params["label"] = label
        cypher += "RETURN n.id AS id, labels(n) AS labels, properties(n) AS props ORDER BY n.id"
        if limit:
            cypher += " LIMIT $limit"
            params["limit"] = int(limit)
        return [_to_node(r) for r in self._run(cypher, **params)]

    def edges(self, rel_type: str | None = None, limit: int | None = None) -> list[Edge]:
        cypher = (
            "MATCH (a:Entity)-[r]->(b:Entity) "
            + ("WHERE type(r) = $type " if rel_type else "")
            + "RETURN type(r) AS type, a.id AS head, b.id AS tail, properties(r) AS props"
        )
        params: dict[str, Any] = {"type": rel_type} if rel_type else {}
        if limit:
            cypher += " LIMIT $limit"
            params["limit"] = int(limit)
        return [_to_edge(r) for r in self._run(cypher, **params)]

    def neighbors(
        self,
        node_id: str,
        rel_types: list[str] | None = None,
        direction: str = "both",
        limit: int = 200,
    ) -> list[tuple[Edge, Node]]:
        # 容错：单个关系名也接受字符串，Cypher 的 IN 需要列表
        if isinstance(rel_types, str):
            rel_types = [rel_types]
        # 不带前导 AND：下面模板已经写了 WHERE，带了会拼成 `WHERE AND ...` 的语法错误
        rel_filter = "type(r) IN $types" if rel_types else "true"
        dir_filter = {
            "out": "AND startNode(r).id = $id ",
            "in": "AND endNode(r).id = $id ",
            "both": "",
        }.get(direction, "")
        cypher = f"""
        MATCH (n:Entity {{id:$id}})-[r]-(m:Entity)
        WHERE {rel_filter or 'true'} {dir_filter}
        RETURN type(r) AS type, startNode(r).id AS head, endNode(r).id AS tail,
               properties(r) AS rprops, m.id AS id, labels(m) AS labels, properties(m) AS props
        LIMIT $limit
        """
        params: dict[str, Any] = {"id": node_id, "limit": int(limit)}
        if rel_types:
            params["types"] = rel_types
        out: list[tuple[Edge, Node]] = []
        for row in self._run(cypher, **params):
            node = _to_node({"id": row["id"], "labels": row["labels"], "props": row["props"]})
            out.append((_to_edge({"type": row["type"], "head": row["head"], "tail": row["tail"],
                                  "props": row["rprops"]}), node))
        return out

    def search(self, text: str, label: str | None = None, limit: int = 10) -> list[Node]:
        cypher = """
        MATCH (n:Entity)
        WHERE ($label IS NULL OR $label IN labels(n))
          AND (
            n.id = $exact
            OR toLower(coalesce(n.name,'')) CONTAINS $q
            OR toLower(coalesce(n.title,'')) CONTAINS $q
            OR toLower(coalesce(n.aliasesText,'')) CONTAINS $q
            OR toLower(coalesce(n.code,'')) = $q
            OR toLower(coalesce(n.modelCode,'')) = $q
          )
        RETURN n.id AS id, labels(n) AS labels, properties(n) AS props
        LIMIT $limit
        """
        rows = self._run(cypher, q=text.strip().lower(), exact=text.strip(), label=label,
                         limit=int(limit))
        return [_to_node(r) for r in rows]

    def stats(self) -> dict:
        by_label = {
            row["label"]: row["c"]
            for row in self._run(
                "MATCH (n:Entity) "
                "RETURN coalesce(n._label, head([l IN labels(n) WHERE l <> 'Entity'])) AS label, "
                "count(*) AS c ORDER BY c DESC"
            )
        }
        by_rel = {
            row["t"]: row["c"]
            for row in self._run("MATCH ()-[r]->() RETURN type(r) AS t, count(*) AS c ORDER BY c DESC")
        }
        return {
            "backend": self.backend,
            "entities": sum(by_label.values()),
            "relations": sum(by_rel.values()),
            "byLabel": by_label,
            "byRelation": by_rel,
        }

    def full_graph(self, node_limit: int = 5000, edge_limit: int = 20000) -> Graph:
        if self._cache is not None and len(self._cache) <= node_limit:
            return self._cache
        graph = Graph()
        for row in self._run(
            "MATCH (n:Entity) RETURN n.id AS id, labels(n) AS labels, properties(n) AS props LIMIT $limit",
            limit=int(node_limit),
        ):
            node = _to_node(row)
            graph.add_node(node.id, node.label, node.props, node.extra_labels)
        for row in self._run(
            "MATCH (a:Entity)-[r]->(b:Entity) RETURN type(r) AS type, a.id AS head, b.id AS tail, "
            "properties(r) AS props LIMIT $limit",
            limit=int(edge_limit),
        ):
            edge = _to_edge(row)
            if graph.has_node(edge.head) and graph.has_node(edge.tail):
                graph.add_edge(edge.type, edge.head, edge.tail, edge.props)
        self._cache = graph
        return graph

    def invalidate(self) -> None:
        self._cache = None
