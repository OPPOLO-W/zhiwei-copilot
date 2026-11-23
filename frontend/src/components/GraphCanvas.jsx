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
 * 布局方向按画布长宽比选：窄高画布用 TB（自上而下一条证明阶梯），
 * 宽扁画布用 LR。原来的固定 LR 在 16:9 的三栏布局里会把 7 个节点压成
 * 中间一条细带，上下全是空白。
 */
// 7 个节点是一条长链：横向排要 1300px 宽，竖向排只要 640px 高。
// 因此只有画布极扁（宽高比 > 2）才值得横排，否则一律竖排，否则节点会被缩成一条细线。
function pickDir(box) {
  if (!box) return "TB";
  return box.clientWidth > box.clientHeight * 2 ? "LR" : "TB";
}

// 大屏上 fit 会把 7 个节点放大到 1.7 倍，字被拉得又大又糊；
// 限制放大倍数，宁可留白，也不让节点变成巨型胶囊
function fitCapped(cy, padding = 26) {
  cy.fit(undefined, padding);
  const LIMIT = 1.2;
  if (cy.zoom() > LIMIT) {
    cy.zoom({ level: LIMIT, renderedPosition: { x: cy.width() / 2, y: cy.height() / 2 } });
    cy.center();
  }
}

function layoutOptions(dir) {
  return dir === "TB"
    ? {
        name: "dagre",
        rankDir: "TB",
        nodeSep: 22,
        rankSep: 58,
        edgeSep: 8,
        animate: false,
        fit: true,
        padding: 24,
      }
    : {
        name: "dagre",
        rankDir: "LR",
        nodeSep: 26,
        rankSep: 82,
        edgeSep: 10,
        animate: false,
        fit: true,
        padding: 26,
      };
}

/**
 * 证明子图渲染。
 * 节点颜色/形状由后端按本体（schema.yaml）下发，前端不维护第二份配色表，
 * 保证「图谱语义 — 可视化样式」始终同源。
 */
export default function GraphCanvas({ graph, highlightEvidenceId, onNodeSelect, headExtra }) {
  const boxRef = useRef(null);
  const cyRef = useRef(null);

  useEffect(() => {
    if (!boxRef.current) return;
    const cy = cytoscape({
      container: boxRef.current,
      elements: [],
      wheelSensitivity: 0.18,
      // 高分屏适配：Canvas 默认按 1x 光栅化再被浏览器拉伸，DPR>1 时文字发虚
      pixelRatio: window.devicePixelRatio || 1,
      style: [
        {
          // 浅色图纸上的节点：纸色填充 + 本体色描边，避免深色底上的「发光胶囊」观感，
          // 也让节点内文字统一用墨色，对比度由填充保证而不是靠字号
          selector: "node",
          style: {
            "background-color": "#fffcf4",
            "background-opacity": 1,
            "border-width": 1.5,
            "border-color": "data(color)",
            label: "data(name)",
            color: "#17140f",
            "font-size": 11.5,
            "font-weight": 500,
            "text-valign": "center",
            "text-halign": "center",
            "text-wrap": "wrap",
            "text-max-width": 108,
            width: "label",
            height: "label",
            padding: "10px",
            shape: "data(shape)",
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
            width: 1,
            "line-color": "#b3a897",
            "target-arrow-color": "#b3a897",
            "target-arrow-shape": "triangle",
            "curve-style": "bezier",
            "arrow-scale": 0.7,
            // 关系名默认不显示：证明子图边较多时文字会互相压盖，
            // 只在悬停/选中（.hl）时展开，保证图面干净。
            label: "",
            "font-size": 9,
            color: "#786e60",
            "text-rotation": "autorotate",
            "text-background-color": "#fffcf4",
            "text-background-opacity": 1,
            "text-background-padding": "2px",
            "text-margin-y": -5,
          },
        },
        {
          selector: "edge.hl",
          style: {
            label: "data(type)",
            width: 2,
            "line-color": "#c0392b",
            "target-arrow-color": "#c0392b",
            color: "#c0392b",
            "font-size": 9.5,
            "z-index": 30,
          },
        },
        {
          selector: "node.hl",
          style: {
            "border-width": 2.5,
            "border-color": "#c0392b",
            "z-index": 20,
          },
        },
        {
          selector: "node.faded",
          style: { opacity: 0.32 },
        },
        {
          selector: "edge.faded",
          style: { opacity: 0.2 },
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

    // 画布尺寸由 flex 决定，首帧可能还是 0；不 resize 的话 cy.fit 会按旧尺寸算，
    // 结果就是最后一个节点被裁在面板外面。这里跟随容器尺寸重新适配。
    const ro = new ResizeObserver(() => {
      cy.resize();
      if (cy.elements().length) fitCapped(cy);
    });
    ro.observe(boxRef.current);

    return () => {
      ro.disconnect();
      cy.destroy();
    };
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
      const dir = pickDir(boxRef.current);
      cy.layout(layoutOptions(dir)).run();
      // 放到下一帧再 fit：布局刚跑完时容器尺寸可能还在变（首屏 flex 收敛），
      // 当帧 fit 会按旧高度算缩放，最后一个节点就被裁在面板外
      requestAnimationFrame(() => {
        cy.resize();
        fitCapped(cy);
      });
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

  // 手动重排也按当前画布比例选方向，避免「点了一下反而更空」
  const relayout = () => {
    const box = boxRef.current;
    if (!box) return;
    cyRef.current?.layout(layoutOptions(pickDir(box))).run();
    fitCapped(cyRef.current);
  };

  const labels = [...new Set((graph?.nodes || []).map((n) => n.label))];
  const legend = labels.map((l) => {
    const hit = (graph.nodes || []).find((n) => n.label === l);
    return { label: l, color: hit?.color || "#64748b" };
  });

  const empty = !graph || !(graph.nodes || []).length;
  const nodeCount = (graph?.nodes || []).length;
  const edgeCount = (graph?.edges || []).length;

  return (
    <div className="panel graph-panel">
      <div className="panel-head graph-head">
        <span className="idx">03</span>
        <b>证明子图</b>
        <span className="en">Proof</span>
        <span className="mono">
          {nodeCount} 节点 / {edgeCount} 边
        </span>
        {headExtra}
        {!empty && (
          <div className="graph-toolbar">
            <button
              className="icon-btn"
              title="适配视图"
              onClick={() => fitCapped(cyRef.current)}
            >
              ⤢
            </button>
            <button className="icon-btn" title="按层级重排" onClick={relayout}>
              ⇄
            </button>
          </div>
        )}
      </div>
      <div className="graph-stage">
        <div id="cy" ref={boxRef} />
        {empty && (
          <div className="graph-empty">
            <div className="lead">No subgraph</div>
            <div style={{ maxWidth: "34em" }}>
              提出问题后，这里会显示「症状 → 故障 → 部件/根因 → 检查/方案」的最小证明路径
            </div>
          </div>
        )}
      </div>
      {/* 图例放在画布下方的独立条带里：浮在画布上会盖住最下面一排节点 */}
      {!empty && (
        <div className="legend">
          <span className="legend-k">图例</span>
          {legend.map((l) => (
            <span key={l.label}>
              <i style={{ background: l.color }} />
              {l.label}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}
