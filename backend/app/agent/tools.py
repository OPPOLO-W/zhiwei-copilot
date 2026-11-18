"""Agent 工具集。

原则（对应文档 8.5）：
  * 窄接口：LLM 不写 Cypher，只选择工具与参数；
  * 参数与返回值可 JSON 序列化，全部带 evidence / 计数，方便裁剪上下文；
  * 失败返回结构化错误，不把裸异常抛给模型。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

from ..kg import queries as Q
from ..kg.store import GraphStore
from ..reasoning.graph_only import diagnose_graph_only
from ..reasoning.service import diagnose


@dataclass
class ToolResult:
    tool: str
    args: dict[str, Any]
    ok: bool
    latencyMs: int
    summary: str
    data: Any = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool, "args": self.args, "ok": self.ok,
            "latencyMs": self.latencyMs, "summary": self.summary,
            "error": self.error,
        }


class Tools:
    def __init__(self, store: GraphStore, use_rules: bool = True) -> None:
        self.store = store
        self.use_rules = use_rules
        self.registry: dict[str, Callable[..., Any]] = {
            "resolve_entity": self.resolve_entity,
            "get_case_context": self.get_case_context,
            "expand_subgraph": self.expand_subgraph,
            "find_paths": self.find_paths,
            "run_rules": self.run_rules,
            "retrieve_chain": self.retrieve_chain,
            "search_similar_cases": self.search_similar_cases,
            "check_compatibility": self.check_compatibility,
            "get_document_span": self.get_document_span,
        }

    # ---- 工具实现 ----
    def resolve_entity(self, text: str, label: str | None = None) -> list[dict[str, Any]]:
        return [
            {"id": n.id, "label": n.label, "name": n.name, "props": {
                k: n.props.get(k) for k in ("code", "modelCode", "sku", "severity") if n.props.get(k)
            }}
            for n in self.store.search(text, label=label, limit=8)
        ]

    def get_case_context(self, case_no: str) -> dict[str, Any] | None:
        return Q.case_context(self.store, case_no)

    def expand_subgraph(self, node_ids: list[str], rel_types: list[str] | None = None,
                        depth: int = 1, limit: int = 80) -> dict[str, Any]:
        sub = self.store.subgraph(node_ids, depth=min(2, depth), rel_types=rel_types, limit=limit)
        return {
            "nodeCount": len(sub.nodes),
            "edgeCount": len(sub.edges),
            "truncated": sub.truncated,
            "nodes": [{"id": n.id, "label": n.label, "name": n.name} for n in sub.nodes[:40]],
        }

    def find_paths(self, start: str, end: str, max_hops: int = 4) -> list[dict[str, Any]]:
        paths = self.store.find_paths(start, end, max_hops=min(4, max_hops), limit=5)
        return [
            {"hops": len(p), "edges": [f"{e.head} -[{e.type}]-> {e.tail}" for e in p]}
            for p in paths
        ]

    def run_rules(self, model: str | None = None, code: str | None = None,
                  symptom: str | None = None, case_no: str | None = None,
                  actions: list[str] | None = None,
                  check_results: list[dict[str, str]] | None = None) -> dict[str, Any]:
        d = diagnose(self.store, case_no=case_no, model=model, code=code, symptom=symptom,
                     actions=actions, check_results=check_results)
        return d.to_dict()

    def retrieve_chain(self, model: str | None = None, code: str | None = None,
                       symptom: str | None = None, case_no: str | None = None) -> dict[str, Any]:
        """消融 E 专用：只做图检索链，不经规则推理（见 reasoning/graph_only.py）。"""
        d = diagnose_graph_only(self.store, case_no=case_no, model=model,
                                code=code, symptom=symptom)
        return d.to_dict()

    def search_similar_cases(self, model: str, symptom: str, days: int = 90) -> list[dict[str, Any]]:
        return Q.similar_cases(self.store, model, symptom, days=days)

    def check_compatibility(self, item: str, model: str) -> dict[str, Any]:
        hits = self.store.search(item, limit=5)
        hits = [n for n in hits if n.label in ("Consumable", "SparePart")]
        if not hits:
            return {"found": False, "reason": "图谱中没有该耗材/备件"}
        item_node = hits[0]
        model_node = Q.resolve_model(self.store, model)
        if model_node is None:
            return {"found": False, "reason": f"未知型号 {model}"}
        edges = [(node, edge.props) for edge, node in
                 self.store.neighbors(item_node.id, ["COMPATIBLE_WITH"], "out", 20)]
        ok = any(node.id == model_node.id for node, _ in edges)
        return {
            "found": True,
            "item": item_node.name,
            "sku": item_node.props.get("sku"),
            "model": model_node.props.get("modelCode"),
            "compatible": ok,
            "userReplaceable": item_node.props.get("userReplaceable"),
            "evidence": edges[0][1].get("evidence") if edges else "",
        }

    def get_document_span(self, doc_id: str) -> dict[str, Any] | None:
        node = self.store.get(doc_id)
        if node is None:
            return None
        return {"docId": node.props.get("docId"), "title": node.name,
                "url": node.props.get("url"), "authority": node.props.get("authority")}

    # ---- 统一执行入口 ----
    def call(self, name: str, **kwargs: Any) -> ToolResult:
        started = time.perf_counter()
        fn = self.registry.get(name)
        if fn is None:
            return ToolResult(name, kwargs, False, 0, "未知工具", error=f"tool_not_found:{name}")
        try:
            data = fn(**kwargs)
            latency = int((time.perf_counter() - started) * 1000)
            return ToolResult(name, kwargs, True, latency, self._summarize(name, data), data)
        except Exception as exc:  # noqa: BLE001 - 工具失败要变成结构化错误
            latency = int((time.perf_counter() - started) * 1000)
            return ToolResult(name, kwargs, False, latency, "工具执行失败", error=str(exc))

    @staticmethod
    def _summarize(name: str, data: Any) -> str:
        if isinstance(data, list):
            return f"返回 {len(data)} 条"
        if isinstance(data, dict):
            if "nodeCount" in data:
                return f"子图 {data['nodeCount']} 节点 / {data['edgeCount']} 边"
            if "faults" in data:
                return f"命中 {len(data['faults'])} 个候选故障、{len(data['solutions'])} 个方案"
            return f"返回 {len(data)} 个字段"
        return "完成"
