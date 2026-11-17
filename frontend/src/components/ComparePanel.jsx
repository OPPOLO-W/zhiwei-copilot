/** 对比模式：规则链 vs Agent 链的逐项对照。 */

function fmt(v) {
  return v === undefined || v === null || v === "" ? "—" : String(v);
}

function Row({ label, a, b, diff }) {
  return (
    <div className="chain-row" style={{ alignItems: "flex-start" }}>
      <span className="k" style={{ flex: "0 0 66px" }}>{label}</span>
      <span className="v" style={{ flex: "1 1 0", color: diff ? "var(--signal)" : "var(--text-dim)" }}>
        {a}
      </span>
      <span className="v" style={{ flex: "1 1 0", color: diff ? "var(--signal)" : "var(--text-dim)" }}>
        {b}
      </span>
    </div>
  );
}

export default function ComparePanel({ result }) {
  if (result?.mode !== "compare" || !result.ruleResult) {
    return <div className="empty-hint">切换到「对比」模式提问，这里会给出规则链与 Agent 链的逐项对照。</div>;
  }
  const rule = result.ruleResult;
  const agent = result.agentResult || {};
  const div = result.detail?.divergence || {};
  const rg = rule.graph || {};
  const ag = agent.graph || {};
  const rt = rule.trace || {};
  const at = agent.trace || {};

  const pick = (o, k) => (o?.[k]?.[0] ? o[k][0].name : "无");
  const same = (k) => pick(rule, k) === pick(agent, k);

  return (
    <div>
      <div className="sec-title">一致性</div>
      <div className="cov" style={{ marginBottom: 12 }}>
        <span className={`cov-item ${div.topFaultSame ? "covered" : "missing"}`}>
          首要故障 {div.topFaultSame ? "一致" : "不一致"}
        </span>
        <span className={`cov-item ${div.topSolutionSame ? "covered" : "missing"}`}>
          首位方案 {div.topSolutionSame ? "一致" : "不一致"}
        </span>
        <span className="cov-item">
          故障候选 {div.ruleFaultCount} vs {div.agentFaultCount}
        </span>
      </div>

      <div className="sec-title">
        逐项对照
        <span style={{ marginLeft: 6, fontWeight: 400, color: "var(--text-faint)", fontSize: 10.5 }}>
          （朱红 = 两侧不同）
        </span>
      </div>
      <div style={{ marginBottom: 4 }}>
        <Row label="" a={<b>规则链</b>} b={<b>Agent 链</b>} />
        <Row
          label="置信度"
          a={(rule.confidence ?? 0).toFixed(2)}
          b={(agent.confidence ?? 0).toFixed(2)}
          diff={rule.confidence !== agent.confidence}
        />
        <Row label="首要故障" a={pick(rule, "faults")} b={pick(agent, "faults")} diff={!same("faults")} />
        <Row label="首要根因" a={pick(rule, "causes")} b={pick(agent, "causes")} diff={!same("causes")} />
        <Row
          label="首选方案"
          a={pick(rule, "solutions")}
          b={pick(agent, "solutions")}
          diff={!same("solutions")}
        />
        <Row
          label="候选故障"
          a={fmt((rule.faults || []).length)}
          b={fmt((agent.faults || []).length)}
          diff={(rule.faults || []).length !== (agent.faults || []).length}
        />
        <Row
          label="证据数"
          a={fmt((rule.evidence || []).length)}
          b={fmt((agent.evidence || []).length)}
          diff={(rule.evidence || []).length !== (agent.evidence || []).length}
        />
        <Row
          label="结论条数"
          a={fmt((rule.claims || []).length)}
          b={fmt((agent.claims || []).length)}
          diff={(rule.claims || []).length !== (agent.claims || []).length}
        />
        <Row
          label="检查项"
          a={fmt((rule.checks || []).length)}
          b={fmt((agent.checks || []).length)}
          diff={(rule.checks || []).length !== (agent.checks || []).length}
        />
        <Row
          label="子图"
          a={`${(rg.nodes || []).length} 节点 / ${(rg.edges || []).length} 边`}
          b={`${(ag.nodes || []).length} 节点 / ${(ag.edges || []).length} 边`}
          diff={(rg.nodes || []).length !== (ag.nodes || []).length}
        />
        <Row
          label="规则触发"
          a={`${Object.keys(rt.firedRules || {}).length} 条`}
          b={`${Object.keys(at.firedRules || {}).length} 条`}
          diff={Object.keys(rt.firedRules || {}).length !== Object.keys(at.firedRules || {}).length}
        />
        <Row
          label="推理迭代"
          a={fmt(rt.iterations)}
          b={fmt(at.iterations)}
          diff={rt.iterations !== at.iterations}
        />
        <Row
          label="命中策略"
          a={(rule.policies || []).join("、") || "无"}
          b={(agent.policies || []).join("、") || "无"}
          diff={(rule.policies || []).join() !== (agent.policies || []).join()}
        />
      </div>

      <div className="sec-title">差异</div>
      <div style={{ fontSize: 11.5, color: "var(--text-dim)", lineHeight: 1.9 }}>
        <div>仅规则链命中策略：{(div.onlyInRule || []).join("、") || "无"}</div>
        <div>仅 Agent 链命中策略：{(div.onlyInAgent || []).join("、") || "无"}</div>
        <div style={{ color: "var(--text-faint)", marginTop: 6 }}>
          两条链路共用同一推理内核，Agent 额外做问题拆解、证据覆盖检查与工具补充（相似案例、兼容性）。
          上表若全为灰色，说明本轮两侧结论同源，差异只体现在工具与证据层面。
        </div>
      </div>

      <div className="sec-title">Agent 覆盖度</div>
      <div className="cov" style={{ marginBottom: 8 }}>
        {Object.entries(result.detail?.coverage || {}).map(([k, v]) => (
          <span key={k} className={`cov-item ${v === "covered" ? "covered" : "missing"}`}>
            {k} · {v}
          </span>
        ))}
      </div>

      {!!(result.detail?.toolResults || []).length && (
        <>
          <div className="sec-title">Agent 工具调用</div>
          <div style={{ fontFamily: "var(--mono)", fontSize: 10.5, color: "var(--text-dim)", lineHeight: 1.8 }}>
            {result.detail.toolResults.map((t, i) => (
              <div key={i}>
                {t.tool} · {t.ok ? "ok" : "fail"} · {fmt(t.latencyMs)}ms
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  );
}
