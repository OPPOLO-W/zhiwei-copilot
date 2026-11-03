"""从评测聚合结果生成报告用图（纯 SVG，不依赖绘图库）。

为什么手写 SVG 而不是 matplotlib：
  1. 项目依赖保持精简，评审核对代码时不需要再装绘图环境；
  2. 图是构建产物，`make figures` 可随时按最新 `var/eval/aggregate_*.json` 重画，
     避免报告里的图和实验结果脱节；
  3. SVG 是文本，diff 可读。

用法：
    python -m backend.tools.gen_report_figures            # 画最新一次聚合结果
    python -m backend.tools.gen_report_figures --agg var/eval/aggregate_xxx.json
"""

from __future__ import annotations

import argparse
import glob
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "reports" / "figures"

# 与 README / 报告一致的显示名
LABELS = {
    "rule": "A rule",
    "rag": "B rag",
    "agent": "C agent",
    "agent_noreflect": "D w/o reflection",
    "agent_norules": "E w/o rules",
}
COLORS = {
    "rule": "#2563eb",
    "rag": "#dc2626",
    "agent": "#0f766e",
    "agent_noreflect": "#7c3aed",
    "agent_norules": "#ea580c",
}


def load(path: str | None) -> dict[str, Any]:
    if path:
        p = Path(path)
    else:
        files = sorted(glob.glob(str(ROOT / "var" / "eval" / "aggregate_*.json")))
        if not files:
            raise SystemExit("没有 aggregate_*.json，先跑 python -m backend.app.eval.run_eval --all")
        p = Path(files[-1])
    data = json.loads(p.read_text(encoding="utf-8"))
    return {"path": p, **data}


def _svg_head(w: int, h: int, title: str) -> list[str]:
    return [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
        f'viewBox="0 0 {w} {h}" font-family="-apple-system, Segoe UI, Microsoft YaHei, sans-serif">',
        f'<rect width="{w}" height="{h}" fill="#ffffff"/>',
        f'<text x="24" y="30" font-size="15" font-weight="600" fill="#0f172a">{title}</text>',
    ]


def scatter(data: dict[str, Any]) -> str:
    """Path Recall × Answer Acc：一眼看出哪组「说得全且答得对」。"""
    w, h = 720, 460
    left, top, pw, ph = 80, 70, 580, 320
    xs = (0.25, 1.0)   # Path Recall 范围
    ys = (0.25, 1.0)   # Answer Acc 范围

    def px(v: float) -> float:
        return left + (v - xs[0]) / (xs[1] - xs[0]) * pw

    def py(v: float) -> float:
        return top + ph - (v - ys[0]) / (ys[1] - ys[0]) * ph

    out = _svg_head(w, h, "图 1　路径召回 × 答案准确率（50 题，Neo4j 后端）")
    # 网格与刻度
    for v in [0.3, 0.45, 0.6, 0.75, 0.9, 1.0]:
        y = py(v)
        out.append(f'<line x1="{left}" y1="{y:.1f}" x2="{left + pw}" y2="{y:.1f}" stroke="#e2e8f0"/>')
        out.append(f'<text x="{left - 10}" y="{y + 4:.1f}" font-size="11" fill="#64748b" '
                   f'text-anchor="end">{v:.2f}</text>')
    for v in [0.3, 0.45, 0.6, 0.75, 0.9]:
        x = px(v)
        out.append(f'<line x1="{x:.1f}" y1="{top}" x2="{x:.1f}" y2="{top + ph}" stroke="#e2e8f0"/>')
        out.append(f'<text x="{x:.1f}" y="{top + ph + 18}" font-size="11" fill="#64748b" '
                   f'text-anchor="middle">{v:.2f}</text>')
    out.append(f'<line x1="{left}" y1="{top + ph}" x2="{left + pw}" y2="{top + ph}" stroke="#94a3b8"/>')
    out.append(f'<line x1="{left}" y1="{top}" x2="{left}" y2="{top + ph}" stroke="#94a3b8"/>')
    out.append(f'<text x="{left + pw / 2:.0f}" y="{top + ph + 40}" font-size="12" fill="#334155" '
               f'text-anchor="middle">Path Recall（证据召回）</text>')
    out.append(f'<text x="26" y="{top + ph / 2:.0f}" font-size="12" fill="#334155" '
               f'transform="rotate(-90 26 {top + ph / 2:.0f})" text-anchor="middle">Answer Accuracy</text>')

    # 数据点：A / C / D 三项指标完全相同（0.980 / 0.942），直接画会叠成一个点。
    # 这里按坐标分组，同组横向并排三个小圆 + 一个合并标签——重合本身就是要传达的信息，
    # 不能靠加随机抖动把它藏起来。
    groups: dict[tuple[float, float], list[str]] = {}
    for key, sysv in data["systems"].items():
        m = sysv["mean"]
        if "pathRecall" not in m:
            continue
        groups.setdefault((round(m["pathRecall"], 4), round(m["answerAccuracy"], 4)), []).append(key)

    for (rx, acc), keys in groups.items():
        x, y = px(rx), py(acc)
        n = len(keys)
        for i, key in enumerate(keys):
            dx = (i - (n - 1) / 2) * 18
            out.append(f'<circle cx="{x + dx:.1f}" cy="{y:.1f}" r="7" '
                       f'fill="{COLORS.get(key, "#475569")}" opacity="0.9"/>')
        label = " / ".join(LABELS.get(k, k) for k in keys)
        if n > 1:
            label += f"（{acc:.2f} / {rx:.3f}）"
        # 靠右的点把标签放左边，避免出画布
        if x > 480:
            out.append(f'<text x="{x - 24:.1f}" y="{y + 4:.1f}" font-size="11.5" fill="#0f172a" '
                       f'font-weight="600" text-anchor="end">{label}</text>')
        else:
            out.append(f'<text x="{x + 24:.1f}" y="{y + 4:.1f}" font-size="11.5" fill="#0f172a" '
                       f'font-weight="600" text-anchor="start">{label}</text>')

    out.append(f'<text x="{left}" y="{h - 26}" font-size="11" fill="#94a3b8">'
               f'数据源：{data["path"].name}（{data.get("nQuestions", 50)} 题 × {data.get("repeat", 1)} 次）</text>')
    out.append("</svg>")
    return "\n".join(out)


def bars(data: dict[str, Any]) -> str:
    """四指标分组柱状：C 与 E 的差距一眼可见。"""
    w, h = 720, 460
    left, top, pw, ph = 70, 70, 600, 300
    metrics = [("answerAccuracy", "Answer Acc"), ("faultF1", "Fault F1"),
               ("pathRecall", "Path R"), ("pathF1", "Path F1")]
    systems = [k for k in ("rule", "rag", "agent", "agent_noreflect", "agent_norules")
               if k in data["systems"]]

    out = _svg_head(w, h, "图 2　五组系统关键指标对比（柱高 = 均值）")
    group_w = pw / len(metrics)
    bar_w = group_w / (len(systems) + 1.2)
    for gi, (key, label) in enumerate(metrics):
        gx = left + gi * group_w
        out.append(f'<text x="{gx + group_w / 2:.1f}" y="{top + ph + 22}" font-size="12" '
                   f'fill="#334155" text-anchor="middle">{label}</text>')
        for si, sysk in enumerate(systems):
            v = float(data["systems"][sysk]["mean"].get(key, 0.0) or 0.0)
            bh = v * ph
            bx = gx + bar_w * (si + 0.6)
            by = top + ph - bh
            out.append(f'<rect x="{bx:.1f}" y="{by:.1f}" width="{bar_w * 0.86:.1f}" height="{bh:.1f}" '
                       f'fill="{COLORS.get(sysk, "#475569")}" opacity="0.88"/>')
            out.append(f'<text x="{bx + bar_w * 0.43:.1f}" y="{by - 5:.1f}" font-size="9.5" '
                       f'fill="#475569" text-anchor="middle">{v:.2f}</text>')
    out.append(f'<line x1="{left}" y1="{top + ph}" x2="{left + pw}" y2="{top + ph}" stroke="#94a3b8"/>')
    for v in [0, 0.25, 0.5, 0.75, 1.0]:
        y = top + ph - v * ph
        out.append(f'<line x1="{left}" y1="{y:.1f}" x2="{left + pw}" y2="{y:.1f}" stroke="#e2e8f0"/>')
        out.append(f'<text x="{left - 8}" y="{y + 4:.1f}" font-size="10.5" fill="#64748b" '
                   f'text-anchor="end">{v:.2f}</text>')

    # 图例
    lx = left
    for sysk in systems:
        out.append(f'<rect x="{lx}" y="{top + ph + 40}" width="10" height="10" '
                   f'fill="{COLORS.get(sysk, "#475569")}"/>')
        out.append(f'<text x="{lx + 15}" y="{top + ph + 49}" font-size="11" fill="#334155">'
                   f'{LABELS.get(sysk, sysk)}</text>')
        lx += 14 + len(LABELS.get(sysk, sysk)) * 7
    out.append("</svg>")
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description="生成报告用图")
    ap.add_argument("--agg", default=None, help="aggregate_*.json 路径，默认取最新")
    args = ap.parse_args()
    data = load(args.agg)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "fig1_scatter.svg").write_text(scatter(data), encoding="utf-8")
    (OUT / "fig2_metrics.svg").write_text(bars(data), encoding="utf-8")
    print(f"已生成：{OUT}/fig1_scatter.svg, fig2_metrics.svg（源 {data['path'].name}）")


if __name__ == "__main__":
    main()
