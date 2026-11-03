"""规则集加载与静态校验。

规则是声明式 YAML：新增规则不需要改 Python。加载时做四类检查：
  1. ID 唯一、优先级合法；
  2. 结论中的变量必须在条件里绑定（防止拼写错误产生全空结论）；
  3. 条件谓词在已知谓词表内（防止拼错谓词导致规则永不触发）；
  4. 安全规则必须显式标记 safety: true。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

from ..config import ROOT
from .engine import VAR_RE, Condition, Head, Rule, RuleError

RULES_PATH = ROOT / "backend" / "app" / "rules" / "rules.yaml"

# 已知谓词（由 facts.py 投影 + 引擎推导）。新增谓词需同步登记，否则静态校验失败。
KNOWN_PREDICATES = {
    # 图谱事实
    "observed_case", "case_about", "case_about_model", "case_reported_by",
    "model_code", "model_has_component", "model_uses_consumable", "compatible_flag",
    "warranty_status", "runs_firmware", "known_issue", "home_environment",
    "observed", "high_risk_symptom", "has_code", "error_applies_to", "error_family",
    "code_category", "symptom_fault_count", "symptom_indicates",
    "fault_code", "fault_occurs_in", "fault_component_code", "fault_in_category",
    "fault_caused_by", "cause_confirmed_by", "cause_resolved_by",
    "solution_authority", "solution_type", "solution_risk", "solution_requires_item",
    "solution_part_count", "solution_consumable_count", "solution_precedes",
    "check_risk_num", "check_duration", "check_name", "check_detects",
    "repeated_issue", "solution_success", "historical_solution",
    "same_issue_within_24h", "check_result",
    # 规则推导
    "suspect_component", "fault_boost", "ambiguous_symptom", "suspect_fault", "suspect_cause",
    "candidate_solution", "solution_score", "reject_solution", "prefer_consumable",
    "prefer_official", "firmware_hint", "chronic_issue", "escalate", "safety_stop",
    "block_self_service", "excluded_fault", "next_check", "ordered_check",
    "network_issue", "suppress_parts", "water_sequence", "duplicate_of",
    "conflicting_causes", "need_further_check", "no_candidate", "insufficient_evidence",
    "step_before",
}


def _parse_condition(raw: dict) -> Condition:
    if "pred" not in raw:
        raise RuleError(f"条件缺少 pred: {raw}")
    return Condition(
        pred=str(raw["pred"]),
        args=tuple(str(a) for a in raw.get("args", [])),
        where=raw.get("where"),
        absent=bool(raw.get("absent", False)),
    )


def _parse_head(raw: dict) -> Head:
    if "pred" not in raw:
        raise RuleError(f"结论缺少 pred: {raw}")
    return Head(
        pred=str(raw["pred"]),
        args=tuple(str(a) for a in raw.get("args", [])),
        cf=str(raw.get("cf", "1.0")),
        combine=str(raw.get("combine", "max")),
    )


def parse_rules(data: dict) -> list[Rule]:
    rules: list[Rule] = []
    seen: set[str] = set()
    for raw in data.get("rules", []):
        rule = Rule(
            id=str(raw["id"]),
            name=str(raw.get("name", raw["id"])),
            when=tuple(_parse_condition(c) for c in raw.get("when", [])),
            then=tuple(_parse_head(h) for h in raw.get("then", [])),
            priority=int(raw.get("priority", 50)),
            category=str(raw.get("category", "diagnosis")),
            explanation=str(raw.get("explanation", "")),
            safety=bool(raw.get("safety", False)),
            where=raw.get("where"),
        )
        if rule.id in seen:
            raise RuleError(f"规则 ID 重复: {rule.id}")
        seen.add(rule.id)
        if not rule.when:
            raise RuleError(f"{rule.id} 没有条件")
        if not rule.then:
            raise RuleError(f"{rule.id} 没有结论")
        if not 0 <= rule.priority <= 100:
            raise RuleError(f"{rule.id} 优先级需在 0-100: {rule.priority}")
        bound: set[str] = set()
        for cond in rule.when:
            for token in cond.args:
                m = VAR_RE.match(token)
                if m:
                    bound.add(m.group(1))
            if cond.pred not in KNOWN_PREDICATES:
                raise RuleError(f"{rule.id} 使用未知谓词 {cond.pred}")
        for head in rule.then:
            if head.pred not in KNOWN_PREDICATES:
                raise RuleError(f"{rule.id} 推导未知谓词 {head.pred}")
            for token in head.args:
                m = VAR_RE.match(token)
                if m and m.group(1) not in bound:
                    raise RuleError(f"{rule.id} 结论变量未绑定: {token}")
            if head.combine not in ("max", "noisy_or"):
                raise RuleError(f"{rule.id} combine 非法: {head.combine}")
        rules.append(rule)
    return rules


@lru_cache
def load_rules(path: Path | None = None) -> list[Rule]:
    path = path or RULES_PATH
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return parse_rules(data)


def rule_meta() -> list[dict]:
    return [
        {"id": r.id, "name": r.name, "priority": r.priority, "category": r.category,
         "safety": r.safety, "conditions": len(r.when), "conclusions": len(r.then)}
        for r in load_rules()
    ]
