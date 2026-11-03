export default function EvidenceList({ evidence, activeId, onHover, onPick }) {
  if (!evidence?.length) {
    return <div className="empty-hint">本次推理没有产生可展示的三元组证据。</div>;
  }
  return (
    <div>
      {evidence.map((e) => (
        <div
          key={e.id}
          className={`evidence-item ${activeId === e.id ? "on" : ""}`}
          onMouseEnter={() => onHover?.(e.id)}
          onMouseLeave={() => onHover?.(null)}
          onClick={() => onPick?.(e)}
        >
          <div className="triple">
            [{e.id}] {e.triple?.[0]} <span style={{ color: "#94a3b8" }}>—{e.triple?.[1]}→</span>{" "}
            {e.triple?.[2]}
          </div>
          {e.text && <div className="text">{e.text}</div>}
          <div className="src">
            {e.source || "未标注来源"} · 置信度 {(e.confidence ?? 0).toFixed(2)}
          </div>
        </div>
      ))}
    </div>
  );
}
