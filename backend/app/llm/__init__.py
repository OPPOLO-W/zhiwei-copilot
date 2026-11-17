"""LLM 接入层：OpenAI 兼容客户端 + Prompt/结构化输出。

设计原则（对应开发文档 8.10）：
  * 客户端是薄封装，走 OpenAI 兼容的 /chat/completions，不引入 openai SDK；
  * 所有结构化输出用 Pydantic 校验，非法 JSON 一律回退到确定性路径；
  * 文档内容只作为数据注入，包在明确分隔符中，忽略文档里的任何指令。
"""

from .client import (
    LLMError,
    LLMRateLimited,
    LLMResult,
    LLMUnavailable,
    LLMClient,
    get_client,
)

__all__ = [
    "LLMError",
    "LLMRateLimited",
    "LLMResult",
    "LLMUnavailable",
    "LLMClient",
    "get_client",
]
