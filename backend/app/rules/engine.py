"""正向链式推理引擎（Datalog 风格），支持证明树与置信度传播。

设计取舍
--------
* 事实是不可变的 `Atom = (pred, arg1, ...)`，全部参数以字符串存储，便于哈希与序列化。
* 规则为声明式 YAML，变量用 `?X` 前缀，避免与常量混淆。
* 否定采用「否定即失败」（negation as failure），即闭世界假设；`absent: true` 的条件
  以已绑定变量做**前缀匹配**，用于表达「不存在任何候选方案」这类判断。
* 置信度是启发式分数，不是校准概率；规则可声明 `combine: noisy_or` 表示独立证据合并，
  默认取最大值（同一结论的多条推导不重复加分）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator

VAR_RE = re.compile(r"^\?([A-Za-z_][A-Za-z0-9_]*)$")
CF_VAR_RE = re.compile(r"^\?CF[A-Za-z0-9_]*$")
NUM_RE = re.compile(r"^-?\d+(\.\d+)?$")

Atom = tuple[str, ...]


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class Condition:
    pred: str
    args: tuple[str, ...] = ()
    where: str | None = None
    absent: bool = False


@dataclass(frozen=True)
class Head:
    pred: str
    args: tuple[str, ...]
    cf: str = "1.0"          # 字面量、变量名或表达式
    combine: str = "max"     # max | noisy_or


@dataclass(frozen=True)
class Rule:
    id: str
    name: str
    when: tuple[Condition, ...]
    then: tuple[Head, ...]
    priority: int = 50
    category: str = "diagnosis"
    explanation: str = ""
    safety: bool = False
    where: str | None = None    # 绑定完成后的整体约束（用于不等式、跨变量比较）

    @property
    def specificity(self) -> int:
        return len(self.when)


@dataclass
class Derivation:
    rule_id: str
    rule_name: str
    premises: tuple[Atom, ...]
    cf: float
    explanation: str = ""
    category: str = "diagnosis"


@dataclass
class FactInfo:
    cf: float = 0.0
    derivations: list[Derivation] = field(default_factory=list)
    source: bool = False

    @property
    def primary(self) -> Derivation | None:
        return self.derivations[0] if self.derivations else None


@dataclass
class InferenceResult:
    facts: dict[Atom, FactInfo]
    iterations: int
    fired_rules: dict[str, int]

    def query(self, pred: str) -> list[Atom]:
        return sorted(a for a in self.facts if a[0] == pred)

    def of(self, pred: str) -> list[tuple[Atom, FactInfo]]:
        return [(a, self.facts[a]) for a in self.query(pred)]

    def cf(self, atom: Atom) -> float:
        info = self.facts.get(atom)
        return info.cf if info else 0.0

    def has(self, atom: Atom) -> bool:
        return atom in self.facts


class RuleError(ValueError):
    pass


class Engine:
    def __init__(self, rules: Iterable[Rule], max_iterations: int = 1000, max_depth: int = 8) -> None:
        self.rules = sorted(rules, key=lambda r: (-r.priority, r.id))
        self.max_iterations = max_iterations
        self.max_depth = max_depth

    # ---------- 匹配 ----------
    @staticmethod
    def _unify(atom: Atom, pattern: tuple[str, ...], binding: dict[str, str]) -> dict[str, str] | None:
        if len(atom) - 1 != len(pattern):
            return None
        new = dict(binding)
        for actual, token in zip(atom[1:], pattern):
            m = VAR_RE.match(token)
            if m:
                name = m.group(1)
                if name in new:
                    if new[name] != actual:
                        return None
                else:
                    new[name] = actual
            elif token != actual:
                return None
        return new

    @staticmethod
    def _eval(expr: str, binding: dict[str, str]) -> Any:
        env: dict[str, Any] = {}
        for key, value in binding.items():
            num = _num(value)
            env[key] = num if num is not None else value
        env.update({"min": min, "max": max, "abs": abs, "len": len})
        try:
            return eval(expr, {"__builtins__": {}}, env)  # noqa: S307 - 规则文件为本地可信配置
        except Exception as exc:  # noqa: BLE001
            raise RuleError(f"表达式求值失败: {expr!r} -> {exc}") from exc

    def _match(
        self,
        conds: tuple[Condition, ...],
        facts: dict[Atom, FactInfo],
        binding: dict[str, str],
        idx: int = 0,
    ) -> Iterator[dict[str, str]]:
        if idx == len(conds):
            yield binding
            return
        cond = conds[idx]
        if cond.absent:
            prefix = tuple(binding.get(t[1:], t) if VAR_RE.match(t) else t for t in cond.args)
            exists = any(
                atom[0] == cond.pred and atom[1:1 + len(prefix)] == prefix
                for atom in facts
            )
            if not exists:
                yield from self._match(conds, facts, binding, idx + 1)
            return
        pattern = cond.args
        # 置信度伪变量：以 CF 开头的末位变量（?CF、?CFS、?CF1…）绑定到事实的置信度。
        # 仅当谓词本身没有同数量的显式参数时才启用，因此
        # `symptom_indicates(S, F, CF)` 这种「CF 是真实参数」的谓词不受影响。
        cf_name = None
        if pattern and CF_VAR_RE.match(pattern[-1]):
            cf_name = pattern[-1][1:]
        for atom in facts:
            if atom[0] != cond.pred:
                continue
            if cf_name and len(atom) == len(pattern):
                bound = self._unify(atom, pattern[:-1], binding)
                if bound is None:
                    continue
                bound = {**bound, cf_name: str(facts[atom].cf)}
            elif len(atom) - 1 == len(pattern):
                bound = self._unify(atom, pattern, binding)
                if bound is None:
                    continue
            else:
                continue
            if cond.where and not self._eval(cond.where, bound):
                continue
            yield from self._match(conds, facts, bound, idx + 1)

    def _instantiate(self, head: Head, binding: dict[str, str]) -> Atom:
        args = []
        for token in head.args:
            if token.startswith("$"):          # $ 前缀表示表达式参数，如 "$R * 100 + D"
                value = self._eval(token[1:], binding)
                if isinstance(value, float) and value.is_integer():
                    value = int(value)
                args.append(str(value))
                continue
            m = VAR_RE.match(token)
            if m:
                if m.group(1) not in binding:
                    raise RuleError(f"规则结论变量未绑定: {token}")
                args.append(binding[m.group(1)])
            else:
                args.append(token)
        return (head.pred, *args)

    def _cf(self, head: Head, binding: dict[str, str]) -> float:
        token = head.cf.strip()
        if NUM_RE.match(token):
            value = float(token)
        elif VAR_RE.match(token):
            value = _num(binding.get(VAR_RE.match(token).group(1)))  # type: ignore[union-attr]
            value = value if value is not None else 0.0
        else:
            value = _num(self._eval(token, binding))
            value = value if value is not None else 0.0
        return round(max(0.0, min(0.99, value)), 4)

    # ---------- 推理 ----------
    def run(self, source_atoms: Iterable[Atom]) -> InferenceResult:
        """分层求值：先对「无否定条件」的规则求不动点，再求值否定规则。

        否则 `absent` 会在候选尚未推导出来时提前成立（例如 R24 误判「没有方案」）。
        """
        facts: dict[Atom, FactInfo] = {}
        for atom in source_atoms:
            facts[atom] = FactInfo(cf=1.0, source=True)
        fired: dict[str, int] = {}
        iterations = 0

        positive = [r for r in self.rules if not any(c.absent for c in r.when)]
        negative = [r for r in self.rules if any(c.absent for c in r.when)]
        for _stratum in range(3):
            iterations += self._saturate(positive, facts, fired)
            added = self._saturate(negative, facts, fired, single_pass=True)
            if added == 0:
                break
        return InferenceResult(facts=facts, iterations=max(1, iterations), fired_rules=fired)

    def _saturate(
        self,
        rules: list[Rule],
        facts: dict[Atom, FactInfo],
        fired: dict[str, int],
        single_pass: bool = False,
    ) -> int:
        iterations = 0
        added_total = 0

        while iterations < self.max_iterations:
            iterations += 1
            snapshot = dict(facts)
            new_facts: list[tuple[Atom, Derivation, Head]] = []

            for rule in rules:
                for binding in self._match(rule.when, snapshot, {}):
                    if rule.where and not self._eval(rule.where, binding):
                        continue
                    premises = tuple(
                        self._premise_atom(cond, binding) for cond in rule.when if not cond.absent
                    )
                    for head in rule.then:
                        atom = self._instantiate(head, binding)
                        if atom in snapshot and snapshot[atom].source:
                            continue
                        cf = self._cf(head, binding)
                        deriv = Derivation(
                            rule_id=rule.id,
                            rule_name=rule.name,
                            premises=premises,
                            cf=cf,
                            explanation=self._render(rule.explanation, binding),
                            category=rule.category,
                        )
                        new_facts.append((atom, deriv, head))

            changed = False
            for atom, deriv, head in new_facts:
                info = facts.get(atom)
                if info is None:
                    facts[atom] = FactInfo(cf=deriv.cf, derivations=[deriv])
                    fired[deriv.rule_id] = fired.get(deriv.rule_id, 0) + 1
                    changed = True
                    continue
                if any(d.rule_id == deriv.rule_id and d.premises == deriv.premises
                       for d in info.derivations):
                    continue
                info.derivations.append(deriv)
                fired[deriv.rule_id] = fired.get(deriv.rule_id, 0) + 1
                if head.combine == "noisy_or":
                    new_cf = 1 - (1 - info.cf) * (1 - deriv.cf)
                    new_cf = round(min(0.99, new_cf), 4)
                else:
                    new_cf = max(info.cf, deriv.cf)
                if new_cf > info.cf:
                    info.cf = new_cf
                    changed = True

            added_total += sum(1 for a in facts if a not in snapshot)
            if single_pass or not changed:
                break

        return added_total

    @staticmethod
    def _premise_atom(cond: Condition, binding: dict[str, str]) -> Atom:
        args = []
        for token in cond.args:
            m = VAR_RE.match(token)
            args.append(binding.get(m.group(1), token) if m else token)
        return (cond.pred, *args)

    @staticmethod
    def _render(template: str, binding: dict[str, str]) -> str:
        if not template:
            return ""
        out = template
        for key, value in binding.items():
            # 绑定值可能是数值（如置信度 ?CF），统一转成字符串再替换，
            # 否则 str.replace 会抛 TypeError 打断整轮推理。
            out = out.replace("{" + key + "}", str(value))
        return out

    # ---------- 证明树 ----------
    def proof_tree(self, result: InferenceResult, atom: Atom, depth: int = 0) -> dict[str, Any]:
        info = result.facts.get(atom)
        node: dict[str, Any] = {
            "fact": "(" + ", ".join(atom) + ")",
            "pred": atom[0],
            "args": list(atom[1:]),
            "cf": round(info.cf, 3) if info else 0.0,
            "source": bool(info and info.source),
        }
        if not info or not info.derivations or depth >= self.max_depth:
            return node
        deriv = info.derivations[0]
        node.update({
            "ruleId": deriv.rule_id,
            "ruleName": deriv.rule_name,
            "explanation": deriv.explanation,
            "premises": [self.proof_tree(result, p, depth + 1) for p in deriv.premises],
        })
        return node
