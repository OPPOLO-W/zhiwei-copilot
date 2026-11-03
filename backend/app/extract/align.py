"""双源对账：手册故障表 ↔ 官网 FAQ。

为什么值得单独做一层
--------------------
同一台机器有两份独立描述它的材料：官方手册的「故障排查表」是**工程视角**
（故障 → 原因 → 方法），官网 FAQ 是**用户视角**（一句话现象 → 一串步骤）。
两份材料各写一遍同一件事，就能互相校验：

* 手册里有、FAQ 里没有 → 可能是低频故障，也可能是手册写得太细，用户不会这样描述；
* FAQ 里有、手册里没有 → 说明手册排查表覆盖不足，FAQ 是必要的补充知识源；
* 两边都有但说法差很远 → 恰恰是「用户语言 → 工程语义」映射最难的地方，
  值得单独沉淀同义说法。

所以这份报告既是对账指标，也是**同义词典的候选清单**。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _applies_to(fault: dict[str, Any], product: str) -> bool:
    """手册里「（除 T20 机型外）」这类注解决定该故障是否适用于本机型。"""
    for excl in fault.get("excludedModels") or []:
        if product.upper().startswith(str(excl).upper()):
            return False
    return True


def alignment_report(ext: Any, product: str) -> dict[str, Any]:
    """统计手册故障与 FAQ 症状的对齐情况。"""
    faults = {n.key: n for n in ext.nodes if n.label == "Fault"}
    symptoms = {
        n.key: n for n in ext.nodes
        if n.label == "Symptom" and n.props.get("faqType") == "troubleshoot"
    }
    applicable = {
        k: n for k, n in faults.items() if _applies_to(n.props, product)
    }
    excluded = [n.name for k, n in faults.items() if k not in applicable]

    pairs: list[dict[str, Any]] = []
    covered_faults: set[str] = set()
    covered_symptoms: set[str] = set()
    for e in ext.edges:
        if e.type != "INDICATES":
            continue
        if e.tail.key not in faults or e.head.key not in symptoms:
            continue
        pairs.append({
            "symptom": symptoms[e.head.key].name,
            "fault": faults[e.tail.key].name,
            "score": e.confidence,
            "basis": e.props.get("matchMethod", ""),
            "faqDoc": e.doc_id,
        })
        covered_faults.add(e.tail.key)
        covered_symptoms.add(e.head.key)

    applicable_keys = set(applicable)
    manual_missed = [n.name for k, n in applicable.items() if k not in covered_faults]
    faq_missed = [n.name for k, n in symptoms.items() if k not in covered_symptoms]

    pairs.sort(key=lambda p: -p["score"])
    return {
        "product": product,
        "manualFaultCount": len(faults),
        "manualApplicable": len(applicable),
        "manualExcluded": excluded,
        "faqTroubleshootCount": len(symptoms),
        "alignedPairs": len(pairs),
        "manualCovered": len(covered_faults & applicable_keys),
        "faqCovered": len(covered_symptoms),
        "manualCoverage": round(len(covered_faults & applicable_keys) / len(applicable), 4)
        if applicable else 0.0,
        "faqCoverage": round(len(covered_symptoms) / len(symptoms), 4) if symptoms else 0.0,
        "pairs": pairs,
        "manualMissed": manual_missed,
        "faqMissed": faq_missed,
    }


def write_report(report: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def print_report(report: dict[str, Any]) -> None:
    print("\n双源对账（手册故障表 ↔ 官网 FAQ）：")
    print(f"  手册故障      {report['manualFaultCount']} 条"
          f"（本机型适用 {report['manualApplicable']}）")
    if report["manualExcluded"]:
        print(f"  不适用本机型  {'、'.join(report['manualExcluded'][:3])}")
    print(f"  FAQ 故障类    {report['faqTroubleshootCount']} 条")
    print(f"  对齐成功      {report['alignedPairs']} 对"
          f"   手册覆盖 {report['manualCoverage'] * 100:.0f}%"
          f"    FAQ 覆盖 {report['faqCoverage'] * 100:.0f}%")
    if report["pairs"]:
        print("  高置信样例：")
        for p in report["pairs"][:3]:
            print(f"    {p['score']:.2f}  {p['symptom'][:26]} ←→ {p['fault'][:26]}"
                  f"  ({p['basis']})")
    if report["manualMissed"]:
        print(f"  手册未被 FAQ 覆盖：{'、'.join(n[:14] for n in report['manualMissed'][:5])}")
    if report["faqMissed"]:
        print(f"  FAQ 无对应手册故障：{'、'.join(n[:14] for n in report['faqMissed'][:5])}")
