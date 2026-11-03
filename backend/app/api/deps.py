"""API 依赖：图存储。"""

from __future__ import annotations

from fastapi import HTTPException

from ..kg import get_store
from ..kg.store import GraphStore


def store_dep() -> GraphStore:
    try:
        return get_store()
    except Exception as exc:  # noqa: BLE001 - 转成 503，前端可展示可操作提示
        raise HTTPException(status_code=503, detail=str(exc)) from exc
