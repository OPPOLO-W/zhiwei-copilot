"""集中配置。所有路径基于仓库根目录，避免依赖启动位置。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
ONTOLOGY_PATH = DATA_DIR / "ontology" / "schema.yaml"
SEED_DIR = DATA_DIR / "seed"
CURATED_DIR = DATA_DIR / "curated"
EVAL_DIR = DATA_DIR / "eval"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Neo4j
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "neo4j"
    neo4j_database: str = "neo4j"
    kg_readonly: bool = True

    # 图存储后端
    kg_backend: Literal["neo4j", "memory"] = "neo4j"

    # LLM
    llm_api_key: str = ""
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_model: str = "deepseek-chat"
    llm_temperature: float = 0.0

    # 运行参数
    agent_max_steps: int = 6
    agent_max_reflections: int = 2
    agent_token_budget: int = 24000
    # 单个工具调用的墙钟上限（秒）。<=0 表示不设限，用于对照实验。
    agent_tool_timeout_s: float = 6.0
    rule_max_iterations: int = 1000
    rule_max_depth: int = 8

    cors_origins: str = "http://localhost:5173"
    trace_dir: Path = Field(default=ROOT / "var" / "traces")

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm_api_key.strip())

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
