"""OpenAI 兼容的 LLM 客户端（薄封装）。

用 `httpx` 直连 `/chat/completions`，不引入 openai SDK——项目已依赖 httpx，
少一个重量级依赖、少一层抽象。支持：

  * `chat()`：普通文本补全，返回 `LLMResult`（含 token 用量）；
  * `chat_json()`：请求 JSON 输出（优先 `response_format=json_object`，
    兼容不支持该参数的模型时退化为文本 + 括号提取）；
  * 429 / 5xx / 超时自动重试（指数退避），其余错误抛分层异常；
  * 未配置 key 时 `enabled=False`，调用直接抛 `LLMUnavailable`，
    由上层据此降级到确定性路径。

异常分层是为了让调用方「一处 try/except 就能决定降级」：
    LLMUnavailable  —— 没 key / 服务不可达 / 5xx 耗尽，属「不可用」，整体降级；
    LLMRateLimited  —— 限流，可提示用户稍后再试；
    LLMError        —— 其余（返回格式错误等），属「单次失败」。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import httpx

from ..config import Settings, get_settings

log = logging.getLogger("zhiwei.llm")

DEFAULT_TIMEOUT_S = 60.0
DEFAULT_MAX_TOKENS = 2048


class LLMError(Exception):
    """LLM 调用失败（格式错误 / 响应异常等）。"""


class LLMRateLimited(LLMError):
    """触发限流（429）。"""


class LLMUnavailable(LLMError):
    """LLM 不可用（未配置 key / 服务不可达 / 5xx 重试耗尽）。"""


@dataclass
class LLMResult:
    content: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    model: str = ""

    @property
    def usage(self) -> dict[str, int]:
        return {
            "promptTokens": self.prompt_tokens,
            "completionTokens": self.completion_tokens,
            "totalTokens": self.total_tokens,
        }


class LLMClient:
    """同步 OpenAI 兼容客户端。每个实例即一个连接池，进程内复用。"""

    def __init__(self, settings: Settings | None = None, *, timeout: float = DEFAULT_TIMEOUT_S) -> None:
        self._settings = settings or get_settings()
        self._timeout = timeout
        self._client: httpx.Client | None = None
        # 最近一次调用的用量；供调用方在紧邻的下一步读取（同线程顺序执行，安全）
        self.last_usage: dict[str, int] = {}

    # ---- 状态 ----
    @property
    def enabled(self) -> bool:
        return self._settings.llm_enabled

    @property
    def base_url(self) -> str:
        return self._settings.llm_base_url.rstrip("/")

    @property
    def model(self) -> str:
        return self._settings.llm_model

    @property
    def temperature(self) -> float:
        return self._settings.llm_temperature

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(base_url=self.base_url, timeout=self._timeout)
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    # ---- 核心调用 ----
    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        json_mode: bool = False,
        retries: int = 2,
    ) -> LLMResult:
        """发送一次 chat 补全。失败按类型抛分层异常。"""
        if not self.enabled:
            raise LLMUnavailable("未配置 LLM_API_KEY")

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature if temperature is None else temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        last: Exception = LLMUnavailable("LLM 未响应")
        for attempt in range(retries + 1):
            try:
                resp = self._http().post(
                    "/chat/completions",
                    headers={"Authorization": f"Bearer {self._settings.llm_api_key}"},
                    json=payload,
                )
            except httpx.HTTPError as exc:
                last = LLMUnavailable(f"LLM 网络错误: {exc}")
                self._backoff(attempt, exc)
                continue

            if resp.status_code == 200:
                result = self._parse(resp.json())
                self.last_usage = result.usage
                return result

            if resp.status_code == 429:
                last = LLMRateLimited(f"LLM 限流(429): {self._snippet(resp)}")
                self._backoff(attempt, last)
                continue

            if resp.status_code >= 500:
                last = LLMUnavailable(f"LLM 服务端错误({resp.status_code}): {self._snippet(resp)}")
                self._backoff(attempt, last)
                continue

            # 400 等客户端错误：重试无意义，直接抛
            raise LLMError(f"LLM 请求错误({resp.status_code}): {self._snippet(resp)}")

        raise last

    def chat_json(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        retries: int = 2,
    ) -> dict[str, Any]:
        """请求结构化 JSON，返回解析后的 dict。

        优先用 `response_format=json_object`；若仍拿到非 JSON（个别模型不支持），
        退化为「从文本中提取首个 {...} 块」再解析，仍失败则抛 LLMError。
        """
        result = self.chat(messages, temperature=temperature, max_tokens=max_tokens,
                           json_mode=True, retries=retries)
        return self._extract_json(result.content)

    # ---- 内部 ----
    @staticmethod
    def _snippet(resp: httpx.Response) -> str:
        return (resp.text or "")[:200]

    @staticmethod
    def _backoff(attempt: int, exc: Exception) -> None:
        delay = min(0.5 * (2 ** attempt), 2.0)
        log.warning("LLM 调用失败（第 %d 次重试前），%.2fs 后重试: %s", attempt + 1, delay, exc)
        time.sleep(delay)

    @staticmethod
    def _parse(body: dict[str, Any]) -> LLMResult:
        try:
            choice = body["choices"][0]["message"]
            content = choice.get("content") or ""
            usage = body.get("usage") or {}
            return LLMResult(
                content=str(content),
                prompt_tokens=int(usage.get("prompt_tokens", 0)),
                completion_tokens=int(usage.get("completion_tokens", 0)),
                total_tokens=int(usage.get("total_tokens", 0)),
                model=str(body.get("model", "")),
            )
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"LLM 响应结构异常: {exc}") from exc

    @staticmethod
    def _extract_json(text: str) -> dict[str, Any]:
        raw = (text or "").strip()
        # 去掉可能包裹的 markdown 代码块
        if raw.startswith("```"):
            raw = raw.strip("`")
            if raw.lower().startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            start, end = raw.find("{"), raw.rfind("}")
            if start == -1 or end <= start:
                raise LLMError(f"LLM 未返回合法 JSON: {raw[:120]!r}")
            try:
                obj = json.loads(raw[start : end + 1])
            except json.JSONDecodeError as exc:
                raise LLMError(f"LLM 返回的 JSON 无法解析: {exc}") from exc
        if not isinstance(obj, dict):
            raise LLMError("LLM 返回的 JSON 顶层不是对象")
        return obj


@lru_cache
def get_client() -> LLMClient:
    return LLMClient()
