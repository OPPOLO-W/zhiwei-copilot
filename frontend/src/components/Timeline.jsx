import { useState } from "react";

const LABEL = {
  intake: ["解析槽位", "🔎"],
  decompose: ["问题拆解", "🧩"],
  plan: ["工具规划", "🗺"],
  act: ["执行工具", "⚙"],
  reflect: ["反思重试", "↻"],
  verify: ["一致性核查", "✓"],
  finalize: ["生成答案", "★"],
  run_rules: ["规则推理", "⚙"],
};

export default function Timeline({ trace, runId }) {
  const [active, setActive] = useState(null);
  const steps = trace || [];

  if (!steps.length) {
    return (
      <div className="timeline">
        <div className="timeline-head">推理轨迹</div>
        <div style={{ color: "var(--text-faint)", fontSize: 11.5 }}>
          尚未运行。每次提问都会生成 intake → decompose → plan → act → reflect → verify → finalize 的可观测轨迹。
        </div>
      </div>
    );
  }

  const payload = active !== null ? steps[active] : null;

  return (
    <div className="timeline">
      <div className="timeline-head">
        <span>推理轨迹 · {steps.length} 步</span>
        <span style={{ fontFamily: "var(--mono)" }}>{runId}</span>
        <span style={{ marginLeft: "auto" }}>点击步骤查看载荷</span>
      </div>
      <div className="steps">
        {steps.map((s, i) => {
          const [label, icon] = LABEL[s.node] || [s.node, "•"];
          const prev = i > 0 ? steps[i - 1].latencyMs || 0 : 0;
          const delta = Math.max(0, (s.latencyMs || 0) - prev);
          return (
            <div
              key={i}
              className={`step ${active === i ? "on" : ""}`}
              onClick={() => setActive(active === i ? null : i)}
            >
              <div className="n">
                <i>{icon}</i>
                {label}
              </div>
              <div className="s">
                +{delta}ms{s.tool ? ` · ${s.tool}` : ""}
              </div>
            </div>
          );
        })}
      </div>
      {payload && (
        <div className="step-payload">{JSON.stringify(payload, null, 2).slice(0, 1400)}</div>
      )}
    </div>
  );
}
