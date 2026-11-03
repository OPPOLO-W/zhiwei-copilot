"""FastAPI 入口。

启动：
    uvicorn backend.app.main:app --reload --port 8000
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api.eval import router as eval_router
from .api.graph import router as graph_router
from .api.query import router as query_router
from .config import get_settings
from .kg import get_store, store_info

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("zhiwei.api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    try:
        store = get_store()
        log.info("图存储就绪: %s", store.health())
    except Exception as exc:  # noqa: BLE001 - 启动阶段降级而不是直接崩
        log.error("图存储不可用，API 将返回 503: %s", exc)
    if not settings.llm_enabled:
        log.warning("未配置 LLM_API_KEY：Agent 走确定性兜底，抽取接口不可用")
    yield


app = FastAPI(
    title="智维 Copilot API",
    description="科沃斯 DEEBOT T 系列售后知识图谱与故障排查 Agent",
    version="0.1.0",
    lifespan=lifespan,
)

settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(query_router)
app.include_router(graph_router)
app.include_router(eval_router)


@app.get("/api/health", tags=["ops"])
def health() -> dict:
    info = store_info()
    return {
        "ok": info.get("ok", False),
        "backend": info.get("backend"),
        "entities": info.get("entities"),
        "relations": info.get("relations"),
        "llm": settings.llm_enabled,
        "model": settings.llm_model if settings.llm_enabled else None,
        "error": info.get("error"),
    }


@app.get("/", include_in_schema=False)
def root() -> dict:
    return {"service": "zhiwei-copilot", "docs": "/docs", "health": "/api/health"}
