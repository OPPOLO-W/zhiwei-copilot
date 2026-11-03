"""本体加载与访问。

schema.yaml 是唯一来源；本模块把它变成强类型对象，供校验、加载、抽取、前端使用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from ..config import ONTOLOGY_PATH


@dataclass(frozen=True, slots=True)
class ClassDef:
    name: str
    label: str
    prefix: str
    key: str
    definition: str
    required: bool
    props: dict[str, dict[str, Any]]
    viz: dict[str, str]
    parent: str | None = None
    abstract: bool = False
    example: dict[str, Any] = field(default_factory=dict)

    @property
    def required_props(self) -> list[str]:
        return [k for k, v in self.props.items() if v.get("required")]


@dataclass(frozen=True, slots=True)
class RelationDef:
    name: str
    label: str
    head: tuple[str, ...]
    tail: tuple[str, ...]
    desc: str
    card: str = ""
    min_per_type: int = 0
    symmetric: bool = False


@dataclass(frozen=True, slots=True)
class Ontology:
    version: str
    namespace: str
    product_line_scope: str
    classes: dict[str, ClassDef]
    relations: dict[str, RelationDef]
    gates: dict[str, Any]

    # ---- 查询 ----
    def class_of(self, name: str) -> ClassDef | None:
        return self.classes.get(name)

    def is_subclass(self, child: str, parent: str) -> bool:
        cur = self.classes.get(child)
        seen = 0
        while cur is not None and cur.parent and seen < 10:
            if cur.parent == parent:
                return True
            cur = self.classes.get(cur.parent)
            seen += 1
        return False

    def relation(self, name: str) -> RelationDef | None:
        return self.relations.get(name)

    def prefix_of(self, class_name: str) -> str:
        cls = self.classes.get(class_name)
        return cls.prefix if cls else class_name.upper()

    def color_map(self) -> dict[str, str]:
        return {name: c.viz.get("color", "#64748b") for name, c in self.classes.items()}

    def shape_map(self) -> dict[str, str]:
        return {name: c.viz.get("shape", "round-rectangle") for name, c in self.classes.items()}

    # ---- 机器可读视图（给 LLM / 校验器）----
    def relation_table(self) -> list[dict[str, Any]]:
        return [
            {
                "relation": r.name,
                "meaning": r.desc,
                "head": list(r.head),
                "tail": list(r.tail),
                "cardinality": r.card,
            }
            for r in self.relations.values()
        ]

    def class_table(self) -> list[dict[str, Any]]:
        return [
            {
                "type": c.name,
                "meaning": c.label,
                "definition": c.definition,
                "properties": {k: v.get("desc", "") for k, v in c.props.items()},
            }
            for c in self.classes.values()
            if not c.abstract
        ]

    def prompt_context(self, include: list[str] | None = None) -> str:
        """生成抽取 Prompt 所需的紧凑本体描述。

        include 给定时只保留相关类，以及与这些类相关的关系（文档 5.3 Step 3 的子集策略）。
        """
        classes = [c for c in self.class_table() if not include or c["type"] in include]
        allowed = {c["type"] for c in classes}
        rels = [
            r for r in self.relation_table()
            if not include or (set(r["head"]) & allowed and set(r["tail"]) & allowed)
        ]
        lines = ["【实体类型】"]
        for c in classes:
            lines.append(f"- {c['type']}（{c['meaning']}）：{c['definition']}")
        lines.append("\n【关系类型（domain -> range）】")
        for r in rels:
            lines.append(
                f"- {r['relation']}（{r['meaning']}）：{'|'.join(r['head'])} -> {'|'.join(r['tail'])}"
            )
        return "\n".join(lines)

    def json_schema_fragment(self) -> dict[str, Any]:
        return {
            "entityTypes": [c.name for c in self.classes.values() if not c.abstract],
            "relationTypes": list(self.relations.keys()),
            "relationSignatures": {r.name: {"head": list(r.head), "tail": list(r.tail)} for r in self.relations.values()},
        }


def load_ontology(path: Path | None = None) -> Ontology:
    path = path or ONTOLOGY_PATH
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    classes: dict[str, ClassDef] = {}
    for name, spec in raw["classes"].items():
        classes[name] = ClassDef(
            name=name,
            label=spec.get("label", name),
            prefix=spec.get("prefix", name.upper()),
            key=spec.get("key", "id"),
            definition=spec.get("definition", ""),
            required=spec.get("required", True),
            props=spec.get("props") or {},
            viz=spec.get("viz") or {},
            parent=spec.get("parent"),
            abstract=spec.get("abstract", False),
            example=spec.get("example") or {},
        )
    relations: dict[str, RelationDef] = {}
    for name, spec in raw["relations"].items():
        relations[name] = RelationDef(
            name=name,
            label=spec.get("label", name),
            head=tuple(spec.get("head", [])),
            tail=tuple(spec.get("tail", [])),
            desc=spec.get("desc", ""),
            card=spec.get("card", ""),
            min_per_type=int(spec.get("minPerType", 0)),
            symmetric=bool(spec.get("symmetric", False)),
        )
    # 本体自检：关系两端必须是已定义类，父类必须存在
    missing = {
        r.name: [c for c in (*r.head, *r.tail) if c not in classes] for r in relations.values()
    }
    missing = {k: v for k, v in missing.items() if v}
    if missing:
        raise ValueError(f"本体关系引用了未定义类: {missing}")
    for c in classes.values():
        if c.parent and c.parent not in classes:
            raise ValueError(f"类 {c.name} 的父类 {c.parent} 未定义")

    return Ontology(
        version=str(raw.get("version", "0")),
        namespace=raw.get("namespace", ""),
        product_line_scope=raw.get("productLineScope", ""),
        classes=classes,
        relations=relations,
        gates=raw.get("qualityGates") or {},
    )


@lru_cache
def get_ontology() -> Ontology:
    return load_ontology()
