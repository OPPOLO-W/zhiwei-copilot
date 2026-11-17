import { useEffect, useState } from "react";

const EXAMPLES = [
  "T20 PRO 提示 E11，左轮不转",
  "T20 PRO 报 10010，配网一直失败",
  "T20 PRO 电池鼓包，还能继续用吗",
  "T20 PRO 拖地不出水",
  "T20 PRO 运行声音很大",
];

// 一维四选一：每项是「链路 + 引擎」的确定组合，不再让用户拼两个正交开关，
// 因此不存在「规则推理 + LLM」「对比 + 确定性」这类无意义组合。
const ROUTES = [
  ["rule", "规则推理", "纯规则引擎，无工具调用、无编排"],
  ["agent_det", "Agent·确定性", "七节点编排，启发式驱动，可离线演示"],
  ["agent_llm", "Agent·LLM", "七节点编排，LLM 驱动（无 key 自动降级）"],
  ["compare", "对比", "规则链 vs Agent 链，双侧并列"],
];

export default function QueryPanel({ route, setRoute, onSubmit, loading, slots, initial }) {
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
    // flex: 0 0 auto —— 禁止被下方 AnswerCard（flex:1 1 auto）压缩，
    // 否则出结果后 QueryPanel 会被挤扁，按钮与引擎开关被 overflow 裁掉
    <div className="panel" style={{ flex: "0 0 auto" }}>
      <div className="panel-head">
        <b>售后问题</b>
        <div className="spacer" />
        <div className="segmented">
          {ROUTES.map(([key, label, hint]) => (
            <button
              key={key}
              className={route === key ? "on" : ""}
              onClick={() => setRoute(key)}
              title={hint}
              style={{ padding: "5px 8px", fontSize: 11.5 }}
            >
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
            // Enter 直接提交，Shift+Enter 换行（原先只支持 Ctrl/Cmd+Enter，容易误判为「点了没反应」）
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              submit();
            }
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
                <span style={{ marginLeft: 8, fontSize: 12 }}>诊断中…</span>
              </span>
            ) : (
              "开始诊断"
            )}
          </button>
          <span
            style={{ marginLeft: "auto", fontSize: 10.5, color: "var(--text-faint)" }}
            title={ROUTES.find(([k]) => k === route)?.[2]}
          >
            {ROUTES.find(([k]) => k === route)?.[1]}
          </span>
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
