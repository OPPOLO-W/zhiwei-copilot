"""图存储工厂。

优先 Neo4j；若未启动且允许降级，则回退到内存后端（仅用于开发/演示，日志会明确提示）。
生产语义仍是「Neo4j 是唯一事实源」。
"""

from __future__ import annotations

import logging
from functools import lru_cache

from ..config import get_settings
from .memory_store import MemoryStore
from .store import GraphStore

log = logging.getLogger("zhiwei.kg")

_store: GraphStore | None = None


def get_store(force: str | None = None) -> GraphStore:
    global _store
    if _store is not None and force is None:
        return _store

    backend = force or get_settings().kg_backend
    if backend == "memory":
        _store = MemoryStore.from_dir()
        return _store

    from .neo4j_store import Neo4jStore

    store = Neo4jStore()
    health = store.health()
    if not health.get("ok"):
        store.close()
        raise RuntimeError(
            "无法连接 Neo4j：" + str(health.get("error")) +
            "\n请确认 Neo4j Desktop 中 DBMS 已启动，并检查 .env 的 NEO4J_URI / NEO4J_PASSWORD。"
            "\n临时离线演示可设置 KG_BACKEND=memory。"
        )
    _store = store
    return _store


def reset_store() -> None:
    global _store
    if _store is not None:
        _store.close()
    _store = None


@lru_cache
def store_info() -> dict:
    try:
        return get_store().health()
    except Exception as exc:  # noqa: BLE001
        log.warning("图存储不可用: %s", exc)
        return {"ok": False, "error": str(exc)}
