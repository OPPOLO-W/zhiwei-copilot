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
from . import grounding, metrics

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


def _evidence_ids(d) -> list[str]:
    """本次诊断真实产出的证据 id 集合（E1/E2/…）。

    grounding 的悬空引用指标要拿它做白名单：claim 引用的 id 不在这里面，
    说明是「挂了个不存在的编号」，比「没挂编号」更接近伪造。
    """
    edges = (getattr(d, "graph", None) or {}).get("edges") or []
    return sorted({e.get("evidenceId") for e in edges if e.get("evidenceId")})


def _run_rule(store: GraphStore, q: dict[str, Any]) -> dict[str, Any]:
    d = diagnose(store, case_no=q.get("caseNo"), model=q.get("model"),
                 code=q.get("code"), symptom=q.get("symptom"), question=q.get("question"))
    return {
        "answer": d.answer,
        "faultCodes": [f["code"] for f in d.faults if f.get("code")],
        "solutionNames": [s["name"] for s in d.solutions],
        "triples": [[t[0], t[1], t[2]] for t in (e["triple"] for e in d.evidence if len(e["triple"]) == 3)],
        "claims": d.claims,
        "checkNames": [c["name"] for c in d.checks],
        "evidenceIds": _evidence_ids(d),
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
    # 答案自带的 claims（LLM 路径）优先：悬空引用/可信度要量「这段话的引用」，
    # 而不是永远拿规则模板的 claims 去量模型的措辞。
    llm_claims = getattr(run, "claims", None) or []
    # P1 候选重排的审计信息：重排是否发生、是否被安全边界锁定、拒了多少非法 id。
    # 没有 rank 步（确定性系统）时全为 None，不参与指标汇总。
    rank = next((t for t in run.trace if t.get("node") == "rank"), None)
    return {
        "answer": run.answer,
        "faultCodes": [f["code"] for f in d.faults if f.get("code")],
        "solutionNames": [s["name"] for s in d.solutions],
        "triples": [[t[0], t[1], t[2]] for t in (e["triple"] for e in d.evidence if len(e["triple"]) == 3)],
        "claims": llm_claims or d.claims,
        "claimsSource": "llm" if llm_claims else "rule",
        "checkNames": [c["name"] for c in d.checks],
        "evidenceIds": _evidence_ids(d),
        "latencyMs": 0,
        "rankDriver": (rank or {}).get("driver"),
        "rankLocked": (rank or {}).get("locked"),
        "rankRejected": (rank or {}).get("rejected", []),
        "rankEvidenceMiss": (rank or {}).get("evidenceMiss", 0),
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
                "triples": [], "claims": [], "checkNames": [], "evidenceIds": [], "latencyMs": 0}
    sym = hits[0]
    faults = _nbrs(store, sym.id, "INDICATES", "out", 5)
    faults.sort(key=lambda x: -float(x[1].get("confidence", 0)))
    if not faults:
        return {"answer": "无匹配故障。", "faultCodes": [], "solutionNames": [], "triples": [],
                "claims": [], "checkNames": [], "evidenceIds": [], "latencyMs": 0}
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
    # 注意：这里的 "E1" 是弱基线自己编的编号，并不指向真实 Evidence 节点。
    # 保留原样——grounding 会把它们全部判为悬空引用，这正是指标应有的行为
    # （弱基线的「引用」本来就是形式上的）。不为让它好看而改成空列表。
    return {"answer": answer, "faultCodes": [fault.props.get("code")], "solutionNames": sols,
            "triples": triples, "claims": [{"text": answer, "evidenceIds": ["E1"] if triples else []}],
            "evidenceIds": [], "latencyMs": 0}


def _gold_fault_name(store: GraphStore, q: dict[str, Any]) -> str | None:
    """金标准故障 id → 中文名，用于在答案文本里匹配。

    题集里 `goldFaults` 存的是节点 id（如 LEFT-WHEEL-STUCK），答案里写的是
    「左驱动轮卡滞」这类 name；不映射就永远匹配不上，expressed 指标会恒为 0。
    """
    gid = ((q.get("goldFaults") or [""])[0] or "").strip()
    if not gid:
        return None
    node = store.get(f"FAULT:{gid}") or store.get(gid)
    name = str(((node.props if node else {}) or {}).get("name") or "").strip()
    return name or None


def _run_rag_llm(store: GraphStore, q: dict[str, Any]) -> dict[str, Any]:
    """真·Vanilla RAG（BM25 检索 + 一次 LLM 调用），见 eval/rag.py。

    与旧 `rag` 的区别：旧基线不调 LLM、只取一条图链，拿它做 RAG 对照是稻草人。
    """
    from . import rag

    out = rag.answer_with_rag(store, str(q.get("question") or ""))
    out["claimsSource"] = "rag"
    return out


SYSTEMS = {
    "rule": _run_rule,
    "agent": _run_agent,
    # 消融 D / E（文档 9.3）
    "agent_noreflect": _run_agent_noreflect,
    "agent_norules": _run_agent_norules,
    "rag": _run_rag,
    # 真基线：检索 + 一次生成。旧 `rag` 保留，避免打断历史对比。
    "rag_llm": _run_rag_llm,
    # LLM 驱动版（文档第 8 节）：与 A~E 并列的对比项，默认不进入 --all
    "agent_llm": _run_agent_llm,
}

# `--all` 固定跑文档 9.3 的五组系统（A~E），与 agent_llm 解耦，
# 避免「接入 LLM」悄悄改变 make eval 的历史口径。
ALL_SYSTEMS = ["rule", "agent", "agent_noreflect", "agent_norules", "rag"]


def run_eval(store: GraphStore, questions: list[dict[str, Any]], system: str = "rule",
             dataset: str = "questions") -> dict[str, Any]:
    runner = SYSTEMS.get(system)
    if runner is None:
        raise ValueError(f"未知系统 {system}，可选 {list(SYSTEMS)}")
    # 本体白名单只构建一次（内部按 store 缓存），用于编造实体检测
    vocab = grounding.build_vocab(store)
    rows: list[dict[str, Any]] = []
    for q in questions:
        started = time.perf_counter()
        try:
            out = runner(store, q)
        except Exception as exc:  # noqa: BLE001 - 单题失败不应中断整体评测
            out = {"answer": f"[error] {exc}", "faultCodes": [], "solutionNames": [],
                   "triples": [], "claims": [], "checkNames": [], "evidenceIds": [], "latencyMs": 0}
        out["latencyMs"] = int((time.perf_counter() - started) * 1000)
        f = metrics.fault_prf(out["faultCodes"], q.get("goldFaults", []))
        p = metrics.path_prf(out["triples"], q.get("goldEvidenceTriples", []))
        # 没有金标准的题（超范围/型号未收录）记 None，不参与均值——
        # 它们本来就没有故障/方案/证据可答，计 0 分会污染整体指标。
        has_fault_gold = bool(q.get("goldFaults"))
        has_solution_gold = bool(str(q.get("goldSolution") or "").strip())
        has_path_gold = bool(q.get("goldEvidenceTriples"))
        # 硬幻觉四项：与正确性指标并列落盘，任一非零都能定位到具体题号
        g = grounding.check_row(
            answer=out["answer"],
            fault_codes=out["faultCodes"],
            solution_names=out["solutionNames"][:3],
            claims=out["claims"],
            available_evidence_ids=out.get("evidenceIds") or [],
            must_not_claim=q.get("mustNotClaim"),
            vocab=vocab,
            forbid_solutions=bool(q.get("forbidAnySolution")),
        )
        gold_fault = _gold_fault_name(store, q)
        gold_sol = str(q.get("goldSolution") or "").strip()
        expr = grounding.expressed_entities(out["answer"], vocab)
        rows.append({
            **g,
            "id": q.get("id"),
            "type": q.get("type"),
            "answer": out["answer"],
            "faultCodes": out["faultCodes"],
            "solutionNames": out["solutionNames"][:3],
            # 双评分：decision_* 量「诊断结构里排第一的结论」，expressed_* 量
            # 「答案文字里先说的结论」。只量前者时，换措辞与改推理不可区分。
            "goldFaultName": gold_fault,
            "expressedTopFault": expr["topFault"],
            "expressedTopSolution": expr["topSolution"],
            "expressedTopFaultCorrect": (
                expr["topFault"] == gold_fault if gold_fault else None
            ),
            "expressedTopSolutionCorrect": (
                expr["topSolution"] == gold_sol if gold_sol else None
            ),
            "expressedGoldSolutionMentioned": (
                gold_sol in out["answer"] if gold_sol else None
            ),
            # claims 来源：rule = 规则模板，llm = 答案自带。悬空引用的分母是谁，必须可查。
            "claimsSource": out.get("claimsSource", "rule"),
            "faultF1": f["f1"] if has_fault_gold else None,
            "pathF1": p["f1"] if has_path_gold else None,
            "pathPrecision": p["precision"] if has_path_gold else None,
            "pathRecall": p["recall"] if has_path_gold else None,
            "answerCorrect": metrics.answer_correct(out["answer"], q.get("mustInclude", [])),
            # 首选结论判定：旧口径「答案里出现过就算对」会把「先给错方案、再列对方案」判成正确，
            # 难题集上必须看系统**推荐了什么**而不是**提到了什么**。
            "topFaultCorrect": metrics.top1_correct(out["faultCodes"], (q.get("goldFaults") or [""])[0]),
            "topSolutionCorrect": metrics.top1_correct(out["solutionNames"], q.get("goldSolution")),
            "topCheckCorrect": metrics.top1_correct(out.get("checkNames") or [], q.get("goldCheckName")),
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
    result = {"id": f"{system}_{stamp}", "system": system, "dataset": dataset,
              "at": datetime.now().isoformat(timespec="seconds"),
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
    qpath = args.questions or (EVAL_DIR / "questions.jsonl")
    questions = load_questions(qpath)[: args.limit]
    dataset = qpath.stem
    systems = ALL_SYSTEMS if args.all else [args.system]

    agg: dict[str, dict[str, Any]] = {}
    for system in systems:
        runs = [run_eval(store, questions, system=system, dataset=dataset)
                for _ in range(max(1, args.repeat))]
        summary = metrics.aggregate([r["summary"] for r in runs])
        # 可重复性必须跨 run 比对 rows，不能靠 aggregate 的均值——均值相同不代表逐题相同
        summary["repeatability"] = metrics.repeatability([r["rows"] for r in runs])
        agg[system] = summary
        print(f"\n== {system}（{len(runs)} 次）")
        print(json.dumps(summary, ensure_ascii=False, indent=2))

    if len(systems) > 1:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = RESULTS_DIR / f"aggregate_{stamp}.json"
        path.write_text(json.dumps(
            {"at": datetime.now().isoformat(timespec="seconds"), "dataset": dataset,
             "repeat": max(1, args.repeat), "nQuestions": len(questions), "systems": agg},
            ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n汇总（均值±标准差）已写入 {path}")


if __name__ == "__main__":
    main()
