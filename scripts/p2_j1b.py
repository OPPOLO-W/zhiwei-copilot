"""J1b：多轮追问验收（P2.3）。

对 H14–H16（用户主动要求鉴别）与 3 道歧义题：
  第 1 轮：不注入任何事实，看系统是否问、问哪个检查；
  第 2 轮：模拟用户回答「正常/没异常」（negative），看结论是否真的收窄，
          以及首选检查是否变成金标准那个。

用法：`.venv\\Scripts\\python.exe scripts/p2_j1b.py`
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.app.agent.llm_agent import run_llm  # noqa: E402
from backend.app.kg import get_store  # noqa: E402

TARGETS = ["H14", "H15", "H16", "H01", "H06", "H08"]


def _top_check(run) -> str | None:
    return run.diagnosis.checks[0]["name"] if run.diagnosis.checks else None


def main() -> None:
    store = get_store("memory")
    questions = {q["id"]: q for q in map(json.loads, (ROOT / "data/eval/questions_hard.jsonl")
                                        .read_text(encoding="utf-8").splitlines())}
    rows = []
    for qid in TARGETS:
        q = questions[qid]
        ask_ok = q.get("goldCheckName")
        first = run_llm(store, question=q["question"], model=q.get("model") or "T20 PRO",
                        code=q.get("code"), symptom=q.get("symptom"), allow_ask=True,
                        asked_checks=[])
        ask = first.extra.get("askUser")
        row = {"id": qid, "goldCheck": ask_ok, "asked": bool(ask),
               "askedCheck": (ask or {}).get("name"), "turn1Check": _top_check(first),
               "turn2Check": None, "rounds": 1, "status1": first.status}
        if ask:
            second = run_llm(
                store, question="正常，没发现异常", model=q.get("model") or "T20 PRO",
                code=q.get("code"),
                check_results=[{"check": ask["checkId"], "fault": ask.get("faultId") or "",
                                "result": "negative"}],
                prior_facts=first.extra.get("facts") or [], allow_ask=True,
                asked_checks=[ask["checkId"]],
            )
            row["turn2Check"] = _top_check(second)
            row["rounds"] = 2
            row["status2"] = second.status
            row["topCause2"] = second.diagnosis.causes[0]["name"] if second.diagnosis.causes else None
        row["hitGoldAfter"] = bool(ask_ok) and row["turn2Check"] == ask_ok
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False))

    asked = sum(1 for r in rows if r["asked"])
    hit = sum(1 for r in rows if r["hitGoldAfter"])
    print(json.dumps({"n": len(rows), "asked": asked, "goldAfterTurn2": hit,
                      "maxRounds": max(r["rounds"] for r in rows)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
