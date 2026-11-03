import { useEffect, useRef } from "react";
import cytoscape from "cytoscape";
import dagre from "cytoscape-dagre";

cytoscape.use(dagre);

const SHAPE_FILL = {
  "round-rectangle": 0.16,
  rectangle: 0,
  ellipse: 0.5,
  diamond: 0,
  hexagon: 0,
};

/**
 * 证明子图渲染。
 * 节点颜色/形状由后端按本体（schema.yaml）下发，前端不维护第二份配色表，
 * 保证「图谱语义 — 可视化样式」始终同源。
 */
export default function GraphCanvas({ graph, highlightEvidenceId, onNodeSelect }) {
  const boxRef = useRef(null);
  const cyRef = useRef(null);

  useEffect(() => {
    if (!boxRef.current) return;
    const cy = cytoscape({
      container: boxRef.current,
      elements: [],
      wheelSensitivity: 0.18,
      style: [
        {
          selector: "node",
          style: {
            "background-color": "data(color)",
            "background-opacity": 0.92,
            label: "data(name)",
            color: "#08131b",
            "font-size": 12,
            "font-weight": 600,
            "text-valign": "center",
            "text-halign": "center",
            "text-wrap": "wrap",
            "text-max-width": 116,
            width: "label",
            height: "label",
            padding: "11px",
            shape: "data(shape)",
            "border-width": 0,
          },
        },
        {
          selector: 'node[label = "Symptom"]',
          style: { shape: "round-rectangle" },
        },
        {
          selector: 'node[label = "ErrorCode"]',
          style: { shape: "round-rectangle", "font-family": "monospace" },
        },
        {
          selector: 'node[label = "Fault"]',
          style: { shape: "round-rectangle" },
        },
        {
          selector: "edge",
          style: {
            width: 1.4,
            "line-color": "#3b4d5c",
            "target-arrow-color": "#3b4d5c",
            "target-arrow-shape": "triangle",
            "curve-style": "bezier",
            "arrow-scale": 0.8,
            // 关系名默认不显示：证明子图边较多时文字会互相压盖，
            // 只在悬停/选中（.hl）时展开，保证图面干净。
            label: "",
            "font-size": 9,
            color: "#7dd3fc",
            "text-rotation": "autorotate",
            "text-background-color": "#0a0f14",
            "text-background-opacity": 0.9,
            "text-background-padding": "2px",
            "text-margin-y": -5,
          },
        },
        {
          selector: "edge.hl",
          style: {
            label: "data(type)",
            width: 3,
            "line-color": "#38bdf8",
            "target-arrow-color": "#38bdf8",
            color: "#7dd3fc",
            "font-size": 9.5,
            "z-index": 30,
          },
        },
        {
          selector: "node.hl",
          style: {
            "border-width": 3,
            "border-color": "#38bdf8",
            "z-index": 20,
          },
        },
        {
          selector: "node.faded",
          style: { opacity: 0.3 },
        },
        {
          selector: "edge.faded",
          style: { opacity: 0.18 },
        },
      ],
      layout: { name: "preset" },
    });
    cyRef.current = cy;
    cy.on("mouseover", "edge", (e) => e.target.addClass("hl"));
    cy.on("mouseout", "edge", (e) => e.target.removeClass("hl"));
    cy.on("tap", "node", (e) => onNodeSelect?.(e.target.data()));
    cy.on("tap", (e) => {
      if (e.target === cy) onNodeSelect?.(null);
    });
    return () => cy.destroy();
  }, [onNodeSelect]);

  useEffect(() => {
    const cy = cyRef.current;
    if (!cy) return;
    const nodes = (graph?.nodes || []).map((n) => ({
      data: {
        id: n.id,
        name: n.name || n.id,
        label: n.label,
        color: n.color || "#64748b",
        shape: n.shape || "round-rectangle",
        risk: n.risk,
        step: n.step,
        evidenceIds: n.evidenceIds || [],
      },
    }));
    const edges = (graph?.edges || []).map((e, i) => ({
      data: {
        id: e.id || `e${i}`,
        source: e.source,
        target: e.target,
        type: e.type,
        confidence: e.confidence,
        evidenceId: e.evidenceId,
      },
    }));
    cy.elements().remove();
    cy.add([...nodes, ...edges]);
    if (nodes.length) {
      cy.layout({
        name: "dagre",
        rankDir: "LR",
        nodeSep: 26,
        rankSep: 82,
        edgeSep: 10,
        animate: false,
        fit: true,
        padding: 26,
      }).run();
      cy.fit(undefined, 26);
    }
  }, [graph]);

  useEffect(() => {
    const cy = cyRef.current;
    if (!cy) return;
    cy.elements().removeClass("hl faded");
    if (!highlightEvidenceId) return;
    const edge = cy.edges().filter((e) => e.data("evidenceId") === highlightEvidenceId);
    if (!edge.length) return;
    cy.elements().addClass("faded");
    edge.removeClass("faded").addClass("hl");
    edge.connectedNodes().removeClass("faded").addClass("hl");
    cy.animate({ center: { eles: edge }, zoom: 1.05 }, { duration: 260 });
  }, [highlightEvidenceId]);

  const labels = [...new Set((graph?.nodes || []).map((n) => n.label))];
  const legend = labels.map((l) => {
    const hit = (graph.nodes || []).find((n) => n.label === l);
    return { label: l, color: hit?.color || "#64748b" };
  });

  const empty = !graph || !(graph.nodes || []).length;

  return (
    <div className="panel graph-panel">
      <div id="cy" ref={boxRef} />
      {empty && (
        <div className="graph-empty">
          <div>暂无证明子图</div>
          <div style={{ fontSize: 11.5 }}>
            提出问题后，这里会显示「症状 → 故障 → 部件/根因 → 检查/方案」的最小证明路径
          </div>
        </div>
      )}
      {!empty && (
        <>
          <div className="graph-toolbar">
            <button
              className="icon-btn"
              title="适配视图"
              onClick={() => cyRef.current?.fit(undefined, 26)}
            >
              ⤢
            </button>
            <button
              className="icon-btn"
              title="按层级重排"
              onClick={() =>
                cyRef.current
                  ?.layout({ name: "dagre", rankDir: "LR", nodeSep: 26, rankSep: 82, fit: true, padding: 26 })
                  .run()
              }
            >
              ⇄
            </button>
          </div>
          <div className="legend">
            {legend.map((l) => (
              <span key={l.label}>
                <i style={{ background: l.color }} />
                {l.label}
              </span>
            ))}
          </div>
        </>
      )}
    </div>
  );
}
