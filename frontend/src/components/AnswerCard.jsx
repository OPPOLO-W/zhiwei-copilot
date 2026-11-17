const STATUS_LABEL = {
  ok: ["ok", "推理完成"],
  degraded: ["warn", "降级回答"],
  out_of_scope: ["warn", "超出范围"],
  error: ["danger", "失败"],
};

const POLICY_LABEL = {
  safety_stop: "安全拦截",
  official_service_first: "优先官方售后",
  network_first: "网络问题优先",
  chronic_issue: "重复报修",
  firmware_hint: "固件已知问题",
  conflict: "根因冲突",
  insufficient_evidence: "证据不足",
  duplicate_ticket: "重复工单",
  water_sequence: "水路检查顺序",
  cross_model_compare: "跨型号对比",
  code_not_applicable: "型号/错误码不匹配",
  unknown_code: "错误码未收录",
  unknown_model: "型号未收录",
  unknown_intent: "信息不足",
  out_of_scope: "超出范围",
};

export default function AnswerCard({ result, onEvidence }) {
  if (!result) {
    return (
      <div className="panel">
        <div className="panel-head">
          <span className="idx">02</span>
          <b>推理结论</b>
          <span className="en">Verdict</span>
        </div>
        <div className="empty-hint">
          <div className="lead">Awaiting input</div>
          输入一个真实售后问题开始。规则链与 Agent 链都会给出可逐条追溯的证明路径。
        </div>
      </div>
    );
  }

  const [cls, label] = STATUS_LABEL[result.status] || ["", result.status];
  const policies = result.detail?.policies || [];
  const safety = result.warnings?.filter((w) => w.includes("安全") || w.includes("停止使用")) || [];
  // 后端会把策略性提示同时写进 answer 与 warnings，这里去重，避免同一句话出现两次
  const others = (result.warnings || []).filter(
    (w) => !safety.includes(w) && !String(result.answer || "").includes(w)
  );
  const conf = Math.round((result.confidence || 0) * 100);

  return (
    <div className="panel" style={{ flex: "1 1 auto", minHeight: 0 }}>
      <div className="panel-head">
        <span className="idx">02</span>
        <b>推理结论</b>
        <span className="en">Verdict</span>
        <span className={`pill ${cls}`}>{label}</span>
        <div className="conf">
          <span>置信</span>
          <div className="conf-bar">
            <i style={{ width: `${conf}%` }} />
          </div>
          <span>{(result.confidence ?? 0).toFixed(2)}</span>
        </div>
      </div>
      <div className="panel-body">
        <div style={{ display: "flex", flexWrap: "wrap", gap: 5, marginBottom: 10 }}>
          {policies.map((p) => (
            <span key={p} className={`pill ${p.includes("not_applicable") || p.startsWith("unknown") ? "warn" : "accent"}`}>
              {POLICY_LABEL[p] || p}
            </span>
          ))}
          <span className="pill">
            {result.latencyMs ?? 0} ms · {result.mode}
          </span>
        </div>

        <div className="answer-text">{result.answer}</div>

        {safety.map((w, i) => (
          <div className="warn-box" key={`s${i}`}>
            <span className="mark">!</span>
            {w}
          </div>
        ))}
        {others.slice(0, 3).map((w, i) => (
          <div className="warn-box info" key={`o${i}`}>
            {w}
          </div>
        ))}

        {!!(result.claims || []).length && (
          <>
            <div className="sec-title">可追溯结论</div>
            <div className="claims">
              {result.claims.map((c, i) => (
                <div className="claim" key={i}>
                  <span className="rule-ids">{(c.ruleIds || []).join(" ")}</span>
                  <span>{c.text}</span>
                  {!!(c.evidenceIds || []).length && (
                    <button className="ev-btn" onClick={() => onEvidence?.(c.evidenceIds[0])}>
                      证据 {c.evidenceIds.length}
                    </button>
                  )}
                </div>
              ))}
            </div>
          </>
        )}
      </div>
    </div>
  );
}
