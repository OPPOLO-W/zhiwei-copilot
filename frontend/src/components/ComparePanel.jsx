/** 对比模式：规则链 vs Agent 链的同异。 */
export default function ComparePanel({ result }) {
  if (result?.mode !== "compare" || !result.ruleResult) {
    return <div className="empty-hint">切换到「对比」模式提问，这里会给出规则链与 Agent 链的差异分析。</div>;
  }
  const rule = result.ruleResult;
  const agent = result.agentResult || {};
  const div = result.detail?.divergence || {};

  return (
    <div>
      <div className="sec-title">一致性</div>
      <div className="cov" style={{ marginBottom: 12 }}>
        <span className={`cov-item ${div.topFaultSame ? "covered" : "missing"}`}>
          首要故障 {div.topFaultSame ? "一致" : "不一致"}
        </span>
        <span className={`cov-item ${div.topSolutionSame ? "covered" : "missing"}`}>
          首位方案 {div.topSolutionSame ? "一致" : "不一致"}
        </span>
        <span className="cov-item">故障候选 {div.ruleFaultCount} vs {div.agentFaultCount}</span>
      </div>

      <div className="split">
        <div className="side">
          <h4>规则链（rule）</h4>
          <div style={{ fontSize: 11, color: "var(--text-faint)", marginBottom: 5 }}>
            置信度 {(rule.confidence ?? 0).toFixed(2)} · 策略 {(rule.policies || []).length}
          </div>
          <ul>
            {(rule.faults || []).slice(0, 3).map((f) => (
              <li key={f.id}>
                {f.name} <span style={{ color: "var(--text-faint)" }}>{f.confidence.toFixed(2)}</span>
              </li>
            ))}
            {(rule.solutions || []).slice(0, 2).map((s) => (
              <li key={s.id} style={{ color: "#86efac" }}>{s.name}</li>
            ))}
            {!(rule.faults || []).length && <li>无候选故障</li>}
          </ul>
        </div>
        <div className="side">
          <h4>Agent 链（agent）</h4>
          <div style={{ fontSize: 11, color: "var(--text-faint)", marginBottom: 5 }}>
            置信度 {(agent.confidence ?? 0).toFixed(2)} · 策略 {(agent.policies || []).length}
          </div>
          <ul>
            {(agent.faults || []).slice(0, 3).map((f) => (
              <li key={f.id}>
                {f.name} <span style={{ color: "var(--text-faint)" }}>{f.confidence.toFixed(2)}</span>
              </li>
            ))}
            {(agent.solutions || []).slice(0, 2).map((s) => (
              <li key={s.id} style={{ color: "#86efac" }}>{s.name}</li>
            ))}
            {!(agent.faults || []).length && <li>无候选故障</li>}
          </ul>
        </div>
      </div>

      <div className="sec-title">差异</div>
      <div style={{ fontSize: 11.5, color: "var(--text-dim)", lineHeight: 1.9 }}>
        <div>仅规则链命中策略：{(div.onlyInRule || []).join("、") || "无"}</div>
        <div>仅 Agent 链命中策略：{(div.onlyInAgent || []).join("、") || "无"}</div>
        <div style={{ color: "var(--text-faint)", marginTop: 6 }}>
          说明：两条链路共用同一推理内核，Agent 额外做问题拆解、证据覆盖检查与工具补充（相似案例、兼容性），
          因此在同等准确率下提供更完整的上下文，代价是额外 1~2 次工具调用。
        </div>
      </div>

      <div className="sec-title">Agent 覆盖度</div>
      <div style={{ fontFamily: "var(--mono)", fontSize: 11, color: "var(--text-dim)" }}>
        {Object.entries(result.detail?.coverage || {}).map(([k, v]) => (
          <div key={k}>
            {k} · {v}
          </div>
        ))}
      </div>
    </div>
  );
}
