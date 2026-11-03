import { useEffect, useState } from "react";

const EXAMPLES = [
  "T20 PRO 提示 E11，左轮不转",
  "T20 PRO 报 10010，配网一直失败",
  "T20 PRO 电池鼓包，还能继续用吗",
  "T20 PRO 拖地不出水",
  "T20 PRO 运行声音很大",
];

const MODES = [
  ["rule", "规则推理"],
  ["agent", "Agent"],
  ["compare", "对比"],
];

export default function QueryPanel({ mode, setMode, onSubmit, loading, slots, initial }) {
  const [text, setText] = useState(EXAMPLES[0]);

  // 深链演示时把 URL 里的问题同步进输入框
  useEffect(() => {
    if (initial) setText(initial);
  }, [initial]);

  const submit = () => {
    const q = text.trim();
    if (!q || loading) return;
    onSubmit(q);
  };

  return (
    <div className="panel">
      <div className="panel-head">
        <b>售后问题</b>
        <div className="spacer" />
        <div className="segmented">
          {MODES.map(([key, label]) => (
            <button key={key} className={mode === key ? "on" : ""} onClick={() => setMode(key)}>
              {label}
            </button>
          ))}
        </div>
      </div>
      <div className="panel-body" style={{ flex: "0 0 auto" }}>
        <textarea
          className="query-input"
          value={text}
          placeholder="用自然语言描述故障，例如：T20 PRO 报 E11，左轮不转"
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) submit();
          }}
        />
        <div className="chips">
          {EXAMPLES.map((e) => (
            <button className="chip" key={e} onClick={() => setText(e)}>
              {e.length > 18 ? `${e.slice(0, 18)}…` : e}
            </button>
          ))}
        </div>
        <div className="actions">
          <button className="btn primary" onClick={submit} disabled={loading}>
            {loading ? (
              <span className="loading">
                <i />
                <i />
                <i />
              </span>
            ) : (
              "开始诊断"
            )}
          </button>
        </div>
        <div className="slot-row">
          <Slot label="型号" value={slots?.model} />
          <Slot label="错误码" value={slots?.code} />
          <Slot label="症状" value={slots?.symptom} />
          <Slot label="意图" value={slots?.intent} />
        </div>
      </div>
    </div>
  );
}

function Slot({ label, value }) {
  return (
    <span className={`slot ${value ? "" : "empty"}`}>
      <b>{label}</b>
      {value || "—"}
    </span>
  );
}
