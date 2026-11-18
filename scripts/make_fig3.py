"""生成 P0–P5 总览图（reports/figures/fig3_uplift.svg）。

数据点全部取自 var/eval 下的真实评测文件，写在图脚注里；不引外部绘图库，
与仓库里 fig1/fig2 一样是手写 SVG，方便直接用浏览器或 Word 打开。
"""

from pathlib import Path

STAGES = ["起点\n(阶段B后)", "P1\n候选重排", "P2-P3\n事实+证据", "P4\n成本门控", "P5\n规则计划"]
TOPSOL = [0.400, 0.533, 0.867, 0.867, 0.867]
P50 = [11021, 11948, 9276, 9062, 6384]
W, H = 860, 420


def txt(x, y, s, size=11, fill="#64748b", anchor="middle", weight="400"):
    return (
        f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" fill="{fill}" '
        f'text-anchor="{anchor}" font-weight="{weight}">{s}</text>'
    )


def main() -> None:
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
        f'viewBox="0 0 {W} {H}" '
        'font-family="-apple-system, Segoe UI, Microsoft YaHei, sans-serif">',
        f'<rect width="{W}" height="{H}" fill="#ffffff"/>',
    ]

    # ---- 左：首选方案正确率 ----
    ax, ay, aw, ah = 70, 70, 330, 250
    out.append(txt(ax, 34, "首选方案正确率（难题集 20 题）", 13, "#0f172a", "start", "600"))
    xs = [ax + aw * (i + 0.5) / len(STAGES) for i in range(len(STAGES))]

    def ymap(v, lo=0.3, hi=1.05):
        return ay + ah - (v - lo) / (hi - lo) * ah

    for v in (0.4, 0.6, 0.8, 1.0):
        y = ymap(v)
        out.append(f'<line x1="{ax}" y1="{y:.1f}" x2="{ax + aw}" y2="{y:.1f}" stroke="#e2e8f0"/>')
        out.append(txt(ax - 10, y + 4, f"{v:.1f}", 11, "#64748b", "end"))
    out.append(f'<line x1="{ax}" y1="{ay + ah}" x2="{ax + aw}" y2="{ay + ah}" stroke="#94a3b8"/>')
    out.append(f'<line x1="{ax}" y1="{ay}" x2="{ax}" y2="{ay + ah}" stroke="#94a3b8"/>')
    pts = " ".join(f"{x:.1f},{ymap(v):.1f}" for x, v in zip(xs, TOPSOL))
    out.append(f'<polyline points="{pts}" fill="none" stroke="#2563eb" stroke-width="2.5"/>')
    for x, v in zip(xs, TOPSOL):
        y = ymap(v)
        out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5" fill="#2563eb"/>')
        out.append(txt(x, y - 12, f"{v:.3f}", 11, "#1d4ed8", "middle", "600"))
    out.append(txt(ax + 8, ay + ah - 10, "规则路径 0.000", 10, "#b91c1c", "start"))
    for x, s in zip(xs, STAGES):
        for k, line in enumerate(s.split("\n")):
            out.append(txt(x, ay + ah + 22 + k * 15, line, 11, "#334155"))

    # ---- 右：时延 ----
    bx, by, bw, bh = 520, 70, 290, 250
    out.append(txt(bx, 34, "单题时延 P50（毫秒，越低越好）", 13, "#0f172a", "start", "600"))
    bxs = [bx + bw * (i + 0.5) / len(STAGES) for i in range(len(STAGES))]

    def bymap(v):
        return by + bh - v / 12000 * bh

    for v in (0, 4000, 8000, 12000):
        y = bymap(v)
        out.append(f'<line x1="{bx}" y1="{y:.1f}" x2="{bx + bw}" y2="{y:.1f}" stroke="#e2e8f0"/>')
        out.append(txt(bx - 10, y + 4, f"{v}", 11, "#64748b", "end"))
    out.append(f'<line x1="{bx}" y1="{by + bh}" x2="{bx + bw}" y2="{by + bh}" stroke="#94a3b8"/>')
    out.append(f'<line x1="{bx}" y1="{by}" x2="{bx}" y2="{by + bh}" stroke="#94a3b8"/>')
    bar = bw / len(STAGES) * 0.46
    for x, v in zip(bxs, P50):
        y = bymap(v)
        out.append(
            f'<rect x="{x - bar / 2:.1f}" y="{y:.1f}" width="{bar:.1f}" '
            f'height="{by + bh - y:.1f}" fill="#93c5fd" rx="3"/>'
        )
        out.append(txt(x, y - 8, str(v), 11, "#1d4ed8", "middle", "600"))
    for x, s in zip(bxs, STAGES):
        for k, line in enumerate(s.split("\n")):
            out.append(txt(x, by + bh + 22 + k * 15, line, 11, "#334155"))

    out.append(
        txt(
            24,
            H - 12,
            "数据：var/eval/agent_llm_20261118_130844 / 134922 / 191753 / 195135 / 214744"
            "（memory 后端，单次运行）",
            10,
            "#94a3b8",
            "start",
        )
    )
    out.append("</svg>")

    path = Path(__file__).resolve().parents[1] / "reports" / "figures" / "fig3_uplift.svg"
    path.write_text("\n".join(out), encoding="utf-8")
    print("written", path)


if __name__ == "__main__":
    main()
