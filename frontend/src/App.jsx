import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import QueryPanel from "./components/QueryPanel.jsx";
import AnswerCard from "./components/AnswerCard.jsx";
import CompareAnswers from "./components/CompareAnswers.jsx";
import GraphCanvas from "./components/GraphCanvas.jsx";
import SideTabs from "./components/SideTabs.jsx";
import Timeline from "./components/Timeline.jsx";
import { ask, getHealth, getRules } from "./api.js";

// 一条路由 = 一个「链路 + 引擎」的确定组合；后端仍是 (mode, useLlm) 两个参数
const modeOf = (r) => (r === "rule" ? "rule" : r === "compare" ? "compare" : "agent");
const llmOf = (r) => r === "agent_llm" || r === "compare";

export default function App() {
  const [route, setRoute] = useState("agent_llm");
  // 对比模式下图谱默认看 Agent 侧，可切到规则侧
  const [graphSide, setGraphSide] = useState("agent");
  const [result, setResult] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [activeEvidence, setActiveEvidence] = useState(null);
  const [selectedNode, setSelectedNode] = useState(null);
  const [health, setHealth] = useState(null);
  const [rules, setRules] = useState(null);
  const [preset, setPreset] = useState("");


  useEffect(() => {
    getHealth().then(setHealth).catch(() => setHealth({ ok: false }));
    getRules().then(setRules).catch(() => {});
  }, []);

  // 并发守卫用 ref 而不是 loading state：setState 是异步的，
  // 连续点击时闭包里的 loading 可能仍是旧值，会重复发请求
  const inflight = useRef(false);

  const run = useCallback(
    async (question, routeOverride) => {
      if (inflight.current) return;
      inflight.current = true;
      const r = routeOverride || route;
      setLoading(true);
      setError("");
      setActiveEvidence(null);
      setSelectedNode(null);
      try {
        const data = await ask({ question, mode: modeOf(r), useLlm: llmOf(r) });
        setResult(data);
        // 每次对比都回到 Agent 侧，避免停留在上一次的规则子图
        if (modeOf(r) === "compare") setGraphSide("agent");
      } catch (err) {
        setError(err.message || "请求失败");
      } finally {
        inflight.current = false;
        setLoading(false);
      }
    },
    [route]
  );

  // 支持深链演示：/?q=T20 PRO 报 E11&route=agent_llm
  // 兼容旧链接：?mode=agent&llm=1 会映射到 agent_llm
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const q = params.get("q");
    const m = params.get("mode");
    const r = params.get("route");
    const l = params.get("llm");
    const ROUTES_ALL = ["rule", "agent_det", "agent_llm", "compare"];
    let next;
    if (ROUTES_ALL.includes(r)) next = r;
    else if (m === "rule" || m === "compare") next = m;
    else if (m === "agent") next = l === "1" || l === "true" ? "agent_llm" : "agent_det";
    if (next) setRoute(next);
    if (q) {
      setPreset(q);
      run(q, next);
    }
    // 只在首屏执行一次
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const slots = useMemo(() => result?.slots || {}, [result]);

  // 对比模式：图谱区可在「规则链子图 / Agent 链子图」之间切换，
  // 只显示一侧是之前对比看不出东西的主要原因之一
  const isCompare = result?.mode === "compare";
  const shownGraph = isCompare && graphSide === "rule" ? result?.ruleResult?.graph : result?.graph;

  // 两条链共用同一诊断内核，证据子图通常逐节点相同；只有真存在差异时才给切换，
  // 否则点了没变化反而让人以为界面坏了
  const graphsDiffer = useMemo(() => {
    if (!isCompare) return false;
    const ids = (g) => (g?.nodes || []).map((n) => n.id).sort().join("|");
    return ids(result?.ruleResult?.graph) !== ids(result?.graph);
  }, [isCompare, result]);

  const nodeDetail = useMemo(() => {
    if (!selectedNode) return null;
    const node = (shownGraph?.nodes || []).find((n) => n.id === selectedNode.id);
    return node ? { ...node, ...selectedNode } : null;
  }, [selectedNode, shownGraph]);

  const graphNodeCount = shownGraph?.nodes?.length || 0;
  const graphEdgeCount = shownGraph?.edges?.length || 0;

  // 从 trace 反查本次是否真的走了 LLM：LLM 节点会带 llmSlots / llmDriven / llmAnswer 标记
  const llmSteps = useMemo(
    () => (result?.trace || []).filter((s) => s.llmSlots || s.llmDriven || s.llmAnswer).length,
    [result]
  );

  return (
    <div className="app">
      <header className="header">
        <div className="brand">
          智维 Copilot
          <small>DEEBOT T 系列 · 售后知识图谱推理</small>
        </div>
        <div className="header-right">
          <span>
            <i className={`dot ${health?.ok ? "ok" : "bad"}`} />
            {health?.ok ? `${health.backend} · ${health.entities} 实体 / ${health.relations} 关系` : "图存储未连接"}
          </span>
          <span style={{ color: "var(--text-faint)" }}>
            {health?.llm ? `LLM 已配置 · ${health.model}` : "未配置 LLM key · 确定性兜底"}
          </span>
          {result && (
            <span style={{ color: llmSteps > 0 ? "var(--accent)" : "var(--text-faint)" }}>
              本次 {llmSteps > 0 ? `LLM 驱动（${llmSteps} 步）` : "确定性"}
            </span>
          )}
          <span style={{ color: "var(--text-faint)" }}>
            子图 {graphNodeCount} 节点 / {graphEdgeCount} 边
          </span>
        </div>
      </header>

      <div className="main">
        <div className="col">
          <QueryPanel
            route={route}
            setRoute={setRoute}
            onSubmit={run}
            loading={loading}
            slots={slots}
            initial={preset}
          />
          {isCompare ? (
            <CompareAnswers result={result} onEvidence={(id) => setActiveEvidence(id)} />
          ) : (
            <AnswerCard result={result} onEvidence={(id) => setActiveEvidence(id)} />
          )}
        </div>

        <div className="col" style={{ position: "relative" }}>
          {isCompare && graphsDiffer && (
            <div style={{ position: "absolute", top: 10, left: 12, zIndex: 6 }}>
              <div className="segmented" title="切换查看哪条链路检索出的子图">
                <button
                  className={graphSide === "agent" ? "on" : ""}
                  onClick={() => setGraphSide("agent")}
                >
                  Agent 子图
                </button>
                <button
                  className={graphSide === "rule" ? "on" : ""}
                  onClick={() => setGraphSide("rule")}
                >
                  规则子图
                </button>
              </div>
            </div>
          )}
          {isCompare && !graphsDiffer && (
            <div
              style={{
                position: "absolute",
                top: 12,
                left: 14,
                zIndex: 6,
                fontSize: 10.5,
                color: "var(--text-faint)",
                background: "var(--bg-soft)",
                border: "1px solid var(--line-soft)",
                borderRadius: 6,
                padding: "3px 8px",
              }}
              title="规则链与 Agent 链共用同一诊断内核，证据子图逐节点相同"
            >
              两侧子图相同（同一证据子图）
            </div>
          )}
          <GraphCanvas
            graph={shownGraph}
            highlightEvidenceId={activeEvidence}
            onNodeSelect={setSelectedNode}
          />
          {nodeDetail && (
            <div className="node-card">
              <div className="t">
                <i
                  style={{
                    width: 9,
                    height: 9,
                    borderRadius: 2,
                    background: nodeDetail.color || "#64748b",
                    display: "inline-block",
                  }}
                />
                <b>{nodeDetail.name}</b>
                <span style={{ color: "var(--text-faint)", fontSize: 10.5 }}>{nodeDetail.label}</span>
                <span className="close" onClick={() => setSelectedNode(null)}>
                  ✕
                </span>
              </div>
              <code>{nodeDetail.id}</code>
              {nodeDetail.risk && <div>风险等级：{nodeDetail.risk}</div>}
              <div style={{ color: "var(--text-faint)", marginTop: 2 }}>
                证据：{(nodeDetail.evidenceIds || []).join(" ") || "—"}
              </div>
            </div>
          )}
        </div>

        <div className="col">
          <SideTabs
            result={result}
            rules={rules}
            activeEvidence={activeEvidence}
            setActiveEvidence={setActiveEvidence}
            onFocusEvidence={(e) => setActiveEvidence(e.id)}
          />
        </div>
      </div>

      <Timeline trace={result?.trace} runId={result?.runId} />

      {error && <div className="toast">⚠ {error}</div>}
    </div>
  );
}
