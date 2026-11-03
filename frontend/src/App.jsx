import { useCallback, useEffect, useMemo, useState } from "react";
import QueryPanel from "./components/QueryPanel.jsx";
import AnswerCard from "./components/AnswerCard.jsx";
import GraphCanvas from "./components/GraphCanvas.jsx";
import SideTabs from "./components/SideTabs.jsx";
import Timeline from "./components/Timeline.jsx";
import { ask, getHealth, getRules } from "./api.js";

export default function App() {
  const [mode, setMode] = useState("agent");
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

  const run = useCallback(
    async (question, modeOverride) => {
      const useMode = modeOverride || mode;
      setLoading(true);
      setError("");
      setActiveEvidence(null);
      setSelectedNode(null);
      try {
        const data = await ask({ question, mode: useMode });
        setResult(data);
      } catch (err) {
        setError(err.message || "请求失败");
      } finally {
        setLoading(false);
      }
    },
    [mode]
  );

  // 支持深链演示：/?q=T20 PRO 报 E11&mode=agent 打开即自动诊断
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const q = params.get("q");
    const m = params.get("mode");
    const valid = ["rule", "agent", "compare"];
    if (m && valid.includes(m)) setMode(m);
    if (q) {
      setPreset(q);
      run(q, valid.includes(m) ? m : undefined);
    }
    // 只在首屏执行一次
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const slots = useMemo(() => result?.slots || {}, [result]);
  const nodeDetail = useMemo(() => {
    if (!selectedNode) return null;
    const node = (result?.graph?.nodes || []).find((n) => n.id === selectedNode.id);
    return node ? { ...node, ...selectedNode } : null;
  }, [selectedNode, result]);

  const graphNodeCount = result?.graph?.nodes?.length || 0;
  const graphEdgeCount = result?.graph?.edges?.length || 0;

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
            {health?.llm ? `LLM ${health.model}` : "确定性模式（未接 LLM）"}
          </span>
          <span style={{ color: "var(--text-faint)" }}>
            子图 {graphNodeCount} 节点 / {graphEdgeCount} 边
          </span>
        </div>
      </header>

      <div className="main">
        <div className="col">
          <QueryPanel
            mode={mode}
            setMode={setMode}
            onSubmit={run}
            loading={loading}
            slots={slots}
            initial={preset}
          />
          <AnswerCard result={result} onEvidence={(id) => setActiveEvidence(id)} />
        </div>

        <div className="col" style={{ position: "relative" }}>
          <GraphCanvas
            graph={result?.graph}
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
