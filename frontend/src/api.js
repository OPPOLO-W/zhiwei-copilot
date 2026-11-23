const BASE = import.meta.env.VITE_API_BASE || "";

// LLM 路径单次约 8–15s，留足余量；超时兜底避免 fetch 永久挂起把按钮锁死在 loading
const TIMEOUT_MS = 90000;

async function json(path, init) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), TIMEOUT_MS);
  let res;
  try {
    res = await fetch(`${BASE}${path}`, {
      headers: { "Content-Type": "application/json" },
      signal: ctrl.signal,
      ...init,
    });
  } catch (err) {
    if (err.name === "AbortError") {
      throw new Error(
        `请求超时（${TIMEOUT_MS / 1000}s）。LLM 模式通常 8–15s，若持续超时请查看后端日志。`
      );
    }
    throw err;
  } finally {
    clearTimeout(timer);
  }
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

export const ask = ({ question, mode, model, code, symptom, caseNo, useLlm, sessionId, checkResults }) =>
  json("/api/query", {
    method: "POST",
    body: JSON.stringify({
      question: question || null,
      mode,
      model: model || null,
      code: code || null,
      symptom: symptom || null,
      caseNo: caseNo || null,
      // 会话续跑：sessionId 让后端继承槽位与已采信事实
      sessionId: sessionId || null,
      // 追问答案：checkId + 用户选择，后端据此改写事实并重算
      // 无追问答案时发空列表而非 null：后端 schema 按 list 声明，null 会被 422 拦下
      checkResults: checkResults || [],
      // 后端 opt-in：仅 true 时走 LLM 驱动，默认仍为确定性路径
      useLlm: !!useLlm,
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
