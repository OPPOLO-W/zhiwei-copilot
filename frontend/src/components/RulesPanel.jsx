const CAT_COLOR = {
  diagnosis: "#38bdf8",
  ranking: "#a78bfa",
  safety: "#f87171",
  hygiene: "#64748b",
  aggregation: "#34d399",
};

/** 规则库概览 + 本次运行的触发统计，用来证明「规则是显式、可审计的」。 */
export default function RulesPanel({ rules, ruleTrace }) {
  const fired = ruleTrace?.firedRules || {};
  const firedIds = Object.keys(fired);
  const body = rules?.items || [];

  if (!body.length) {
    return <div className="empty-hint">规则库加载中或后端不可用。</div>;
  }

  const sorted = [...body].sort((a, b) => Number(b.fired || 0) - Number(a.fired || 0) || a.id.localeCompare(b.id));

  return (
    <div>
      <div className="sec-title">
        规则集 {rules.count} 条 · 本次触发 {firedIds.length} 条
      </div>
      <div
        style={{
          display: "flex",
          gap: 10,
          flexWrap: "wrap",
          marginBottom: 10,
          fontFamily: "var(--mono)",
          fontSize: 11,
          color: "var(--text-faint)",
        }}
      >
        <span>迭代次数 {ruleTrace?.iterations ?? "—"}</span>
        <span>事实数 {ruleTrace?.factCount ?? "—"}</span>
        <span>后端 {ruleTrace?.backend ?? "—"}</span>
      </div>
      <table className="mini-table">
        <thead>
          <tr>
            <th style={{ width: 34 }}>ID</th>
            <th>规则</th>
            <th style={{ width: 54 }}>优先级</th>
            <th style={{ width: 40 }}>触发</th>
          </tr>
        </thead>
        <tbody>
          {sorted.map((r) => (
            <tr key={r.id}>
              <td className="num" style={{ color: CAT_COLOR[r.category] || "var(--text-dim)" }}>
                {r.id}
              </td>
              <td>
                <div style={{ color: "var(--text)", fontSize: 11.5 }}>{r.name}</div>
                <div style={{ color: "var(--text-faint)", fontSize: 10.5, lineHeight: 1.6 }}>
                  {r.explanation}
                </div>
              </td>
              <td className="num">{r.priority}</td>
              <td className="num" style={{ color: fired[r.id] ? "var(--ok)" : "var(--text-faint)" }}>
                {fired[r.id] || "—"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
