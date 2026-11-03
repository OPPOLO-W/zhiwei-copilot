# 智维 Copilot 常用命令
# Windows 上如果没有 make，用等价的 python -m 命令（README 1.4 节有完整清单）。
#
#   make dev     起后端（热重载）
#   make seed    重建图谱（种子 + 合成 → data/curated）
#   make eval    跑五组系统 x 3 次，出均值±标准差
#   make test    跑测试
#
# 虚拟环境不叫 .venv 时覆盖：make PY=python3 seed

PY ?= .venv/Scripts/python.exe
ifeq ($(OS),)
PY := python3
endif

COMPOSE ?= docker compose
DATA ?= data/seed
NEO4J_URI ?= bolt://localhost:7687

.PHONY: help up down logs dev seed extract validate load eval eval-rule test docs check fmt

help:
	@echo "up        docker compose 起 Neo4j + 后端 + 前端"
	@echo "down      停掉并保留数据卷"
	@echo "dev       本机起后端（uvicorn --reload）"
	@echo "seed      重建图谱：种子 + 合成 → data/curated"
	@echo "extract   抽取：documents/ → data/raw → data/extracted"
	@echo "validate  9 类图谱质量校验"
	@echo "load      data/curated → Neo4j（--reset 先清空）"
	@echo "eval      五组系统 x 3 次评测"
	@echo "test      pytest"
	@echo "docs      由 schema.yaml 重新生成 docs/ontology.md"

# ---------- 容器 ----------
up:
	$(COMPOSE) up -d
	@echo "Neo4j:    http://localhost:7474"
	@echo "后端:     http://localhost:8000/docs"
	@echo "前端:     http://localhost:5173"
	@echo "首次启动请跑 make seed && make load"

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f backend

# ---------- 本机开发 ----------
dev:
	$(PY) -m uvicorn backend.app.main:app --reload --port 8000

# ---------- 数据 ----------
# 三步分开是刻意的：抽取失败和构建失败是两类问题，混在一条命令里就分不清
extract:
	$(PY) -m backend.app.ingest.build
	$(PY) -m backend.app.extract.pipeline

seed:
	$(PY) -m backend.tools.gen_faq_review --check
	$(PY) -m backend.app.kg.build_seed --seed $(DATA)

validate:
	$(PY) -m backend.app.kg.validate

load:
	$(PY) -m backend.app.kg.loader --reset

# ---------- 实验 ----------
# 文档 9.3 要求 3 次重复报均值±标准差；本实现确定性，标准差预期为 0
eval:
	$(PY) -m backend.app.eval.run_eval --all --repeat 3

eval-rule:
	$(PY) -m backend.app.eval.run_eval --system rule

test:
	$(PY) -m pytest backend/tests -q

docs:
	$(PY) -m backend.tools.gen_ontology_doc

# 文档与本体是否同步（CI 用；不同步就非零退出）
check:
	$(PY) -m backend.tools.gen_ontology_doc --check
	$(PY) -m backend.tools.gen_faq_review --check
