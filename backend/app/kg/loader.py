"""把 data/curated 的 jsonl 幂等导入 Neo4j Desktop。

用法（先在 Neo4j Desktop 启动 DBMS，并把 Bolt URL/密码写进 .env）：
    python -m backend.app.kg.loader --reset      # 清空后全量导入
    python -m backend.app.kg.loader             # 增量 MERGE（不删除）

安全约定：
  * 标签与关系类型只来自本体白名单，不接受任意字符串拼接；
  * 导入使用独立写连接，API/Agent 使用只读语义的 Neo4jStore。
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path

from neo4j import GraphDatabase

from ..config import CURATED_DIR, ROOT, get_settings
from .model import Graph, Node
from .ontology import get_ontology

SCHEMA_PATH = ROOT / "backend" / "app" / "kg" / "schema.cypher"
BATCH = 200


def split_statements(text: str) -> list[str]:
    out: list[str] = []
    for chunk in text.split(";"):
        lines = [ln for ln in chunk.splitlines() if not ln.strip().startswith("//")]
        stmt = "\n".join(lines).strip()
        if stmt:
            out.append(stmt)
    return out


def ensure_schema(session) -> list[str]:
    applied = []
    for stmt in split_statements(SCHEMA_PATH.read_text(encoding="utf-8")):
        session.run(stmt).consume()
        applied.append(stmt.splitlines()[0][:80])
    return applied


def clear_graph(session, batch: int = 5000) -> int:
    total = 0
    while True:
        rec = session.run(
            "MATCH (n:Entity) WITH n LIMIT $batch DETACH DELETE n RETURN count(n) AS c",
            batch=batch,
        ).single()
        deleted = rec["c"] if rec else 0
        total += deleted
        if deleted < batch:
            break
    return total


def upsert_nodes(session, graph: Graph) -> int:
    groups: dict[tuple[str, ...], list[dict]] = defaultdict(list)
    for node in graph.nodes():
        labels = tuple(lab for lab in node.labels[1:] if lab != node.label)  # 额外标签
        groups[(node.label, *labels)].append({
            "id": node.id,
            "props": {**node.props, "id": node.id, "_label": node.label},
        })
    count = 0
    for (primary, *extra), rows in groups.items():
        label_clause = ":".join([primary, *extra])
        cypher = (
            f"UNWIND $rows AS row MERGE (n:Entity {{id: row.id}}) SET n:{label_clause} "
            "SET n = row.props"
        )
        for i in range(0, len(rows), BATCH):
            session.run(cypher, rows=rows[i:i + BATCH]).consume()
            count += len(rows[i:i + BATCH])
    return count


def upsert_edges(session, graph: Graph) -> int:
    groups: dict[str, list[dict]] = defaultdict(list)
    for edge in graph.edges():
        groups[edge.type].append({"head": edge.head, "tail": edge.tail, "props": edge.props})
    count = 0
    for rel_type, rows in groups.items():
        cypher = (
            "UNWIND $rows AS row "
            "MATCH (h:Entity {id: row.head}) MATCH (t:Entity {id: row.tail}) "
            f"MERGE (h)-[r:{rel_type}]->(t) SET r = row.props"
        )
        for i in range(0, len(rows), BATCH):
            session.run(cypher, rows=rows[i:i + BATCH]).consume()
            count += len(rows[i:i + BATCH])
    return count


def load(data_dir: Path | None = None, reset: bool = False) -> dict:
    onto = get_ontology()
    graph = Graph.read_jsonl(data_dir or CURATED_DIR)
    # 导入前再验一次白名单，防止手工改过的 jsonl 混入非法类型
    bad_labels = {n.label for n in graph.nodes()} - set(onto.classes)
    bad_rels = {e.type for e in graph.edges()} - set(onto.relations)
    if bad_labels or bad_rels:
        raise SystemExit(f"数据包含本体外类型: labels={bad_labels}, relations={bad_rels}")

    s = get_settings()
    driver = GraphDatabase.driver(s.neo4j_uri, auth=(s.neo4j_user, s.neo4j_password))
    try:
        driver.verify_connectivity()
        with driver.session(database=s.neo4j_database) as session:
            applied = ensure_schema(session)
            deleted = clear_graph(session) if reset else 0
            n_nodes = upsert_nodes(session, graph)
            n_edges = upsert_edges(session, graph)
            rec = session.run(
                "MATCH (n:Entity) WITH count(n) AS e MATCH ()-[r]->() RETURN e, count(r) AS r"
            ).single()
        return {
            "uri": s.neo4j_uri,
            "database": s.neo4j_database,
            "constraintsApplied": len(applied),
            "deleted": deleted,
            "nodesUpserted": n_nodes,
            "edgesUpserted": n_edges,
            "dbEntities": rec["e"] if rec else 0,
            "dbRelations": rec["r"] if rec else 0,
        }
    finally:
        driver.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="导入图谱到 Neo4j")
    parser.add_argument("--dir", type=Path, default=CURATED_DIR)
    parser.add_argument("--reset", action="store_true", help="导入前清空所有 :Entity 节点")
    args = parser.parse_args()
    try:
        result = load(args.dir, reset=args.reset)
    except Exception as exc:  # noqa: BLE001 - CLI 需要给出可操作提示
        print(f"[失败] {type(exc).__name__}: {exc}")
        print("请检查：1) Neo4j Desktop 中 DBMS 已启动；2) .env 的 NEO4J_URI/PASSWORD 正确；"
              "3) Desktop 分配的不是 7687 而是其它端口。")
        raise SystemExit(1) from exc
    print("\n导入完成")
    for k, v in result.items():
        print(f"  {k:>18}: {v}")
    if result["dbEntities"] != result["nodesUpserted"] or result["dbRelations"] != result["edgesUpserted"]:
        print("\n[提示] 数据库计数与导入计数不一致，可能存在残留数据；如需干净重建请加 --reset")


if __name__ == "__main__":
    main()
