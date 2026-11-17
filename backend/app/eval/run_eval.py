"""评测执行器：rule / agent / rag 三组系统 + 两组消融。

用法：
    python -m backend.app.eval.run_eval --system rule
    python -m backend.app.eval.run_eval --system agent --limit 20
    python -m backend.app.eval.run_eval --all --repeat 3     # 五组 × 3 次，出均值±标准差

说明：vanilla-rag 是「检索若干三元组 → 单轮生成」的弱基线，仅用于对比；
本实现不调用 LLM，直接用最强三元组套模板，因此它代表「无规则约束的图检索」下限。

消融（文档 9.3）：
  * agent_noreflect —— 去掉覆盖度重试节点（确定性路径的 retry；该系统名沿用历史口径，
    确定性实现里没有语义反思，真正的 reflect 只在 --system agent_llm 中）；
  * agent_norules   —— 把规则推理换成纯图检索链（`reasoning/graph_only.py`）。
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import EVAL_DIR, ROOT
from ..kg import queries as Q
from ..kg.store import GraphStore
from ..reasoning.service import diagnose
from . import metrics

RESULTS_DIR = ROOT / "var" / "eval"


def load_questions(path: Path | None = None) -> list[dict[str, Any]]:
    path = path or (EVAL_DIR / "questions.jsonl")
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def _run_rule(store: GraphStore, q: dict[str, Any]) -> dict[str, Any]:
    d = diagnose(store, case_no=q.get("caseNo"), model=q.get("model"),
                 code=q.get("code"), symptom=q.get("symptom"), question=q.get("question"))
    return {
        "answer": d.answer,
        "faultCodes": [f["code"] for f in d.faults if f.get("code")],
        "solutionNames": [s["name"] for s in d.solutions],
        "triples": [[t[0], t[1], t[2]] for t in (e["triple"] for e in d.evidence if len(e["triple"]) == 3)],
        "claims": d.claims,
        "latencyMs": 0,
    }


def _run_agent(store: GraphStore, q: dict[str, Any], *,
               use_reflection: bool = True, use_rules: bool = True,
               use_llm: bool = False) -> dict[str, Any]:
    if use_llm:
        from ..agent.llm_agent import run_llm

        run = run_llm(store, question=q["question"], model=q.get("model"),
                      code=q.get("code"), symptom=q.get("symptom"),
                      use_reflection=use_reflection, use_rules=use_rules)
    else:
        from ..agent import graph as agent_graph

        run = agent_graph.run(store, question=q["question"], model=q.get("model"),
                              code=q.get("code"), symptom=q.get("symptom"),
                              use_reflection=use_reflection, use_rules=use_rules)
    d = run.diagnosis
    covered = [v for v in (run.coverage or {}).values() if v == "covered"]
    return {
        "answer": run.answer,
        "faultCodes": [f["code"] for f in d.faults if f.get("code")],
        "solutionNames": [s["name"] for s in d.solutions],
        "triples": [[t[0], t[1], t[2]] for t in (e["triple"] for e in d.evidence if len(e["triple"]) == 3)],
        "claims": d.claims,
        "latencyMs": 0,
        # 上下文完整度与工具效率：D/E 组的退化往往体现在这两项而不是准确率上
        "coverage": round(len(covered) / 5, 4) if run.coverage else None,
        "toolCalls": len(run.tool_results),
        # 字段名沿用历史口径：确定性系统统计的是覆盖度重试轮次（retry），
        # agent_llm 系统统计的才是 LLM 语义反思轮次（reflect）。
        "reflectionRounds": len(run.reflections),
        "policies": d.policies,
    }


def _run_agent_noreflect(store: GraphStore, q: dict[str, Any]) -> dict[str, Any]:
    return _run_agent(store, q, use_reflection=False)


def _run_agent_norules(store: GraphStore, q: dict[str, Any]) -> dict[str, Any]:
    return _run_agent(store, q, use_rules=False)


def _run_agent_llm(store: GraphStore, q: dict[str, Any]) -> dict[str, Any]:
    """RefKG-lite 的 LLM 驱动版本（文档第 8 节）；仅在配置 LLM_API_KEY 后可用。"""
    return _run_agent(store, q, use_llm=True)


def _nbrs(store: GraphStore, node_id: str, rel: str, direction: str = "out", limit: int = 50):
    """store.neighbors 返回 (edge, node)；统一成 (node, props)，并按单关系传参。"""
    return [(node, edge.props) for edge, node in store.neighbors(node_id, [rel], direction, limit)]


def _run_rag(store: GraphStore, q: dict[str, Any]) -> dict[str, Any]:
    """弱基线：只取最强的一条 症状→故障→根因→方案 链，不做规则校验与覆盖度重试。"""
    symptom = q.get("symptom") or ""
    if not symptom and q.get("code"):
        nodes = Q.find_error_code(store, q["code"])
        if nodes:
            parents = _nbrs(store, nodes[0].id, "HAS_CODE", "in")
            symptom = parents[0][0].name if parents else ""
    hits = store.search(symptom, label="Symptom", limit=1) if symptom else []
    if not hits:
        return {"answer": "无法从图谱检索到相关三元组。", "faultCodes": [], "solutionNames": [],
                "triples": [], "claims": [], "latencyMs": 0}
    sym = hits[0]
    faults = _nbrs(store, sym.id, "INDICATES", "out", 5)
    faults.sort(key=lambda x: -float(x[1].get("confidence", 0)))
    if not faults:
        return {"answer": "无匹配故障。", "faultCodes": [], "solutionNames": [], "triples": [], "claims": [], "latencyMs": 0}
    fault = faults[0][0]
    causes = _nbrs(store, fault.id, "CAUSED_BY", "out", 5)
    causes.sort(key=lambda x: -float(x[1].get("confidence", 0)))
    sols = []
    triples = [["SYM:" + symptom, "INDICATES", fault.id]]
    if causes:
        cause = causes[0][0]
        triples.append([fault.id, "CAUSED_BY", cause.id])
        sols = [n.name for n, _ in _nbrs(store, cause.id, "RESOLVED_BY", "out", 5)]
    answer = f"检索到最相关故障：{fault.name}。建议方案：{'、'.join(sols[:2]) or '未检索到'}。"
    return {"answer": answer, "faultCodes": [fault.props.get("code")], "solutionNames": sols,
            "triples": triples, "claims": [{"text": answer, "evidenceIds": ["E1"] if triples else []}],
            "latencyMs": 0}


SYSTEMS = {
    "rule": _run_rule,
    "agent": _run_agent,
    # 消融 D / E（文档 9.3）
    "agent_noreflect": _run_agent_noreflect,
    "agent_norules": _run_agent_norules,
    "rag": _run_rag,
    # LLM 驱动版（文档第 8 节）：与 A~E 并列的对比项，默认不进入 --all
    "agent_llm": _run_agent_llm,
}

# `--all` 固定跑文档 9.3 的五组系统（A~E），与 agent_llm 解耦，
# 避免「接入 LLM」悄悄改变 make eval 的历史口径。
ALL_SYSTEMS = ["rule", "agent", "agent_noreflect", "agent_norules", "rag"]


def run_eval(store: GraphStore, questions: list[dict[str, Any]], system: str = "rule") -> dict[str, Any]:
    runner = SYSTEMS.get(system)
    if runner is None:
        raise ValueError(f"未知系统 {system}，可选 {list(SYSTEMS)}")
    rows: list[dict[str, Any]] = []
    for q in questions:
        started = time.perf_counter()
        try:
            out = runner(store, q)
        except Exception as exc:  # noqa: BLE001 - 单题失败不应中断整体评测
            out = {"answer": f"[error] {exc}", "faultCodes": [], "solutionNames": [],
                   "triples": [], "claims": [], "latencyMs": 0}
        out["latencyMs"] = int((time.perf_counter() - started) * 1000)
        f = metrics.fault_prf(out["faultCodes"], q.get("goldFaults", []))
        p = metrics.path_prf(out["triples"], q.get("goldEvidenceTriples", []))
        # 没有金标准的题（超范围/型号未收录）记 None，不参与均值——
        # 它们本来就没有故障/方案/证据可答，计 0 分会污染整体指标。
        has_fault_gold = bool(q.get("goldFaults"))
        has_solution_gold = bool(str(q.get("goldSolution") or "").strip())
        has_path_gold = bool(q.get("goldEvidenceTriples"))
        rows.append({
            "id": q.get("id"),
            "type": q.get("type"),
            "answer": out["answer"],
            "faultCodes": out["faultCodes"],
            "solutionNames": out["solutionNames"][:3],
            "faultF1": f["f1"] if has_fault_gold else None,
            "pathF1": p["f1"] if has_path_gold else None,
            "pathPrecision": p["precision"] if has_path_gold else None,
            "pathRecall": p["recall"] if has_path_gold else None,
            "answerCorrect": metrics.answer_correct(out["answer"], q.get("mustInclude", [])),
            "solutionCorrect": (
                any(str(q.get("goldSolution", "")) in s for s in out["solutionNames"][:3])
                if has_solution_gold else None
            ),
            "faithfulness": metrics.faithfulness(out["claims"]),
            "hallucination": metrics.hallucination_rate(out["claims"]),
            "coverage": out.get("coverage"),
            "toolCalls": out.get("toolCalls"),
            "reflectionRounds": out.get("reflectionRounds"),
            "latencyMs": out["latencyMs"],
        })

    summary = metrics.summarize(rows)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    result = {"id": f"{system}_{stamp}", "system": system, "at": datetime.now().isoformat(timespec="seconds"),
              "summary": summary, "rows": rows}
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / f"{system}_{stamp}.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="运行评测")
    parser.add_argument("--system", default="rule", choices=list(SYSTEMS))
    parser.add_argument("--all", action="store_true", help="按文档 9.3 跑全部五组系统")
    parser.add_argument("--repeat", type=int, default=1, help="每系统重复次数（文档要求 3）")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--questions", type=Path, default=None)
    args = parser.parse_args()

    from ..kg import get_store

    store = get_store()
    questions = load_questions(args.questions)[: args.limit]
    systems = ALL_SYSTEMS if args.all else [args.system]

    agg: dict[str, dict[str, Any]] = {}
    for system in systems:
        runs = [run_eval(store, questions, system=system) for _ in range(max(1, args.repeat))]
        summary = metrics.aggregate([r["summary"] for r in runs])
        agg[system] = summary
        print(f"\n== {system}（{len(runs)} 次）")
        print(json.dumps(summary, ensure_ascii=False, indent=2))

    if len(systems) > 1:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = RESULTS_DIR / f"aggregate_{stamp}.json"
        path.write_text(json.dumps(
            {"at": datetime.now().isoformat(timespec="seconds"),
             "repeat": max(1, args.repeat), "nQuestions": len(questions), "systems": agg},
            ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n汇总（均值±标准差）已写入 {path}")


if __name__ == "__main__":
    main()
