"""会话状态：多轮排查的槽位、已采信事实与已问检查（P2.2）。

设计约束：
  * 进程内存储，与 `RUNS` 同级别——本项目是课程演示，不引入 Redis；
  * TTL + 容量上限，避免长跑进程内存泄漏；
  * 只存「用户说过什么」与「系统问过什么」，不缓存推理结论：
    结论必须每轮按当前事实重新推，否则会话里的旧答案会掩盖新事实。
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

SESSION_TTL_SECONDS = 30 * 60
SESSION_CAP = 500


@dataclass
class Session:
    session_id: str
    slots: dict[str, Any] = field(default_factory=dict)
    facts: list[dict[str, Any]] = field(default_factory=list)
    asked_checks: list[str] = field(default_factory=list)
    turn: int = 0
    last_run_id: str | None = None
    created_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {"sessionId": self.session_id, "turn": self.turn,
                "slots": self.slots, "facts": self.facts,
                "askedChecks": self.asked_checks}


class SessionStore:
    """带 TTL 与容量上限的进程内会话表（线程安全）。"""

    def __init__(self, ttl: int = SESSION_TTL_SECONDS, cap: int = SESSION_CAP) -> None:
        self.ttl = ttl
        self.cap = cap
        self._items: dict[str, Session] = {}
        self._lock = threading.Lock()

    def get(self, session_id: str | None) -> Session | None:
        if not session_id:
            return None
        with self._lock:
            session = self._items.get(session_id)
            if session is None:
                return None
            if time.time() - session.last_seen > self.ttl:
                self._items.pop(session_id, None)
                return None
            session.last_seen = time.time()
            session.turn += 1
            return session

    def create(self, slots: dict[str, Any] | None = None) -> Session:
        # 只存已识别的槽位：全 None 的骨架 dict 会让「会话已存在」被误判为「已有上下文」
        known = {k: v for k, v in (slots or {}).items() if v}
        session = Session(session_id=f"s_{uuid.uuid4().hex[:10]}", slots=known, turn=1)
        with self._lock:
            if len(self._items) >= self.cap:
                oldest = min(self._items.values(), key=lambda s: s.last_seen)
                self._items.pop(oldest.session_id, None)
            self._items[session.session_id] = session
        return session

    def get_or_create(self, session_id: str | None,
                      slots: dict[str, Any] | None = None) -> Session:
        """有 id 且未过期就续用，否则新建；返回的 session 已记为「本轮」。"""
        session = self.get(session_id)
        if session is not None:
            if slots:
                self.merge_slots(session, slots)
            return session
        return self.create(slots)

    def merge_slots(self, session: Session, slots: dict[str, Any]) -> None:
        """本轮新识别出的槽位补进会话；已有值不覆盖（用户后续修正走显式参数）。"""
        for key, value in (slots or {}).items():
            if value and not session.slots.get(key):
                session.slots[key] = value

    def add_facts(self, session: Session, facts: list[dict[str, Any]]) -> None:
        """按 (kind, nodeId, quote) 去重追加，避免同一句话反复申报。"""
        seen = {(f.get("kind"), f.get("nodeId"), f.get("quote")) for f in session.facts}
        for fact in facts or []:
            key = (fact.get("kind"), fact.get("nodeId"), fact.get("quote"))
            if key not in seen:
                session.facts.append(dict(fact))
                seen.add(key)

    def add_asked(self, session: Session, check_ids: list[str]) -> None:
        for check_id in check_ids or []:
            if check_id and check_id not in session.asked_checks:
                session.asked_checks.append(check_id)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


_STORE = SessionStore()


def get_session_store() -> SessionStore:
    return _STORE
