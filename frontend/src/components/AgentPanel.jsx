const COV_LABEL = {
  q1: "错误码→部件/故障",
  q2: "根因与检查顺序",
  q3: "同型号相似案例",
  q4: "耗材/备件兼容性",
  q5: "停用/售后/保修",
};

export default function AgentPanel({ detail, trace }) {
  const coverage = detail?.coverage || {};
  const subs = detail?.subQuestions || [];
  const reflections = detail?.reflections || [];
  const tools = detail?.toolResults || [];
  const similar = detail?.similarCases || [];
  const compat = detail?.compatibility || [];
  const issues = detail?.issues || [];

  if (!subs.length && !tools.length) {
    return <div className="empty-hint">规则模式没有 Agent 编排信息，切换到 Agent / 对比模式查看。</div>;
  }

  return (
    <div>
      <div className="sec-title">问题拆解与证据覆盖</div>
      <div className="cov">
        {Object.entries(COV_LABEL).map(([k, label]) => (
          <span key={k} className={`cov-item ${coverage[k] || ""}`} title={subs.find((s) => s.id === k)?.question}>
            {label}
            {coverage[k] === "covered" ? " ✓" : coverage[k] === "partial" ? " ~" : " ✗"}
          </span>
        ))}
      </div>

      {!!subs.length && (
        <>
          <div className="sec-title">子问题</div>
          <table className="mini-table">
            <thead>
              <tr>
                <th style={{ width: 28 }}>#</th>
                <th>问题</th>
                <th style={{ width: 92 }}>工具</th>
              </tr>
            </thead>
            <tbody>
              {subs.map((s) => (
                <tr key={s.id}>
                  <td className="num">{s.id.toUpperCase()}</td>
                  <td>{s.question}</td>
                  <td style={{ fontFamily: "var(--mono)", fontSize: 10.5 }}>{s.toolHint}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}

      <div className="sec-title">工具调用</div>
      {tools.length ? (
        <table className="mini-table">
          <thead>
            <tr>
              <th>工具</th>
              <th>参数</th>
              <th style={{ width: 96 }}>结果</th>
              <th style={{ width: 46 }}>耗时</th>
            </tr>
          </thead>
          <tbody>
            {tools.map((t, i) => (
              <tr key={i}>
                <td style={{ fontFamily: "var(--mono)", fontSize: 10.5, color: "#7dd3fc" }}>{t.tool}</td>
                <td style={{ fontFamily: "var(--mono)", fontSize: 10, color: "var(--text-faint)" }}>
                  {Object.entries(t.args || {})
                    .filter(([, v]) => v !== null && v !== "")
                    .map(([k, v]) => `${k}=${v}`)
                    .join(" ")}
                </td>
                <td>
                  {t.ok ? t.summary : <span style={{ color: "var(--danger)" }}>{t.error}</span>}
                </td>
                <td className="num">{t.latencyMs}ms</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <div className="empty-hint" style={{ padding: 12 }}>
          无工具调用
        </div>
      )}

      <div className="sec-title">重试 / 反思轮次</div>
      {reflections.length ? (
        reflections.map((r) => (
          <div className="reflect" key={r.round}>
            <b>第 {r.round} 轮</b> · {r.decision === "retry" ? "证据不足，追加动作" : "证据已足够，停止"}
            <div style={{ color: "var(--text-faint)" }}>
              缺失：{(r.missing || []).join("、") || "无"}
              {!!(r.nextActions || []).length && ` → ${r.nextActions.join("、")}`}
            </div>
          </div>
        ))
      ) : (
        <div className="empty-hint" style={{ padding: 12 }}>
          首轮证据已覆盖全部子问题，未触发重试或反思
        </div>
      )}

      {(!!similar.length || !!compat.length || !!issues.length) && (
        <>
          <div className="sec-title">案例与一致性核查</div>
          {!!similar.length && (
            <div style={{ marginBottom: 8 }}>
              <div style={{ color: "var(--text-faint)", fontSize: 11, marginBottom: 4 }}>
                同型号相似案例 {similar.length} 条
              </div>
              {similar.slice(0, 4).map((s) => (
                <div key={s.caseNo} className="kv" style={{ marginBottom: 5 }}>
                  <span className="idx">{s.caseType === "Ticket" ? "工单" : "案例"}</span>
                  <span className="name" style={{ fontSize: 11.5 }}>{s.caseNo}</span>
                  <span className="meta">{s.solutions?.[0] || "未记录处置"}</span>
                </div>
              ))}
            </div>
          )}
          {!!compat.length && (
            <div style={{ marginBottom: 8 }}>
              {compat.map((c, i) => (
                <div key={i} className="kv" style={{ marginBottom: 5 }}>
                  <span className="idx">件</span>
                  <span className="name" style={{ fontSize: 11.5 }}>{c.item}</span>
                  <span className="meta" style={{ color: c.compatible ? "var(--ok)" : "var(--danger)" }}>
                    {c.found ? (c.compatible ? "兼容" : "不兼容") : c.reason}
                  </span>
                </div>
              ))}
            </div>
          )}
          {!!issues.length && (
            <div className="warn-box" style={{ fontSize: 11.5 }}>
              一致性告警：{issues.join("；")}
            </div>
          )}
        </>
      )}

      <div className="sec-title">执行轨迹</div>
      <div style={{ fontFamily: "var(--mono)", fontSize: 10.5, color: "var(--text-faint)", lineHeight: 1.9 }}>
        {(trace || []).map((t, i) => (
          <div key={i}>
            {String(i + 1).padStart(2, "0")} {t.node.padEnd(10, " ")} {t.latencyMs}ms
            {t.tool ? ` · ${t.tool}` : ""}
            {t.decision ? ` · ${t.decision}` : ""}
          </div>
        ))}
      </div>
    </div>
  );
}
