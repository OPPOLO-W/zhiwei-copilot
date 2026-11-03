"""规则集：数量门槛、谓词白名单、变量绑定与优先级。"""

from __future__ import annotations

from pathlib import Path

from backend.app.rules.engine import Engine
from backend.app.rules.loader import KNOWN_PREDICATES, load_rules, parse_rules

ROOT = Path(__file__).resolve().parents[2]


def test_rule_count_meets_course_gate():
    rules = load_rules()
    assert len(rules) >= 20, "课程要求 ≥20 条推理规则"
    assert len({r.id for r in rules}) == len(rules), "规则 ID 必须唯一"


def test_rule_predicates_are_whitelisted():
    rules = load_rules()
    for rule in rules:
        for cond in rule.when:
            assert cond.pred in KNOWN_PREDICATES, f"{rule.id} 使用了未登记谓词 {cond.pred}"
        for head in rule.then:
            assert head.pred in KNOWN_PREDICATES, f"{rule.id} 推导出未登记谓词 {head.pred}"


def test_negative_body_uses_absent():
    """分层求值的前提：否定条件必须以 absent 显式声明，不能靠「查不到就当成立」。"""
    rules = load_rules()
    negative = [r for r in rules if any(c.absent for c in r.when)]
    assert negative, "至少要有规则使用 negation as failure"
    for rule in negative:
        assert all(c.pred in KNOWN_PREDICATES for c in rule.when)


def test_invalid_rule_is_rejected(tmp_path):
    bad = tmp_path / "rules.yaml"
    bad.write_text(
        """
rules:
  - id: R99
    name: 非法谓词
    priority: 10
    when:
      - {pred: not_a_predicate, args: ["?X"]}
    then:
      - {pred: suspect_fault, args: ["?X"]}
""",
        encoding="utf-8",
    )
    import yaml

    try:
        parse_rules(yaml.safe_load(bad.read_text(encoding="utf-8")))
    except ValueError as exc:  # RuleError 是 ValueError 子类
        assert "not_a_predicate" in str(exc)
    else:  # pragma: no cover - 校验失效才走到这里
        raise AssertionError("非法谓词应被拒绝")


def test_engine_saturates_and_reports_iterations():
    from backend.app.rules.engine import Atom

    engine = Engine(load_rules(), max_iterations=10, max_depth=6)
    atoms: list[Atom] = [
        ("observed", "CASE:QUERY", "SYM:LEFT-WHEEL-STUCK"),
        ("case_about_model", "CASE:QUERY", "MODEL:T30-PRO"),
        ("symptom_indicates", "SYM:LEFT-WHEEL-STUCK", "FAULT:LEFT-WHEEL-STUCK", 0.82),
        ("symptom_fault_count", "SYM:LEFT-WHEEL-STUCK", 1),
    ]
    result = engine.run(atoms)
    assert result.iterations >= 1
    assert result.query("suspect_fault"), "应推导出候选故障"
    assert result.fired_rules, "应记录触发过的规则"
    assert "R03" in result.fired_rules, "症状→故障规则应被触发"


def test_rules_yaml_is_the_single_source():
    text = (ROOT / "backend/app/rules/rules.yaml").read_text(encoding="utf-8")
    assert "explanation" in text, "每条规则都要有自然语言解释，供可解释性展示"
