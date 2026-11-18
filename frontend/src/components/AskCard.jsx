const OPTIONS = [
  ["positive", "有异常／能复现"],
  ["negative", "正常／没有"],
  ["skip", "不确定"],
];

/**
 * 追问卡片：后端在证据不足且存在高信息增益检查项时返回 askUser。
 * 三个按钮就是「把这次检查的结果告诉系统」，点完续用同一 session 重算。
 */
export default function AskCard({ askUser, onAnswer, disabled }) {
  if (!askUser) return null;
  const { question, checkId, reason } = askUser;
  const ig = reason?.ig;
  const cost = reason?.cost;
  return (
    <div className="ask-card">
      <div className="ask-head">
        <span className="mark">?</span>
        <span className="ask-title">还需要一步现场确认</span>
        <span className="ask-meta mono">
          {checkId}
          {typeof ig === "number" ? ` · IG ${ig.toFixed(2)}` : ""}
          {typeof cost === "number" ? ` · 成本 ${cost.toFixed(0)}` : ""}
        </span>
      </div>
      <div className="ask-q">{question}</div>
      <div className="ask-opts">
        {OPTIONS.map(([value, label]) => (
          <button
            key={value}
            className={`ask-btn${value === "skip" ? " ghost" : ""}`}
            disabled={disabled}
            onClick={() => onAnswer?.(value)}
          >
            {label}
          </button>
        ))}
      </div>
    </div>
  );
}
