"""API 依赖：图存储与运行上下文。"""

from __future__ import annotations

from typing import Iterator

from fastapi import HTTPException

from ..kg import get_store
from ..kg.store import GraphStore


def store_dep() -> GraphStore:
    try:
        return get_store()
    except Exception as exc:  # noqa: BLE001 - 转成 503，前端可展示可操作提示
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def iter_events(events: list[dict]) -> Iterator[str]:
    """把事件列表转成 SSE 帧。"""
    import json

    for event in events:
        yield f"event: {event.get('event', 'message')}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
