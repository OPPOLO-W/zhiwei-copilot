import { useState } from "react";

const LABEL = {
  intake: ["解析槽位", "🔎"],
  decompose: ["问题拆解", "🧩"],
  plan: ["工具规划", "🗺"],
  act: ["执行工具", "⚙"],
  // 两个节点语义不同，不可混称「反思」：
  //   retry  = 确定性路径的覆盖度查表重试（无 LLM）
  //   reflect = LLM 路径的语义反思（缺什么、为何缺、是否有新矛盾）
  retry: ["重试（规则策略）", "↻"],
  reflect: ["反思（LLM）", "↻"],
  // verify 是确定性矛盾校验，两条路径共用同一份实现，LLM 不得改写
  // 主标签保持 4 字以免换行，确定性子标记放在副行（hover 有完整说明）
  verify: ["矛盾校验", "✓"],
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
          尚未运行。确定性路径生成 intake → decompose → plan → act → retry → verify → finalize；
          LLM 路径把 retry 换成 reflect（语义反思）。
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
          // LLM 反思调用失败时仍会写 reflect 节点，但 driver=heuristic，标签必须如实降级
          const shown =
            s.node === "reflect" && s.driver !== "llm" ? "重试（规则策略）" : label;
          const prev = i > 0 ? steps[i - 1].latencyMs || 0 : 0;
          const delta = Math.max(0, (s.latencyMs || 0) - prev);
          return (
            <div
              key={i}
              className={`step ${active === i ? "on" : ""}`}
              onClick={() => setActive(active === i ? null : i)}
              title={
                s.node === "verify"
                  ? "矛盾校验：确定性实现，两条路径共用，LLM 不得改写"
                  : s.node === "retry"
                    ? "覆盖度重试：按缺失子问题查表放宽检索，非语义反思"
                    : undefined
              }
            >
              <div className="n">
                <i>{icon}</i>
                {shown}
              </div>
              <div className="s">
                +{delta}ms{s.tool ? ` · ${s.tool}` : ""}
                {s.node === "verify" ? " · 确定性" : ""}
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
