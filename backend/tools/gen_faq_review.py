"""生成/校验 FAQ 案例审核台账（data/review/faq_cases.yaml）。

为什么需要这一层
----------------
抽取层产出的是**候选**，不是事实。58 条官方 FAQ 抽取结果里混着三类东西：
  * 故障排查（有现象、有处置）—— 真正的案例卡；
  * 保养维护（如何清理边刷、换尘袋）—— 没有现象，但有可对齐的处置方案；
  * 纯功能教程（如何下载 APP、如何设置预约）—— 既无现象也无处置，属文档而非案例。

把三类一律灌进 Case 层，会让「案例」这个概念失效，也会污染相似案例检索。
所以入库前必须有一步人工定论，且定论要落盘可追溯——这就是本台账。

脚本只做两件事，**不做判定**：
  1. `--init`：从 candidates.jsonl 拉出骨架，附机器对齐参考（仅作参考，不写进 decision）；
  2. `--check`：校验台账与候选是否一一对应、引用 ID 是否存在、accepted 条目字段是否齐全。

判定（kind / symptom / solution / decision）由人填写，写入台账。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
CANDIDATES = ROOT / "data" / "extracted" / "candidates.jsonl"
SEED = ROOT / "data" / "seed" / "t20pro" / "02_knowledge.yaml"
REVIEW = ROOT / "data" / "review" / "faq_cases.yaml"

HEADER = """# 官方 FAQ 案例审核台账
#
# 流程（开发文档 5.3）：抽取候选 → 人工定论（本文件）→ accepted 才进图
# 来源：data/extracted/candidates.jsonl 中 extractor=faq/case 的 58 条，
#       原文与 URL 均来自官网 T20 PRO 常见问题页，evidence 为逐字引用。
#
# kind 判定口径
#   troubleshoot  有「现象 → 处置」结构，可对齐本体症状与方案，是真正的案例卡
#   maintenance   无现象但有可对齐的保养/更换动作（CLEAN-* / REPLACE-*），作处置卡
#   usage         纯功能设置/使用教程，既无现象也无处置，已有 Document 节点承载，不进 Case 层
#
# decision 取值：accepted（进图）/ rejected（不进图，理由写 note）
# symptom / solution 必须引用种子中真实存在的 code，写 null 表示不对齐。
# 不对齐比错对齐好：一条错边会污染推理链，缺一条边只会少一条证据。

meta:
  product: T20 PRO
  source: data/extracted/candidates.jsonl
  extractor: faq/case
  reviewedAt: "2026-10-29"
  reviewer: zhiwei
  rule: 只有能对齐本体症状或方案的官方 FAQ 才进 Case 层；纯教程留在 Document 层

cases:
"""


def load_candidates() -> list[dict[str, Any]]:
    rows = []
    with CANDIDATES.open(encoding="utf-8") as fh:
        for line in fh:
            d = json.loads(line)
            if d["kind"] == "node" and d["label"] == "FAQCase":
                rows.append(d)
    rows.sort(key=lambda r: r["key"])
    return rows


def seed_index() -> tuple[dict[str, dict], dict[str, dict]]:
    data = yaml.safe_load(SEED.read_text(encoding="utf-8"))
    symptoms = {s["code"]: s for s in data["symptoms"]}
    solutions = {s["code"]: s for s in data["solutions"]}
    return symptoms, solutions


def hint_for(text: str, symptoms: dict[str, dict], solutions: dict[str, dict]) -> dict[str, Any]:
    """子串匹配给出**参考**候选，不作为判定。"""
    sym_hits = []
    for code, s in symptoms.items():
        for term in [s["name"], *(s.get("aliases") or [])]:
            if term and term in text:
                sym_hits.append({"code": code, "term": term})
                break
    sol_hits = []
    for code, s in solutions.items():
        name = s["name"]
        # 方案名较长，取核心词（去「清理/更换/重新安装」等动词前缀）
        core = name
        for prefix in ("清理", "更换", "重新安装", "清空并清洗", "切换", "升级", "重试", "调整", "重建", "清洗", "转"):
            if core.startswith(prefix):
                core = core[len(prefix):]
                break
        if core and len(core) >= 2 and core in text:
            sol_hits.append({"code": code, "term": core})
    return {"symptoms": sym_hits[:4], "solutions": sol_hits[:4]}


def cmd_init(force: bool) -> int:
    if REVIEW.exists() and not force:
        print(f"{REVIEW} 已存在，追加/覆盖请用 --force（会丢失已填判定）")
        return 1
    symptoms, solutions = seed_index()
    rows = load_candidates()
    out = [HEADER]
    for r in rows:
        p = r["props"]
        text = f"{p['title']} {p.get('rawText', '')}"
        hint = hint_for(text, symptoms, solutions)
        sym_codes = [h["code"] for h in hint["symptoms"]]
        sol_codes = [h["code"] for h in hint["solutions"]]
        out.append(f"  - caseNo: {r['key']}\n")
        out.append(f"    docId: {p['docId']}\n")
        out.append(f"    title: {json.dumps(p['title'], ensure_ascii=False)}\n")
        out.append(f"    stepCount: {p.get('stepCount', 0)}\n")
        out.append(f"    url: {json.dumps(p.get('url', ''), ensure_ascii=False)}\n")
        out.append(f"    kind: \n")
        out.append(f"    decision: \n")
        out.append(f"    symptom: null\n")
        out.append(f"    solution: null\n")
        out.append(f"    note: \"\"\n")
        out.append(f"    autoHint: {{symptoms: {json.dumps(sym_codes, ensure_ascii=False)}, "
                   f"solutions: {json.dumps(sol_codes, ensure_ascii=False)}}}\n")
    REVIEW.parent.mkdir(parents=True, exist_ok=True)
    REVIEW.write_text("".join(out), encoding="utf-8")
    print(f"已生成骨架：{REVIEW}（{len(rows)} 条）")
    return 0


def cmd_check() -> int:
    if not REVIEW.exists():
        print(f"台账不存在：{REVIEW}", file=sys.stderr)
        return 1
    doc = yaml.safe_load(REVIEW.read_text(encoding="utf-8"))
    cases = doc.get("cases") or []
    cands = {r["key"]: r["props"] for r in load_candidates()}
    symptoms, solutions = seed_index()

    errors: list[str] = []
    seen: set[str] = set()
    stats = {"accepted": 0, "rejected": 0, "pending": 0}
    kinds: dict[str, int] = {}
    with_symptom = with_solution = 0

    for c in cases:
        no = c.get("caseNo")
        if no not in cands:
            errors.append(f"{no}: 不在抽取候选中")
            continue
        if no in seen:
            errors.append(f"{no}: 重复")
        seen.add(no)
        decision = (c.get("decision") or "").strip()
        stats[decision if decision in stats else "pending"] += 1
        if decision != "accepted":
            if decision == "rejected" and not (c.get("note") or "").strip():
                errors.append(f"{no}: 拒绝但没写理由")
            continue
        kind = (c.get("kind") or "").strip()
        if kind not in {"troubleshoot", "maintenance", "usage"}:
            errors.append(f"{no}: accepted 但 kind 非法（{kind!r}）")
        kinds[kind] = kinds.get(kind, 0) + 1
        sym, sol = c.get("symptom"), c.get("solution")
        if sym:
            if sym not in symptoms:
                errors.append(f"{no}: symptom {sym} 不在本体症状中")
            else:
                with_symptom += 1
        if sol:
            if sol not in solutions:
                errors.append(f"{no}: solution {sol} 不在本体方案中")
            else:
                with_solution += 1
        # usage 类不应挂症状/方案，否则等于把教程当故障
        if kind == "usage" and (sym or sol):
            errors.append(f"{no}: usage 类不应挂 symptom/solution")

    missing = set(cands) - seen
    if missing:
        errors.append(f"台账漏了 {len(missing)} 条候选：{sorted(missing)[:5]}")

    print(f"台账 {len(cases)} 条 / 候选 {len(cands)} 条")
    print(f"accepted {stats['accepted']} · rejected {stats['rejected']} · pending {stats['pending']}")
    if kinds:
        print(f"accepted 分类：{kinds}")
    print(f"带症状 {with_symptom} 条 · 带方案 {with_solution} 条")
    if errors:
        print("\n校验失败：", file=sys.stderr)
        for e in errors:
            print("  -", e, file=sys.stderr)
        return 1
    print("校验通过")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="FAQ 案例审核台账")
    ap.add_argument("--init", action="store_true", help="生成骨架")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--check", action="store_true", help="校验台账")
    args = ap.parse_args()
    if args.init:
        sys.exit(cmd_init(args.force))
    if args.check:
        sys.exit(cmd_check())
    ap.print_help()


if __name__ == "__main__":
    main()
