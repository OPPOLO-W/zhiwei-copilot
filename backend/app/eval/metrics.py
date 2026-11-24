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


def top1_correct(predicted: list[str], gold: str) -> bool | None:
    """首选结论是否命中金标准。

    为什么需要这个而不是复用 `answer_correct`：完整答案里通常会把候选方案**全部列出**，
    「先建议用户做错的事，再把正确方案列为备选」也会因为关键词出现过而被判对。
    实测在难题集上 rule 的 answerAccuracy 是 0.65，但首选方案只有 1/20 正确——
    这正是旧 50 题 0.98 的机制：判定宽，不是系统强。

    gold 为空表示该题没有金标准（未收录型号/超范围），返回 None，不参与均值。
    """
    if not str(gold or "").strip():
        return None
    return bool(predicted) and str(predicted[0]).strip() == str(gold).strip()


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

    def total(key: str) -> int:
        """求和项：违规条数这类指标求均值没有意义（0.02 次违规是什么意思？）。"""
        return sum(int(r.get(key) or 0) for r in rows)

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
        # 首选结论三项：与 answerAccuracy 并列，回答的是不同问题——
        # 「提到了正确答案吗」 vs 「第一个推荐的就是正确答案吗」。
        # 前者用于与历史 50 题口径对齐，后者用于难题集判定（见 reports/uplift-summary.md）。
        "topFaultAccuracy": mean("topFaultCorrect"),
        "topSolutionAccuracy": mean("topSolutionCorrect"),
        "topCheckAccuracy": mean("topCheckCorrect"),
        # 双评分对照面：答案文本里「先说的结论」是否正确（grounding.expressed_entities）。
        # decision_* 高、expressed_* 低 = 结构排对了但答案没说；反之 = 答案说对了但结构没排。
        "expressedTopFaultAccuracy": mean("expressedTopFaultCorrect"),
        "expressedTopSolutionAccuracy": mean("expressedTopSolutionCorrect"),
        "expressedGoldSolutionRate": mean("expressedGoldSolutionMentioned"),
        "faultF1": mean("faultF1"),
        "solutionAccuracy": mean("solutionCorrect"),
        # path P/R 必须与 F1 一起看：规则/Agent 输出的是完整证明链（召回高、精度被摊薄），
        # 弱基线只返回一条最短路（反之），单看 F1 会误判。
        "pathPrecision": mean("pathPrecision"),
        "pathRecall": mean("pathRecall"),
        "pathF1": mean("pathF1"),
        "faithfulness": mean("faithfulness"),
        "hallucinationRate": mean("hallucination"),
        # 硬幻觉四项（grounding.py）：率看均值，条数看求和。
        # 与上面的 hallucinationRate 并列，不替代——旧指标是 A~E 的历史口径。
        "fabricatedRate": mean("fabricatedRate"),
        "fabricatedTotal": total("fabricatedCount"),
        "danglingRate": mean("danglingRate"),
        "danglingTotal": total("danglingCount"),
        "overclaimTotal": total("overclaimCount"),
        "safetyViolationTotal": total("safetyViolationCount"),
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


# ---------------------------------------------------------------------------
# 可重复性：Pass^k 与逐题一致性
# ---------------------------------------------------------------------------

# 判定「同一次运行的输出与另一次相同」时比较的字段。
# 只比结论性字段：答案文本的措辞可以变（LLM 路径尤其），但故障集合、方案集合、
# 以及对错判定必须稳定——这三项变了，用户看到的结果就变了。
REPEAT_KEYS = ("faultCodes", "solutionNames", "answerCorrect")


def repeatability(runs: list[list[dict[str, Any]]]) -> dict[str, Any]:
    """跨 k 次运行的一致性与 Pass^k。

    `exactRepeatRate`：k 次运行中，逐题判定字段完全一致的题占比。
        确定性系统必须为 1.0；不等于 1.0 说明存在隐藏状态（缓存/时间/随机布局）。
    `passAtK`：k 次全部答对的题占比。注意这是 **Pass^k**（k 次全对才算可靠），
        不是 Pass@k（k 次中一次对就算成功）——后者会把偶发正确算成能力。

    两次运行之间不比较 latencyMs：时延本身受机器负载影响，不算「结果不稳定」。
    """
    if len(runs) < 2:
        return {"runs": len(runs), "exactRepeatRate": None, "passAtK": None,
                "unstableIds": [], "alwaysWrongIds": []}

    index: dict[str, list[dict[str, Any]]] = {}
    for rows in runs:
        for r in rows:
            index.setdefault(str(r.get("id")), []).append(r)

    def signature(item: dict[str, Any]) -> tuple:
        """把一题的结论字段压成可哈希签名。列表字段先排序，避免顺序差异被当成不稳定。"""
        parts: list[tuple[str, ...]] = []
        for key in REPEAT_KEYS:
            val = item.get(key)
            parts.append(tuple(sorted(map(str, val))) if isinstance(val, list) else (str(val),))
        return tuple(parts)

    unstable: list[str] = []
    always_wrong: list[str] = []
    all_correct = 0
    for qid, items in index.items():
        if len(items) != len(runs):
            # 某次运行缺题：本身就是不稳定，直接计入
            unstable.append(qid)
            continue
        if len({signature(it) for it in items}) > 1:
            unstable.append(qid)
        if all(it.get("answerCorrect") is False for it in items):
            always_wrong.append(qid)
        if all(it.get("answerCorrect") is True for it in items):
            all_correct += 1

    n = len(index)
    return {
        "runs": len(runs),
        "exactRepeatRate": round((n - len(unstable)) / n, 4) if n else None,
        # Pass^k 独立于一致性：措辞变了但 k 次都判对，仍然算「可靠答对」；
        # 反之 k 次都一致地答错，exactRepeatRate 高但 passAtK 低，两个数必须分开看。
        "passAtK": round(all_correct / n, 4) if n else None,
        "unstableIds": sorted(unstable),
        "alwaysWrongIds": sorted(always_wrong),
    }
