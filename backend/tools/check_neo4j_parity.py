"""校验 Neo4j 导入与 JSONL 图等价：同一批问题在两个后端必须给出相同结论。

背景：单元测试强制 KG_BACKEND=memory（conftest.py:13），因此 Neo4jStore 的
Cypher 分支长期没有测试覆盖，导入后是否等价无法自动发现。本脚本用真实题库
逐题跑两个后端并逐字段比对。

用法：
    python backend/tools/check_neo4j_parity.py            # 跑全部题目
    python backend/tools/check_neo4j_parity.py --limit 5  # 只跑前 5 题
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.config import EVAL_DIR  # noqa: E402
from backend.app.kg import get_store, reset_store  # noqa: E402
from backend.app.reasoning.service import diagnose  # noqa: E402


def load_questions(limit: int | None) -> list[dict]:
    path = EVAL_DIR / "questions.jsonl"
    rows = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    return rows[:limit] if limit else rows


def current_model() -> str:
    """取图里实际存在的型号，题库型号可能滞后于语料。"""
    reset_store()
    store = get_store("memory")
    models = sorted({n.props["modelCode"] for n in store.nodes("ProductModel")})
    if not models:
        raise SystemExit("图谱中没有 ProductModel，检查 data/curated")
    return models[0]


def comparable(result: dict) -> dict:
    """去掉只用于标识自身的字段，其余必须逐字段相同。

    trace.backend 记的是「这次走的哪个后端」，两个后端天然不同，不构成差异。
    """
    out = dict(result)
    trace = dict(out.get("trace") or {})
    trace.pop("backend", None)
    out["trace"] = trace
    return out


def run(backend: str, questions: list[dict], model: str) -> list[dict]:
    reset_store()
    store = get_store(backend)
    out = []
    for q in questions:
        d = diagnose(
            store,
            model=model,
            code=q.get("code") or None,
            symptom=q.get("symptom") or None,
            question=q["question"],
        )
        out.append(comparable(d.to_dict()))
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="对比 Neo4j 与 memory 后端的诊断结果")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    questions = load_questions(args.limit)
    model = current_model()
    print(f"题目 {len(questions)} 道，统一用型号「{model}」\n")

    mem = run("memory", questions, model)
    neo = run("neo4j", questions, model)

    diff = 0
    for q, a, b in zip(questions, mem, neo):
        if a == b:
            print(f"  OK   {q['id']}  {q['question'][:40]}")
            continue
        diff += 1
        print(f"  DIFF {q['id']}  {q['question'][:40]}")
        keys = {k for k in set(a) | set(b) if a.get(k) != b.get(k)}
        for k in sorted(keys):
            print(f"         {k}: memory={str(a.get(k))[:80]}")
            print(f"         {' ' * len(k)}  neo4j ={str(b.get(k))[:80]}")

    print(f"\n结论：{len(questions) - diff}/{len(questions)} 一致" + ("" if diff == 0 else f"，{diff} 道存在差异"))
    raise SystemExit(0 if diff == 0 else 1)


if __name__ == "__main__":
    main()
