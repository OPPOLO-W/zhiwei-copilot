import { useEffect, useState } from "react";
import EvidenceList from "./EvidenceList.jsx";
import ProofTree from "./ProofTree.jsx";
import RulesPanel from "./RulesPanel.jsx";
import AgentPanel from "./AgentPanel.jsx";
import ComparePanel from "./ComparePanel.jsx";

const TABS = [
  ["evidence", "证据"],
  ["proof", "证明树"],
  ["rules", "规则"],
  ["agent", "Agent"],
  ["compare", "对比"],
];

export default function SideTabs({
  result,
  rules,
  activeEvidence,
  setActiveEvidence,
  onFocusEvidence,
}) {
  const [tab, setTab] = useState(() => {
    const t = new URLSearchParams(window.location.search).get("tab");
    return TABS.some(([k]) => k === t) ? t : "evidence";
  });

  // 对比模式的产物就是「逐项对照」，进来直接停在该页签，省得再手点一次
  useEffect(() => {
    if (result?.mode === "compare") setTab("compare");
  }, [result?.mode]);

  return (
    <div className="panel" style={{ flex: 1, minHeight: 0 }}>
      <div className="tabs">
        {TABS.map(([key, label]) => (
          <button key={key} className={tab === key ? "on" : ""} onClick={() => setTab(key)}>
            {label}
          </button>
        ))}
      </div>
      <div className="panel-body">
        {!result && <div className="empty-hint">运行一次诊断后，这里展示证据、证明树与规则轨迹。</div>}

        {result && tab === "evidence" && (
          <EvidenceList
            evidence={result.evidence}
            activeId={activeEvidence}
            onHover={setActiveEvidence}
            onPick={(e) => onFocusEvidence?.(e)}
          />
        )}
        {result && tab === "proof" && (
          <ProofTree
            tree={result.proof?.derivationTree}
            mode={result.proof?.mode}
            nodes={result.graph?.nodes}
          />
        )}
        {result && tab === "rules" && (
          <RulesPanel rules={rules} ruleTrace={result.detail?.ruleTrace || result.ruleResult?.trace} />
        )}
        {result && tab === "agent" && <AgentPanel detail={result.detail} trace={result.trace} />}
        {result && tab === "compare" && <ComparePanel result={result} />}
      </div>
    </div>
  );
}
