/** 对比模式：把规则链与 Agent 链的结论并列展示，避免只看到一侧。 */
import { useState } from "react";

const PREVIEW = 48;

function Row({ label, value }) {
  return (
    <div className="chain-row">
      <span className="k">{label}</span>
      <span className="v">{value}</span>
    </div>
  );
}

function ChainCard({ title, tag, tone, data, extra = [] }) {
  // 两侧答案都在 300~600 字，全展开要滚很久；默认截断，保证一屏能对照两侧结论
  const [open, setOpen] = useState(false);
  const full = data?.answer || "（无结论）";
  const long = full.length > PREVIEW;
  const shown = open || !long ? full : `${full.slice(0, PREVIEW)}…`;

  const faults = data?.faults || [];
  const causes = data?.causes || [];
  const sols = data?.solutions || [];
  const top = (arr) => (arr.length ? arr[0] : null);

  return (
    <div className="chain-card" style={{ borderLeft: `3px solid ${tone}` }}>
      <div className="chain-head">
        <b style={{ color: tone }}>{title}</b>
        <span className="pill">{tag}</span>
        <span style={{ marginLeft: "auto", fontFamily: "var(--mono)", fontSize: 10.5 }}>
          置信度 {(data?.confidence ?? 0).toFixed(2)}
        </span>
      </div>

      <div className="answer-text" style={{ fontSize: 12, lineHeight: 1.8 }}>
        {shown}
      </div>
      {long && (
        <button className="chain-more" onClick={() => setOpen((v) => !v)}>
          {open ? "收起" : `展开全文（${full.length} 字）`}
        </button>
      )}

      <div className="chain-facts">
        <Row
          label="首要故障"
          value={top(faults) ? `${top(faults).name}（${top(faults).confidence.toFixed(2)}）` : "无"}
        />
        <Row label="首要根因" value={top(causes)?.name || "无"} />
        <Row label="首选方案" value={top(sols)?.name || "无"} />
      </div>

      {/* 原来 9 行事实把两张卡撑到一屏放不下，第二张卡直接看不见；
          次要指标压成一行等宽小字，两侧仍可逐项对读 */}
      <div className="chain-meta">
        <span>策略 {(data?.policies || []).join("、") || "无"}</span>
        <span>证据 {(data?.evidence || []).length}</span>
        <span>结论 {(data?.claims || []).length}</span>
        <span>检查 {(data?.checks || []).length}</span>
        {extra.map((e) => (
          <span key={e.k}>
            {e.k} {e.v}
          </span>
        ))}
      </div>
    </div>
  );
}

export default function CompareAnswers({ result }) {
  if (!result) {
    return (
      <div className="panel" style={{ flex: "1 1 auto", minHeight: 0 }}>
        <div className="panel-head">
          <span className="idx">02</span>
          <b>双侧结论</b>
          <span className="en">Both</span>
        </div>
        <div className="empty-hint">
          选「对比」后提问，这里会并列显示规则链与 Agent 链的结论
          <br />
          中间的图谱区还可以切换看两侧各自检索出的子图
        </div>
      </div>
    );
  }

  if (result.mode !== "compare" || !result.ruleResult) {
    return (
      <div className="panel" style={{ flex: "1 1 auto", minHeight: 0 }}>
        <div className="panel-head">
          <span className="idx">02</span>
          <b>双侧结论</b>
          <span className="en">Both</span>
        </div>
        <div className="empty-hint">当前结果不是对比模式。请在上方选择「对比」后重新提问。</div>
      </div>
    );
  }

  const llmSteps = (result.trace || []).filter(
    (s) => s.llmSlots || s.llmDriven || s.llmAnswer
  ).length;
  const agentTag = llmSteps > 0 ? `LLM 驱动 · ${llmSteps} 步` : "确定性";

  // Agent 相对规则链多做了什么：这几项规则引擎完全没有对应能力
  const d = result.detail || {};
  const covered = Object.values(d.coverage || {}).filter((v) => v === "covered").length;
  const agentExtra = [
    { k: "相似案例", v: `${(d.similarCases || []).length} 条` },
    { k: "兼容性校验", v: `${(d.compatibility || []).length} 项` },
    { k: "工具调用", v: `${(d.toolResults || []).length} 次` },
    { k: "证据覆盖", v: `${covered}/5` },
  ];
  const ruleExtra = [
    { k: "相似案例", v: "—（无此能力）" },
    { k: "兼容性校验", v: "—（无此能力）" },
    { k: "工具调用", v: "—（无）" },
    { k: "证据覆盖", v: "—（不做覆盖度检查）" },
  ];

  const ruleData = result.ruleResult;
  const agentData = result.agentResult || {};
  const first = (o, k) => (o?.[k]?.[0] ? o[k][0].name : null);
  const sameFault = first(ruleData, "faults") === first(agentData, "faults");
  const sameSol = first(ruleData, "solutions") === first(agentData, "solutions");

  return (
    <div className="panel" style={{ flex: "1 1 auto", minHeight: 0 }}>
      <div className="panel-head">
        <span className="idx">02</span>
        <b>双侧结论</b>
        <span className="en">Both</span>
        <span className="pill accent">规则 vs Agent</span>
      </div>
      <div className="panel-body" style={{ overflow: "auto" }}>
        <div className="chain-summary">
          <span className={sameFault ? "same" : "diff"}>首要故障{sameFault ? "一致" : "不一致"}</span>
          <span className={sameSol ? "same" : "diff"}>首位方案{sameSol ? "一致" : "不一致"}</span>
          <span>
            答案 {(ruleData.answer || "").length} / {(agentData.answer || "").length} 字
          </span>
        </div>
        <ChainCard title="规则链" tag="纯规则引擎" tone="var(--ink-3)" data={ruleData} extra={ruleExtra} />
        <ChainCard title="Agent 链" tag={agentTag} tone="var(--signal)" data={agentData} extra={agentExtra} />
      </div>
    </div>
  );
}
