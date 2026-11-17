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
          {/* 三元组拆两行：主语一行、关系+宾语一行。
              原来挤一行时，长 id 会在面板宽度处被硬断成 "STUC K"，很难读。 */}
          <div className="triple">
            <div className="s">
              [{e.id}] {e.triple?.[0]}
            </div>
            <div className="o">
              <span className="rel">{e.triple?.[1]}</span> → {e.triple?.[2]}
            </div>
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
