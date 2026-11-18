"""槽位抽取的回归用例。

重点是「跨型号作答」这条：抽不到型号时如果继续按症状作答，用户问 X5 PRO，
系统会拿 T20 PRO 的结论回答，而且答案里看不出型号不匹配——这是最隐蔽的一类越界。
"""

from __future__ import annotations

import pytest

from backend.app.agent.slots import extract_slots


@pytest.fixture(scope="module")
def store():
    from backend.app.kg import get_store

    return get_store()


def test_t_series_resolved(store):
    s = extract_slots(store, "T20 PRO 报 E11，左轮不转")
    assert s.model == "T20 PRO"
    assert s.model_unknown is None
    assert s.code == "E11"


def test_t_series_prefix_completed(store):
    s = extract_slots(store, "T20 报 E11，左轮不转")
    assert s.model == "T20 PRO"


def test_unknown_t_series_kept_as_unknown(store):
    s = extract_slots(store, "T30 PRO 报 E11，左轮不转")
    assert s.model_unknown == "T30 PRO"


def test_unknown_x_series_kept_as_unknown(store):
    """X 系列不在 MODEL_RE 覆盖范围内，必须由兜底正则认出，否则会跨型号作答。"""
    s = extract_slots(store, "X5 PRO 基站漏水，怎么处理？")
    assert s.model_unknown == "X5 PRO"


def test_unknown_n_series_kept_as_unknown(store):
    s = extract_slots(store, "N8 报 E13 边刷不转")
    assert s.model_unknown == "N8"


def test_error_code_not_mistaken_for_model(store):
    """E11 / 10010 是错误码，不能被兜底正则当型号。"""
    assert extract_slots(store, "扫地机报 E11 左轮不转").model_unknown is None
    assert extract_slots(store, "报 10010 配网失败").model_unknown is None


def test_no_model_mentioned_stays_none(store):
    """压根没提型号时不能伪造一个 unknown，否则所有无型号提问都会被拒绝。"""
    s = extract_slots(store, "左轮不转，报 E11")
    assert s.model is None and s.model_unknown is None
