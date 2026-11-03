"""评测指标。

指标定义对应开发文档 9.4；这里只实现可自动计算的客观指标，
可解释性主观评分（1~5 分）仍需人工双盲。
"""

from __future__ import annotations

from typing import Any, Iterable


def _norm(s: Any) -> str:
    return str(s or "").strip()


def fault_prf(predicted: Iterable[str], gold: Iterable[str]) -> dict[str, float]:
    pred, g = set(map(_norm, predicted)), set(map(_norm, gold))
    tp = len(pred & g)
    precision = tp / len(pred) if pred else 0.0
    recall = tp / len(g) if g else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"precision": round(precision, 4), "recall": round(recall, 4), "f1": round(f1, 4)}


def path_prf(predicted: list[list[str]], gold: list[list[str]]) -> dict[str, float]:
    """证据三元组集合的 P/R/F1（不比较节点序列，避免同一路径多种写法）。"""
    pred = {tuple(map(_norm, t)) for t in predicted}
    g = {tuple(map(_norm, t)) for t in gold}
    tp = len(pred & g)
    precision = tp / len(pred) if pred else 0.0
    recall = tp / len(g) if g else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"precision": round(precision, 4), "recall": round(recall, 4), "f1": round(f1, 4)}


def answer_correct(answer: str, must_include: list[str]) -> bool:
    return all(k in answer for k in must_include) if must_include else True


def faithfulness(claims: list[dict[str, Any]]) -> float:
    if not claims:
        return 0.0
    supported = sum(1 for c in claims if c.get("evidenceIds"))
    return round(supported / len(claims), 4)


def hallucination_rate(claims: list[dict[str, Any]]) -> float:
    if not claims:
        return 0.0
    return round(sum(1 for c in claims if not c.get("evidenceIds")) / len(claims), 4)


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round((p / 100) * (len(ordered) - 1)))))
    return round(ordered[idx], 2)


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {}
    n = len(rows)

    def mean(key: str, source: dict[str, Any] | None = None) -> float:
        """只对「该类有金标准」的题求均值。

        没有金标准的题（超范围、型号未收录等）在 run_eval 里记为 None：
        它们本来就不该有故障/方案/证据，计 0 分等于用 7 道无解题把整体指标压低 14%，
        会把「系统答对了」和「题目没有答案」混成同一个数。
        """
        vals = [(r if source is None else r.get(source, {})).get(key) for r in rows]
        vals = [v for v in vals if v is not None]
        return round(sum(vals) / len(vals), 4) if vals else 0.0

    def optional_mean(key: str) -> float | None:
        """整组都没有这项时返回 None（例如规则系统没有覆盖度/工具调用）。"""
        vals = [r.get(key) for r in rows if r.get(key) is not None]
        return round(sum(vals) / len(vals), 4) if vals else None

    def scored(key: str) -> int:
        return sum(1 for r in rows if r.get(key) is not None)

    latencies = [r["latencyMs"] for r in rows]
    return {
        "n": n,
        "nFaultScored": scored("faultF1"),
        "nSolutionScored": scored("solutionCorrect"),
        "nPathScored": scored("pathF1"),
        "answerAccuracy": mean("answerCorrect"),
        "faultF1": mean("faultF1"),
        "solutionAccuracy": mean("solutionCorrect"),
        # path P/R 必须与 F1 一起看：规则/Agent 输出的是完整证明链（召回高、精度被摊薄），
        # 弱基线只返回一条最短路（反之），单看 F1 会误判。
        "pathPrecision": mean("pathPrecision"),
        "pathRecall": mean("pathRecall"),
        "pathF1": mean("pathF1"),
        "faithfulness": mean("faithfulness"),
        "hallucinationRate": mean("hallucination"),
        # Agent 专项：上下文完整度与工具效率。规则/弱基线没有这两个环节，
        # 记 None 而不是 0——否则对比表会显示「规则系统覆盖度为 0」，那是误导。
        "coverage": optional_mean("coverage"),
        "toolCalls": optional_mean("toolCalls"),
        "reflectionRounds": optional_mean("reflectionRounds"),
        "latencyP50": percentile(latencies, 50),
        "latencyP95": percentile(latencies, 95),
    }


def aggregate(summaries: list[dict[str, Any]]) -> dict[str, Any]:
    """多次运行的均值与标准差（文档 9.3：每系统跑 3 次，报告均值 ± 标准差）。

    本实现的各系统都是确定性的（不调 LLM，温度无关），因此标准差预期为 0；
    仍然照跑 3 次，是为了把「可复现」从口头声明变成可核对的数字。
    """
    if not summaries:
        return {}
    # 只聚合「至少有一次非空」的指标：规则/弱基线没有 coverage、toolCalls，
    # 把它们当 0 参与均值会造出一列假数据。
    keys = [k for k in summaries[0]
            if isinstance(summaries[0][k], (int, float))
            and any(s.get(k) is not None for s in summaries)]
    out: dict[str, Any] = {"runs": len(summaries), "mean": {}, "std": {}}
    for key in keys:
        vals = [float(s.get(key) if s.get(key) is not None else 0.0) for s in summaries]
        m = sum(vals) / len(vals)
        var = sum((v - m) ** 2 for v in vals) / len(vals)
        out["mean"][key] = round(m, 4)
        out["std"][key] = round(var ** 0.5, 4)
    return out
