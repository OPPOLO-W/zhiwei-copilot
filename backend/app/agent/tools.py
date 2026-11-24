"""Agent 工具集。

原则（对应文档 8.5）：
  * 窄接口：LLM 不写 Cypher，只选择工具与参数；
  * 参数与返回值可 JSON 序列化，全部带 evidence / 计数，方便裁剪上下文；
  * 失败返回结构化错误，不把裸异常抛给模型。
  * 单次调用有墙钟上限：超时也返回结构化错误，让上层降级而不是卡死。
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import DATA_DIR, get_settings
from ..kg import queries as Q
from ..kg.store import GraphStore
from ..kg.text_index import BM25
from ..reasoning.graph_only import diagnose_graph_only
from ..reasoning.service import diagnose

# 文档正文缓存：chunks.jsonl 是 925 行的静态语料，进程内只读一次。
_CHUNKS: dict[str, list[dict[str, Any]]] | None = None
_INDEX: tuple[BM25, list[dict[str, Any]]] | None = None


def _load_chunks(path: Path | None = None) -> dict[str, list[dict[str, Any]]]:
    global _CHUNKS
    if _CHUNKS is None:
        groups: dict[str, list[dict[str, Any]]] = {}
        file = path or (DATA_DIR / "raw" / "chunks.jsonl")
        if file.exists():
            for line in file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                doc_id = str(row.get("doc_id") or "")
                if not doc_id:
                    continue
                groups.setdefault(doc_id, []).append({
                    "chunkId": str(row.get("chunk_id") or ""),
                    "page": row.get("page"),
                    "section": str(row.get("section") or ""),
                    "text": str(row.get("text") or ""),
                })
        _CHUNKS = groups
    return _CHUNKS


def _doc_index(path: Path | None = None) -> tuple[BM25, list[dict[str, Any]]]:
    """全语料 BM25 索引（只建一次），供 search_documents 用。"""
    global _INDEX
    if _INDEX is None:
        rows: list[dict[str, Any]] = []
        for doc_id, chunks in _load_chunks(path).items():
            for chunk in chunks:
                rows.append({"docId": doc_id, **chunk})
        _INDEX = (BM25([r["text"] for r in rows]), rows)
    return _INDEX


def _clip(text: str, limit: int) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


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
    def __init__(
        self, store: GraphStore, use_rules: bool = True, tool_timeout_s: float | None = None
    ) -> None:
        self.store = store
        self.use_rules = use_rules
        self.tool_timeout_s = (
            get_settings().agent_tool_timeout_s if tool_timeout_s is None else tool_timeout_s
        )
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
            "search_documents": self.search_documents,
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

    def get_document_span(self, doc_id: str, limit: int = 3,
                          max_chars: int = 600) -> dict[str, Any] | None:
        """返回文档的正文段落（此前只回元数据，名字叫 span 却拿不到文本）。

        单次最多 3 段、合计约 600 字，避免把上下文撑爆；段落按页码/顺序取前几段，
        调用方需要更细的内容就用 search_documents 定向检索。
        """
        node = self.store.get(doc_id)
        if node is None:
            return None
        passages: list[dict[str, Any]] = []
        used = 0
        for chunk in _load_chunks().get(doc_id, [])[:max(1, limit)]:
            text = _clip(chunk["text"], 220)
            if not text or used + len(text) > max_chars:
                continue
            used += len(text)
            passages.append({"chunkId": chunk["chunkId"], "page": chunk["page"],
                             "section": chunk["section"], "text": text})
        return {
            "docId": node.props.get("docId") or node.id,
            "title": node.name,
            "url": node.props.get("url"),
            "authority": node.props.get("authority"),
            "chunkCount": len(_load_chunks().get(doc_id, [])),
            "passages": passages,
        }

    def search_documents(self, query: str, k: int = 3) -> list[dict[str, Any]]:
        """按关键词检索手册/FAQ 正文，返回带 docId 与页码的片段。"""
        index, rows = _doc_index()
        hits = index.search(query, k=max(1, min(5, k)))
        return [
            {
                "docId": rows[i]["docId"],
                "chunkId": rows[i]["chunkId"],
                "page": rows[i]["page"],
                "section": rows[i]["section"],
                "text": _clip(rows[i]["text"], 180),
                "score": round(score, 2),
            }
            for i, score in hits
        ]

    # ---- 统一执行入口 ----
    def call(self, name: str, **kwargs: Any) -> ToolResult:
        """执行工具。超时、异常、未知工具都变成结构化结果，不向上抛。

        超时用守护线程实现而不是 ThreadPoolExecutor：被放弃的调用最多残留一个
        守护线程，不会拖住解释器退出，也不需要为每次调用建/拆线程池。
        """
        started = time.perf_counter()
        fn = self.registry.get(name)
        if fn is None:
            return ToolResult(name, kwargs, False, 0, "未知工具", error=f"tool_not_found:{name}")

        timeout = self.tool_timeout_s
        if timeout <= 0:
            return self._invoke(name, fn, kwargs, started)

        box: dict[str, Any] = {}

        def _target() -> None:
            try:
                box["data"] = fn(**kwargs)
            except BaseException as exc:  # noqa: BLE001 - 交给下方统一转换
                box["error"] = exc

        worker = threading.Thread(target=_target, name=f"tool:{name}", daemon=True)
        worker.start()
        worker.join(timeout)
        if worker.is_alive():
            latency = int((time.perf_counter() - started) * 1000)
            return ToolResult(
                name, kwargs, False, latency, f"工具超时（>{timeout:g}s）",
                error=f"tool_timeout:{name}",
            )
        if "error" in box:
            exc = box["error"]
            latency = int((time.perf_counter() - started) * 1000)
            return ToolResult(name, kwargs, False, latency, "工具执行失败", error=str(exc))
        data = box.get("data")
        latency = int((time.perf_counter() - started) * 1000)
        return ToolResult(name, kwargs, True, latency, self._summarize(name, data), data)

    def _invoke(
        self, name: str, fn: Callable[..., Any], kwargs: dict[str, Any], started: float
    ) -> ToolResult:
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
            if "passages" in data:
                return f"《{data.get('title') or ''}》返回 {len(data['passages'])} 段正文"
            if "nodeCount" in data:
                return f"子图 {data['nodeCount']} 节点 / {data['edgeCount']} 边"
            if "faults" in data:
                return f"命中 {len(data['faults'])} 个候选故障、{len(data['solutions'])} 个方案"
            return f"返回 {len(data)} 个字段"
        return "完成"
