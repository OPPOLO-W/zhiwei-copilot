"""由 `data/ontology/schema.yaml` 生成 `docs/ontology.md`。

用法：
    python -m backend.tools.gen_ontology_doc            # 生成
    python -m backend.tools.gen_ontology_doc --check    # 只校验文档是否与本体同步

为什么生成而不是手写：本体是唯一事实源（schema.yaml 自己就写着「docs/ontology.md
由本文件生成」）。手写一份表，改本体时必然漂移；生成一份，`--check` 能直接进 CI。
实例数从 `data/curated/{nodes,edges}.jsonl` 统计，缺文件时显示「—」。
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = ROOT / "data" / "ontology" / "schema.yaml"
OUT = ROOT / "docs" / "ontology.md"
NODES = ROOT / "data" / "curated" / "nodes.jsonl"
EDGES = ROOT / "data" / "curated" / "edges.jsonl"


def _counts(path: Path, key: str) -> Counter:
    c: Counter = Counter()
    if not path.exists():
        return c
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        c[json.loads(line)[key]] += 1
    return c


def _inst(n: int | None) -> str:
    return "—" if n is None else str(n)


def render(schema: dict[str, Any], node_counts: Counter, edge_counts: Counter) -> str:
    classes = schema["classes"]
    relations = schema["relations"]
    gates = schema.get("qualityGates") or {}
    cqs = schema.get("competencyQuestions") or []
    have_counts = bool(node_counts)

    total_nodes = sum(node_counts.values()) if have_counts else None
    total_edges = sum(edge_counts.values()) if have_counts else None

    lines: list[str] = []
    add = lines.append

    add("# 领域本体说明（TBox）")
    add("")
    add("> 本文件由 `python -m backend.tools.gen_ontology_doc` 从")
    add("> [`data/ontology/schema.yaml`](../data/ontology/schema.yaml) 生成，**请勿手改**。")
    add("> 改本体 → 重跑生成命令 → 提交两份 diff，文档与实现不会漂移。")
    add("")

    add("## 1. 表示方式")
    add("")
    add("| 项目 | 约定 |")
    add("| --- | --- |")
    add(f"| 本体版本 | `{schema.get('version', '')}` |")
    add(f"| 命名空间 | `{schema.get('namespace', '')}` |")
    add(f"| 适用范围 | `productLineScope = {schema.get('productLineScope', '')}`（导入时强制过滤，防范围蔓延） |")
    add("| TBox / ABox | 类型与关系定义在本 YAML；实例三元组存 Neo4j 属性图 |")
    add("| 类 → 标签 | 类名即节点标签，统一附加 `:Entity`；子类用多标签（`:Entity:Case:FAQCase`） |")
    add("| 对象属性 → 关系 | 关系名 UPPER_SNAKE_CASE；数据属性 → 节点属性 camelCase |")
    add("| 实体 ID | `前缀:业务键`，如 `MODEL:T20-PRO`、`ERR:T20-PRO:E11`、`FAULT:NO-SUCTION` |")
    add("| 推理语义 | RDFS/OWL-lite 子类继承 + 声明式规则 + 闭世界假设（negation as failure） |")
    add("")
    add("错误码采用「型号 × 错误码」复合键（`ERR:<MODEL>:<CODE>`），而不是全局 `ERR:<CODE>`：")
    add("同一个 `E11` 在不同型号上可以是不同部件，跨型号复用是售后知识图谱最常见的语义错误。")
    add("故障（`Fault`）、根因（`Cause`）等跨型号共享的实体则使用单键（`FAULT:*` / `CAUSE:*`），")
    add("由 `OCCURS_IN` 指回型号自己的部件。")
    add("")

    add("## 2. 类（TBox）")
    add("")
    add(f"共 **{len(classes)}** 个类（20 个核心类 + `Case` 的 3 个子类）。")
    if have_counts:
        add(f"当前图谱共 **{total_nodes}** 个实体实例。")
    add("")
    add("| 类 | 中文 | ID 前缀 | 业务唯一键 | 必填 | 实例数 | 定义 |")
    add("| --- | --- | --- | --- | --- | ---: | --- |")
    for name, spec in classes.items():
        add("| `{}` | {} | `{}` | `{}` | {} | {} | {} |".format(
            name, spec.get("label", ""), spec.get("prefix", ""), spec.get("key", ""),
            "是" if spec.get("required") else "否",
            _inst(node_counts.get(name, 0) if have_counts else None),
            spec.get("definition", "").replace("|", "\\|"),
        ))
    add("")
    add("`FAQCase` / `PublicCase` / `Ticket` 是 `Case` 的子类，继承 `caseNo`、`sourceType`、")
    add("`createdAt` 等属性，并各自追加来源特有字段（如 `PublicCase.platform`、`Ticket.channel`）。")
    add("按 `Case` 统一查询能一次拿到三类案例，按子类查询可以只取官方口径或真实报修。")
    add("")
    add("`Case` 是抽象超类，实例都带子类标签，因此它自己的计数为 0——不是缺数据，")
    add("而是多标签建模的必然结果。")
    add("")

    add("## 3. 关系（TBox）")
    add("")
    add(f"共 **{len(relations)}** 条关系。`head` / `tail` 即 domain / range，校验器逐条比对；")
    add("`minPerType` 是该关系在每种头实体类型上至少要出现的条数（防止「建了关系但图中没边」）。")
    if have_counts:
        add(f"当前图谱共 **{total_edges}** 条关系实例。")
    add("")
    add("| 关系 | 中文 | 头实体 | 尾实体 | 基数 | 实例数 | 语义 |")
    add("| --- | --- | --- | --- | --- | ---: | --- |")
    for name, spec in relations.items():
        add("| `{}` | {} | {} | {} | `{}` | {} | {} |".format(
            name, spec.get("label", ""),
            " / ".join(f"`{h}`" for h in spec.get("head", [])),
            " / ".join(f"`{t}`" for t in spec.get("tail", [])),
            spec.get("card", "*"),
            _inst(edge_counts.get(name, 0) if have_counts else None),
            spec.get("desc", "").replace("|", "\\|"),
        ))
    add("")
    add("### 3.1 推理主链")
    add("")
    add("```text")
    add("Symptom ──INDICATES──▶ Fault ──CAUSED_BY──▶ Cause")
    add("   ▲                    │                    ├──CONFIRMED_BY──▶ Check")
    add("   │                    │                    └──RESOLVED_BY───▶ Solution")
    add("   │                    └──OCCURS_IN──▶ Component        ├──REQUIRES_CONSUMABLE──▶ Consumable")
    add("ErrorCode ──HAS_CODE(反)──┘                             ├──REQUIRES_PART────────▶ SparePart")
    add("                                                         └──DOCUMENTED_IN────────▶ Document")
    add("```")
    add("")
    add("这条链就是规则层的证据链：LHS 取自链上的边，RHS 也是链上的边，")
    add("所以任一诊断结论都能反查到具体三元组，再由 `sourceId` 落到原文。")
    add("")

    add("## 4. 能力问题（CQ）")
    add("")
    add("CQ 是本体「够不够用」的验收口径：每个 CQ 必须有不绕路的查询能回答。")
    add("")
    add("| 编号 | 能力问题 | 涉及路径 | 实现入口 |")
    add("| --- | --- | --- | --- |")
    for cq in cqs:
        add("| {} | {} | {} | {} |".format(
            cq.get("id", ""), cq.get("question", ""), cq.get("path", ""), cq.get("query", "")))
    add("")

    add("## 5. 质量闸门")
    add("")
    add("`backend/app/kg/validate.py` 在每次构建后跑 9 类检查，任一条不过即视为构建失败：")
    add("")
    add("| 检查 | 内容 | 阈值 |")
    add("| --- | --- | --- |")
    add("| 类型合法性 | 节点标签、关系类型都在本体中声明 | 0 违规 |")
    add("| domain / range | 每条边的 head / tail 类型落在声明范围内 | 0 违规 |")
    add(f"| 规模 | 实体 / 关系数 | ≥ {gates.get('minEntities', 150)} / ≥ {gates.get('minRelations', 300)} |")
    add(f"| 课程底线 | 同一指标的课程最低要求 | ≥ {gates.get('minEntitiesCourse', 60)} / ≥ {gates.get('minRelationsCourse', 120)} |")
    add("| 证据完整性 | 每条边都有 `evidence` + `sourceId` | ≥ " + f"{gates.get('minEvidenceCoverage', 1.0):.0%}" + " |")
    add("| 唯一键 | 同类实体的业务键不重复 | 0 重复 |")
    add("| 自环 | 头尾相同（`SIMILAR_TO` 除外） | 0 条 |")
    add("| 孤立点 | 没有任何边的节点 | 0 个 |")
    add("| 断链 | 边的端点不存在 | 0 条 |")
    add("")
    add("## 6. 与 Neo4j 的对应")
    add("")
    add("| 本体元素 | Neo4j 落地 |")
    add("| --- | --- |")
    add("| 类 | 节点标签 + 唯一约束（`CREATE CONSTRAINT ... REQUIRE n.<key> IS UNIQUE`） |")
    add("| 关系 | 关系类型 + 属性（`confidence` / `evidence` / `sourceId` / `evidenceMode`） |")
    add("| 型号维度 | 每型号一节点，错误码挂 `ERR:<MODEL>:<CODE>`，约束保证不跨型号复用 |")
    add("| 查询 | 只读账号；Cypher 由 `kg/queries.py` 集中维护，LLM 不生成 Cypher |")
    add("")
    add("约束与索引的 DDL 见 `backend/app/kg/schema.cypher`。")
    add("")

    return "\n".join(lines).rstrip() + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="生成 docs/ontology.md")
    parser.add_argument("--check", action="store_true", help="只检查文档是否过期")
    args = parser.parse_args()

    schema = yaml.safe_load(SCHEMA.read_text(encoding="utf-8"))
    text = render(schema, _counts(NODES, "label"), _counts(EDGES, "type"))

    if args.check:
        current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if current != text:
            raise SystemExit("docs/ontology.md 与 schema.yaml 不同步，请重跑生成命令")
        print("docs/ontology.md 已与本体同步")
        return

    OUT.write_text(text, encoding="utf-8")
    print(f"已生成 {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
