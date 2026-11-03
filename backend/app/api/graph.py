"""图谱、本体与规则集查询接口。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from ..kg.ontology import get_ontology
from ..kg.store import GraphStore
from ..rules.loader import load_rules, rule_meta
from .deps import store_dep
from .schemas import SubgraphRequest

router = APIRouter(prefix="/api", tags=["graph"])


@router.get("/graph/stats")
def stats(store: GraphStore = Depends(store_dep)) -> dict[str, Any]:
    data = store.stats()
    data["ontologyVersion"] = get_ontology().version
    data["ruleCount"] = len(load_rules())
    return data


@router.get("/graph/subgraph")
def subgraph(
    nodeIds: list[str] = Query(default_factory=list),
    depth: int = 1,
    relTypes: list[str] | None = Query(default=None),
    limit: int = 80,
    store: GraphStore = Depends(store_dep),
) -> dict[str, Any]:
    if not nodeIds:
        return {"nodes": [], "edges": [], "meta": {"error": "nodeIds 不能为空"}}
    sub = store.subgraph(nodeIds, depth=max(1, min(4, depth)), rel_types=relTypes, limit=limit)
    return sub.to_payload(get_ontology())


@router.post("/graph/subgraph")
def subgraph_post(req: SubgraphRequest, store: GraphStore = Depends(store_dep)) -> dict[str, Any]:
    sub = store.subgraph(req.node_ids, depth=max(1, min(4, req.depth)), rel_types=req.rel_types, limit=req.limit)
    return sub.to_payload(get_ontology())


@router.get("/graph/nodes")
def nodes(
    label: str | None = None,
    q: str | None = None,
    limit: int = 50,
    store: GraphStore = Depends(store_dep),
) -> dict[str, Any]:
    if q:
        return {"items": [{"id": n.id, "label": n.label, "name": n.name, "props": n.props}
                          for n in store.search(q, label=label, limit=limit)]}
    return {"items": [{"id": n.id, "label": n.label, "name": n.name}
                      for n in store.nodes(label, limit=limit)]}


@router.get("/ontology")
def ontology() -> dict[str, Any]:
    onto = get_ontology()
    return {
        "version": onto.version,
        "namespace": onto.namespace,
        "productLineScope": onto.product_line_scope,
        "classes": [
            {"name": c.name, "label": c.label, "prefix": c.prefix, "definition": c.definition,
             "abstract": c.abstract, "parent": c.parent, "viz": c.viz,
             "props": c.props}
            for c in onto.classes.values()
        ],
        "relations": onto.relation_table(),
        "gates": onto.gates,
    }


@router.get("/rules")
def rules() -> dict[str, Any]:
    return {"count": len(load_rules()), "items": rule_meta()}
