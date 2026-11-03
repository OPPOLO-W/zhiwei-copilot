"""pytest 公共夹具。

测试统一跑在 MemoryStore 上：数据来自 data/curated 的同一份 jsonl，
与 Neo4j 导入源一致，因此不需要启动 Neo4j 也能验证推理与接口契约。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("KG_BACKEND", "memory")

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

from backend.app.kg import get_store, reset_store  # noqa: E402


@pytest.fixture(scope="session")
def store():
    reset_store()
    return get_store()


def current_models(store) -> list[str]:
    """语料里实际存在的型号。测试不写死机型，换语料不用改断言。"""
    return sorted(n.props["modelCode"] for n in store.nodes("ProductModel"))


@pytest.fixture(scope="session")
def model(store):
    """当前语料的主型号（取排序后的第一个）。"""
    models = current_models(store)
    assert models, "图谱中没有 ProductModel，检查 data/curated 是否已构建"
    return models[0]


@pytest.fixture(scope="session")
def other_model(store):
    """语料中并不存在的型号，专门用来验证降级路径。"""
    return "NOT-A-MODEL"


@pytest.fixture(scope="session")
def first_case_no(store):
    """按编号取第一个**可诊断**案例（带观察症状），避免测试写死编号。

    接入真实官方 FAQ 后图里多了保养类案例卡（如「如何清理边刷」），
    它们没有故障症状是设计使然，按编号排序的第一个恰好是这类，
    因此这里要求案例带 OBSERVED 边；无症状案例的路径由
    test_agent_case_detail_without_symptom 单独覆盖。
    """
    cases = sorted(
        n.props["caseNo"] for n in store.nodes("FAQCase")
        if store.neighbors(n.id, ["OBSERVED"], "out", 1)
    )
    assert cases, "图谱中没有可诊断的 FAQCase"
    return cases[0]



@pytest.fixture(scope="session")
def client():
    from fastapi.testclient import TestClient

    from backend.app.main import app

    with TestClient(app) as c:
        yield c
