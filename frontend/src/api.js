const BASE = import.meta.env.VITE_API_BASE || "";

async function json(path, init) {
  const res = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  const text = await res.text();
  let body;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    body = { detail: text };
  }
  if (!res.ok) {
    const msg = body?.detail || `请求失败（HTTP ${res.status}）`;
    throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  return body;
}

export const getHealth = () => json("/api/health");

export const ask = ({ question, mode, model, code, symptom, caseNo }) =>
  json("/api/query", {
    method: "POST",
    body: JSON.stringify({
      question: question || null,
      mode,
      model: model || null,
      code: code || null,
      symptom: symptom || null,
      caseNo: caseNo || null,
    }),
  });

export const getRules = () => json("/api/rules");
export const getOntology = () => json("/api/ontology");
export const getCase = (caseNo) => json(`/api/cases/${encodeURIComponent(caseNo)}`);

/**
 * 回放一次运行的事件流（SSE）。后端把 trace 转成事件后一次性推送，
 * 这里仍按标准 EventSource 消费，方便将来换成真正的逐事件流式推理。
 */
export function streamRun(runId, onEvent, onDone) {
  const es = new EventSource(`${BASE}/api/query/${runId}/stream`);
  es.onmessage = (e) => {
    try {
      onEvent?.(JSON.parse(e.data));
    } catch {
      /* 忽略无法解析的心跳帧 */
    }
  };
  es.addEventListener("done", () => {
    es.close();
    onDone?.();
  });
  es.onerror = () => {
    es.close();
    onDone?.();
  };
  return () => es.close();
}
