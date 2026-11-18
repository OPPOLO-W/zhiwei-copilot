"""追问决策（P2.3）：只有在「问一句」能明显改变结论时才打断用户。

设计要点：
  * 信息增益按根因后验算，不按故障算——同一故障下多个根因共享检查时，
    故障级 DETECTS 的增益恒为 0（H01 的三种检查都指向同一故障）；
  * 成本按 风险等级 × 耗时 计，风险高的检查即使增益大也要付出更高代价；
  * 已经采信过用户自述事实的会话不再追问：用户说「我清理过」，再问「你清理过吗」
    是最典型的坏体验，也最容易把已排除的根因问回来。
"""

from __future__ import annotations

import math
from typing import Any, Iterable

from ..kg.store import GraphStore

GATE_MIN_TOP = 0.70       # 首选根因低于此值才考虑追问
GATE_MAX_MARGIN = 0.25    # 首选与次选差距小于此值才算有歧义
MIN_IG = 0.05             # 增益太小的检查不值得打扰用户
CONFIRM_BOOST = 0.9       # 阳性结果对「该检查确认的根因」的后验提升
MAX_CANDIDATES = 4        # 只看置信度最高的几个根因
RISK_COST = {"low": 1.0, "medium": 2.0, "high": 4.0}
MAX_ASKS_PER_TURN = 1

# 命中即不追问：安全、超出范围、未收录型号这类场景先给结论或引导，不能拖成问答
NO_ASK_POLICIES = {
    "safety_stop", "lookup_miss", "unknown_model", "unknown_code", "unknown_intent",
    "out_of_scope", "code_not_applicable",
}


def _entropy(probs: Iterable[float]) -> float:
    return -sum(p * math.log2(p) for p in probs if p > 0)


def _normalise(pairs: list[tuple[str, float]]) -> list[tuple[str, float]]:
    total = sum(max(0.0, p) for _, p in pairs)
    if total <= 0:
        return []
    return [(cid, max(0.0, p) / total) for cid, p in pairs]


def prior_causes(diagnosis) -> list[tuple[str, float]]:
    """按置信度取前 4 个根因归一化成先验（均匀分布会把增益算飞）。"""
    ranked = sorted(diagnosis.causes, key=lambda c: -float(c.get("confidence") or 0))[:MAX_CANDIDATES]
    return _normalise([(c["id"], float(c.get("confidence") or 0)) for c in ranked])


def posterior(prior: list[tuple[str, float]], cause_id: str, positive: bool) -> list[tuple[str, float]]:
    """观察一次检查结果后的后验：阳性抬升被确认的根因，阴性则压低它。"""
    out = []
    for cid, p in prior:
        if cid == cause_id:
            weight = CONFIRM_BOOST if positive else (1.0 - CONFIRM_BOOST)
        else:
            weight = (1.0 - CONFIRM_BOOST) if positive else CONFIRM_BOOST
        out.append((cid, p * weight))
    return _normalise(out)


def information_gain(prior: list[tuple[str, float]], cause_id: str) -> float:
    """IG = H(先验) − [p⁺·H(阳后) + p⁻·H(阴后)]，p⁺ 用该根因的先验。"""
    if not prior:
        return 0.0
    base = _entropy([p for _, p in prior])
    p_pos = dict(prior).get(cause_id, 0.0)
    pos = posterior(prior, cause_id, True)
    neg = posterior(prior, cause_id, False)
    after = p_pos * _entropy([p for _, p in pos]) + (1 - p_pos) * _entropy([p for _, p in neg])
    return max(0.0, base - after)


def _confirmed_causes(store: GraphStore, check_id: str) -> list[str]:
    """CONFIRMED_BY 是 CAUSE → CHECK，所以要看入边。"""
    return [node.id for _edge, node in store.neighbors(check_id, ["CONFIRMED_BY"], "in", 50)]


def _cost(check: dict[str, Any]) -> float:
    risk = RISK_COST.get(str(check.get("riskLevel") or "low").lower(), 1.0)
    return max(1.0, risk * float(check.get("durationMin") or 1))


def ambiguity(diagnosis) -> tuple[bool, dict[str, Any]]:
    """首选根因置信度低或与次选差距小才算有歧义。"""
    conf = [float(c.get("confidence") or 0) for c in diagnosis.causes]
    top = conf[0] if conf else 0.0
    second = conf[1] if len(conf) > 1 else 0.0
    ambiguous = bool(conf) and (top < GATE_MIN_TOP or (top - second) < GATE_MAX_MARGIN)
    return ambiguous, {"top": round(top, 3), "second": round(second, 3)}


def candidate_checks(store: GraphStore, diagnosis) -> list[dict[str, Any]]:
    """把诊断里的检查项按 IG/成本 排序，附上它确认的根因。"""
    prior = prior_causes(diagnosis)
    if not prior:
        return []
    ranked: list[dict[str, Any]] = []
    for check in diagnosis.checks:
        causes = [c for c in _confirmed_causes(store, check["id"]) if c in dict(prior)]
        if not causes:
            continue
        cause_id = max(causes, key=lambda c: dict(prior)[c])
        ig = information_gain(prior, cause_id)
        cost = _cost(check)
        ranked.append({
            "checkId": check["id"], "name": check.get("name"), "method": check.get("method"),
            "causeId": cause_id, "faultId": next((c["faultId"] for c in diagnosis.causes
                                                  if c["id"] == cause_id), None),
            "ig": round(ig, 4), "cost": round(cost, 2), "score": round(ig / cost, 4),
            "riskLevel": check.get("riskLevel"), "durationMin": check.get("durationMin"),
        })
    ranked.sort(key=lambda r: -r["score"])
    return ranked


def should_ask(diagnosis, *, facts: list[dict] | None = None,
               asked: list[str] | None = None) -> tuple[bool, str]:
    """回答「该不该问」，返回 (是否追问, 原因码)。原因码进 trace，便于复盘误判。"""
    if getattr(diagnosis, "status", None) not in (None, "ok"):
        return False, "status_not_ok"
    policies = set(diagnosis.policies or [])
    if policies & NO_ASK_POLICIES:
        return False, "policy_precondition"
    if any(p.get("kind") in {"action", "check"} for p in (facts or [])):
        return False, "facts_already_known"
    ok, detail = ambiguity(diagnosis)
    if not ok:
        return False, f"unambiguous(top={detail['top']})"
    if not diagnosis.checks or not diagnosis.causes:
        return False, "no_candidates"
    return True, "ambiguous"


def question_text(check: dict[str, Any]) -> str:
    method = check.get("method") or check.get("name") or "这项检查"
    return f"为了进一步确认，能帮忙做一次「{check.get('name')}」吗？（{method}）结果怎么样？"


def plan_question(store: GraphStore, diagnosis, *, facts: list[dict] | None = None,
                  asked: list[str] | None = None) -> dict[str, Any] | None:
    """产出一条追问；不值得问时返回 None。结构直接进 API 与前端按钮。"""
    ok, reason = should_ask(diagnosis, facts=facts, asked=asked)
    if not ok:
        return None
    seen = set(asked or [])
    ranked = [c for c in candidate_checks(store, diagnosis) if c["checkId"] not in seen]
    ranked = [c for c in ranked if c["ig"] >= MIN_IG]
    if not ranked:
        return None
    best = ranked[0]
    return {
        "checkId": best["checkId"], "faultId": best["faultId"], "causeId": best["causeId"],
        "name": best["name"], "question": question_text(best),
        "options": [
            {"value": "positive", "label": "不正常 / 有异常"},
            {"value": "negative", "label": "正常 / 没发现异常"},
            {"value": "skip", "label": "不方便检查"},
        ],
        "reason": {"ig": best["ig"], "cost": best["cost"], "score": best["score"],
                   "gate": reason, "alternatives": [c["checkId"] for c in ranked[1:]]},
    }
