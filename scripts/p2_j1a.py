"""P2.1 J1a：8 道 precondition 题跑 extract_facts，检查首选方案与提问数。

用法：.venv\\Scripts\\python.exe scripts\\p2_j1a.py
输出每题：gold / rule 首选 / llm 首选 / 抽取到的事实 / 被门控拒绝的事实 / ask_user 次数（P2.1 恒为 0）。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("KG_BACKEND", "memory")

from backend.app.agent.llm_agent import run_llm  # noqa: E402
from backend.app.agent import graph  # noqa: E402
from backend.app.kg import get_store  # noqa: E402


def main() -> int:
    store = get_store("memory")
    rows = [json.loads(line) for line in
            (ROOT / "data/eval/questions_hard.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    pre = [q for q in rows if q.get("type") == "precondition"]
    hit_rule = hit_llm = 0
    for q in pre:
        base = graph.run(store, question=q["question"]).diagnosis
        run = run_llm(store, question=q["question"])
        step = next((t for t in run.trace if t.get("node") == "facts"), None) or {}
        accepted = step.get("accepted") or []
        rejected = step.get("rejected") or []
        gold = q.get("goldSolution") or ""
        rule_top = base.solutions[0]["name"] if base.solutions else "—"
        llm_top = run.diagnosis.solutions[0]["name"] if run.diagnosis.solutions else "—"
        hit_rule += rule_top == gold
        hit_llm += llm_top == gold
        print(json.dumps({
            "id": q["id"], "gold": gold, "rule": rule_top, "llm": llm_top,
            "ok": llm_top == gold,
            "facts": [(f["kind"], f["ref"], f["quote"]) for f in accepted],
            "rejected": [(r["kind"], r["ref"], r["reason"]) for r in rejected],
            "status": run.status,
        }, ensure_ascii=False))
    print(json.dumps({"n": len(pre), "hitRule": hit_rule, "hitLlm": hit_llm}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
