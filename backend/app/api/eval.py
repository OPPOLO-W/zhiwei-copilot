"""评测接口：读取题库、触发规则基线评测、查看结果。"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from ..config import EVAL_DIR, ROOT
from ..eval.run_eval import load_questions, run_eval
from ..kg.store import GraphStore
from .deps import store_dep

router = APIRouter(prefix="/api/eval", tags=["eval"])

RESULTS_DIR = ROOT / "var" / "eval"


@router.get("/questions")
def questions(split: str | None = None) -> dict[str, Any]:
    items = load_questions()
    if split:
        items = [q for q in items if q.get("split") == split]
    return {"count": len(items), "items": items}


@router.post("/run")
def run(limit: int = 50, system: str = "rule", store: GraphStore = Depends(store_dep)) -> dict[str, Any]:
    items = load_questions()[:limit]
    if not items:
        raise HTTPException(status_code=404, detail=f"未找到题库：{EVAL_DIR / 'questions.jsonl'}")
    return run_eval(store, items, system=system)


@router.get("/runs")
def runs(limit: int = 10) -> dict[str, Any]:
    if not RESULTS_DIR.exists():
        return {"items": []}
    files = sorted(RESULTS_DIR.glob("*.json"), reverse=True)
    # aggregate_*.json 是「五组 × N 次」的汇总，不是单次运行，列表里会显示成一行空记录
    files = [p for p in files if not p.name.startswith("aggregate_")][:limit]
    items = []
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        items.append({"id": path.stem, "system": data.get("system"),
                      "summary": data.get("summary"), "at": data.get("at")})
    return {"items": items}


@router.get("/runs/{run_id}")
def run_detail(run_id: str) -> dict[str, Any]:
    path = RESULTS_DIR / f"{run_id}.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="评测结果不存在")
    return json.loads(path.read_text(encoding="utf-8"))
