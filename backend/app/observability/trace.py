"""可观测性：把每次运行写成 JSONL，供复盘与实验分析使用。"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import get_settings

log = logging.getLogger("zhiwei.trace")


def write_trace(run_id: str, payload: dict[str, Any]) -> None:
    try:
        directory = Path(get_settings().trace_dir)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{datetime.now():%Y-%m-%d}.jsonl"
        record = {"runId": run_id, "at": datetime.now().isoformat(timespec="seconds"), **payload}
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception as exc:  # noqa: BLE001 - 可观测性失败不能影响主流程
        log.warning("写入 trace 失败: %s", exc)


def list_traces(limit: int = 20) -> list[dict[str, Any]]:
    directory = Path(get_settings().trace_dir)
    if not directory.exists():
        return []
    files = sorted(directory.glob("*.jsonl"), reverse=True)
    out: list[dict[str, Any]] = []
    for path in files:
        lines = path.read_text(encoding="utf-8").splitlines()
        for line in reversed(lines):
            if len(out) >= limit:
                return out
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out
