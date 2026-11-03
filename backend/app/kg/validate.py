"""图谱质量校验：本体一致性、证据完整性、规模门槛、孤立点。

用法：
    python -m backend.app.kg.validate            # 校验 data/curated
    python -m backend.app.kg.validate --strict   # 有 error 时退出码 1
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from ..config import CURATED_DIR
from .model import Graph
from .ontology import Ontology, get_ontology

# ErrorCode 的业务键在实际图中按型号重复（ERR:<model>:<code>），唯一性由 id 保证
UNIQUE_BY_ID_ONLY = {"ErrorCode", "AppVersion", "Case", "FAQCase", "PublicCase", "Ticket"}


@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    node_counts: dict[str, int] = field(default_factory=dict)
    edge_counts: dict[str, int] = field(default_factory=dict)
    n_nodes: int = 0
    n_edges: int = 0
    evidence_coverage: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.errors


def validate(graph: Graph, onto: Ontology) -> Report:
    rep = Report()
    rep.n_nodes = len(graph)
    rep.n_edges = len(graph.edges())

    # 1. 类型白名单
    for node in graph.nodes():
        if node.label not in onto.classes:
            rep.errors.append(f"未知节点类型 {node.label}: {node.id}")
        for extra in node.extra_labels:
            if extra not in onto.classes:
                rep.errors.append(f"未知附加标签 {extra}: {node.id}")
    for edge in graph.edges():
        if edge.type not in onto.relations:
            rep.errors.append(f"未知关系类型 {edge.type}: {edge.id}")

    # 2. domain / range（构建期已校验，这里针对外部导入/人工修改再兜底）
    for edge in graph.edges():
        rdef = onto.relations.get(edge.type)
        if not rdef:
            continue
        h, t = graph.node(edge.head), graph.node(edge.tail)
        if not h or not t:
            rep.errors.append(f"悬空边 {edge.id}")
            continue
        if not (h.label in rdef.head or any(onto.is_subclass(h.label, p) for p in rdef.head)):
            rep.errors.append(f"{edge.type} domain 违规: {h.label} ({edge.head})")
        if not (t.label in rdef.tail or any(onto.is_subclass(t.label, p) for p in rdef.tail)):
            rep.errors.append(f"{edge.type} range 违规: {t.label} ({edge.tail})")

    # 3. 自环
    for edge in graph.edges():
        if edge.head == edge.tail:
            rep.errors.append(f"自环: {edge.id}")

    # 4. 必填属性
    for node in graph.nodes():
        cls = onto.classes.get(node.label)
        if not cls:
            continue
        for prop in cls.required_props:
            if prop not in node.props or node.props[prop] in (None, ""):
                rep.errors.append(f"{node.label} {node.id} 缺少必填属性 {prop}")

    # 5. 证据与来源完整性
    missing = 0
    for edge in graph.edges():
        p = edge.props
        if not p.get("sourceId") or not p.get("evidence"):
            missing += 1
            if missing <= 5:
                rep.errors.append(f"关系缺少证据/来源: {edge.id}")
        cf = p.get("confidence")
        if cf is None or not (0.0 <= float(cf) <= 1.0):
            rep.errors.append(f"关系置信度非法: {edge.id} -> {cf}")
    rep.evidence_coverage = 1.0 - (missing / rep.n_edges if rep.n_edges else 0)

    # 6. 业务键唯一性
    for name, cls in onto.classes.items():
        if cls.abstract or name in UNIQUE_BY_ID_ONLY:
            continue
        seen: dict[object, str] = {}
        for node in graph.nodes(name):
            key = node.props.get(cls.key)
            if key is None:
                continue
            if key in seen:
                rep.errors.append(f"{name}.{cls.key} 重复: {key} ({seen[key]} / {node.id})")
            seen[key] = node.id

    # 7. 孤立点
    degree: Counter[str] = Counter()
    for edge in graph.edges():
        degree[edge.head] += 1
        degree[edge.tail] += 1
    orphans = [n.id for n in graph.nodes() if degree[n.id] == 0]
    if orphans:
        rep.errors.append(f"存在 {len(orphans)} 个孤立节点: {orphans[:8]}")

    # 8. 规模与覆盖门槛
    gates = onto.gates
    if rep.n_nodes < int(gates.get("minEntities", 150)):
        rep.errors.append(f"实体数 {rep.n_nodes} < 门槛 {gates.get('minEntities')}")
    if rep.n_edges < int(gates.get("minRelations", 300)):
        rep.errors.append(f"关系数 {rep.n_edges} < 门槛 {gates.get('minRelations')}")

    rep.node_counts = dict(sorted(Counter(n.label for n in graph.nodes()).items(), key=lambda kv: -kv[1]))
    rep.edge_counts = dict(sorted(Counter(e.type for e in graph.edges()).items(), key=lambda kv: -kv[1]))

    for name, cls in onto.classes.items():
        if cls.abstract:
            continue
        if cls.required and rep.node_counts.get(name, 0) == 0:
            rep.errors.append(f"类 {name} 没有实例")

    for name, rdef in onto.relations.items():
        cnt = rep.edge_counts.get(name, 0)
        if rdef.min_per_type and cnt < rdef.min_per_type:
            rep.warnings.append(f"关系 {name} 仅 {cnt} 条 (< 建议 {rdef.min_per_type})")

    # 9. 合成数据占比提示
    synthetic = sum(1 for n in graph.nodes("Case") if n.props.get("dataProvenance") == "seed_generated")
    if synthetic:
        rep.warnings.append(f"{synthetic} 个案例为模板合成数据（faq_template_proxy / synthetic），需替换或标注")
    pending = sum(1 for e in graph.edges() if e.props.get("verification") == "pending")
    if pending:
        rep.warnings.append(f"{pending} 条关系的证据为改写摘要，尚未对照官方原文核对（verification=pending）")

    return rep


def _fmt_counts(counts: dict[str, int], limit: int = 40) -> str:
    items = list(counts.items())[:limit]
    width = max((len(k) for k, _ in items), default=10)
    return "\n".join(f"  {k.ljust(width)}  {v:>5}" for k, v in items)


def print_report(rep: Report, out_dir: Path | None = None) -> None:
    print("\n" + "=" * 64)
    print("图谱质量报告")
    print("=" * 64)
    if out_dir:
        print(f"数据目录: {out_dir}")
    print(f"实体: {rep.n_nodes}   关系: {rep.n_edges}   证据覆盖率: {rep.evidence_coverage:.1%}")
    print("\n[实体类型分布]")
    print(_fmt_counts(rep.node_counts))
    print("\n[关系类型分布]")
    print(_fmt_counts(rep.edge_counts))
    if rep.errors:
        print(f"\n[ERROR] {len(rep.errors)} 项")
        for e in rep.errors[:25]:
            print("  -", e)
        if len(rep.errors) > 25:
            print(f"  ... 其余 {len(rep.errors) - 25} 项省略")
    if rep.warnings:
        print(f"\n[WARN] {len(rep.warnings)} 项")
        for w in rep.warnings:
            print("  -", w)
    print("\n结论:", "通过" if rep.ok else "未通过，请修复 ERROR 后重试")
    print("=" * 64 + "\n")


def summarize(graph: Graph, builder_stats: dict[str, int], case_stats: dict[str, int], out_dir: Path) -> None:
    rep = validate(graph, get_ontology())
    print(f"\n已写入 {out_dir}")
    print("构建统计:", {**builder_stats, **case_stats})
    print_report(rep, out_dir)


def main() -> None:
    parser = argparse.ArgumentParser(description="校验知识图谱")
    parser.add_argument("--dir", type=Path, default=CURATED_DIR)
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()
    graph = Graph.read_jsonl(args.dir)
    rep = validate(graph, get_ontology())
    print_report(rep, args.dir)
    if args.strict and not rep.ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
