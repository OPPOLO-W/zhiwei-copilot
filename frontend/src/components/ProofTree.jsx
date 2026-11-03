import { useMemo, useState } from "react";

/**
 * 证明树：把推理引擎产出的 derivationTree 递归渲染成「规则 ← 前提」结构。
 * 事实里的实体 id 会用本次子图的节点名称替换，避免满屏 CMP:/CAUSE: 前缀。
 */
export default function ProofTree({ tree, mode, nodes }) {
  const names = useMemo(() => {
    const map = new Map();
    (nodes || []).forEach((n) => map.set(n.id, n.name));
    return map;
  }, [nodes]);

  if (!tree) {
    return <div className="empty-hint">本次运行没有可展示的推导树（查询落空或结果为空）。</div>;
  }

  return (
    <div className="tree">
      <div style={{ color: "var(--text-faint)", fontSize: 11, marginBottom: 8 }}>
        证明方式：{mode === "hybrid" ? "规则树（Agent 与规则共用同一推理内核）" : "正向链式推理"}
      </div>
      <Node node={tree} depth={0} root names={names} />
    </div>
  );
}

function humanize(arg, names) {
  const text = String(arg);
  if (names.has(text)) return names.get(text);
  if (text.startsWith("CASE:")) return "本次查询";
  return text.replace(/^[A-Z]+:/, "");
}

/** 规则解释语句里也带着实体 id，一并换成人名，避免证明树出现第二种命名风格。 */
function humanizeText(text, names) {
  return String(text || "").replace(/[A-Z]{2,}:[A-Z0-9-]+/g, (m) => humanize(m, names));
}

function Node({ node, depth, root, names }) {
  const [open, setOpen] = useState(depth < 2);
  const premises = node.premises || [];
  const args = (node.args || []).filter((a) => !/^-?\d*\.?\d+$/.test(String(a)));

  return (
    <div className={root ? "" : "tree-node"}>
      <div className="row">
        <span className="pred">{node.pred || "fact"}</span>
        <span className="fact">({args.map((a) => humanize(a, names)).join(" → ")})</span>
        {node.ruleId && <span className="rule">{node.ruleId}</span>}
        {typeof node.cf === "number" && node.cf > 0 && (
          <span className="cf">cf={node.cf.toFixed(2)}</span>
        )}
        {!!premises.length && (
          <button
            className="ev-btn"
            style={{
              marginLeft: "auto",
              border: "1px solid var(--line)",
              background: "transparent",
              color: "var(--text-faint)",
              borderRadius: 5,
              fontSize: 10,
              padding: "0 5px",
              cursor: "pointer",
            }}
            onClick={() => setOpen((v) => !v)}
          >
            {open ? "收起" : `前提 ${premises.length}`}
          </button>
        )}
      </div>
      {node.explanation && depth < 3 && (
        <div style={{ color: "var(--text-faint)", fontSize: 11, margin: "0 0 4px 0" }}>
          {humanizeText(node.explanation, names)}
        </div>
      )}
      {open && premises.map((p, i) => <Node key={i} node={p} depth={depth + 1} names={names} />)}
    </div>
  );
}
