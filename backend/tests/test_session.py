"""会话状态（P2.2）：续用、槽位继承、事实去重与过期淘汰。"""

from __future__ import annotations

from backend.app.agent.session import SessionStore


def test_get_or_create_reuses_same_session():
    store = SessionStore()
    first = store.get_or_create(None, {"model": "T20 PRO"})
    second = store.get_or_create(first.session_id)
    assert second.session_id == first.session_id
    assert second.turn == 2, "续用一次算一轮"


def test_merge_slots_never_overwrites_existing_value():
    store = SessionStore()
    s = store.get_or_create(None, {"model": "T20 PRO", "code": "E11"})
    store.merge_slots(s, {"model": "T30 PRO", "symptom": "左轮不转"})
    assert s.slots["model"] == "T20 PRO", "后续识别不能覆盖已确认的型号"
    assert s.slots["symptom"] == "左轮不转"


def test_add_facts_dedupes_same_quote():
    store = SessionStore()
    s = store.get_or_create(None)
    fact = {"kind": "action", "nodeId": "SOL:CLEAN-WHEEL", "quote": "毛发清理过了"}
    store.add_facts(s, [fact, dict(fact)])
    assert len(s.facts) == 1


def test_expired_session_is_dropped():
    store = SessionStore(ttl=60)
    s = store.get_or_create(None)
    s.last_seen -= 120
    assert store.get(s.session_id) is None


def test_capacity_evicts_least_recently_seen():
    store = SessionStore(cap=2)
    a = store.get_or_create(None)
    b = store.get_or_create(None)
    a.last_seen -= 100
    store.get_or_create(None)  # 触发淘汰：a 最久未用
    assert store.get(a.session_id) is None
    assert store.get(b.session_id) is not None
