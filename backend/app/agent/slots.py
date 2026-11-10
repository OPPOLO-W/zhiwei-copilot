"""查询解耦：把自然语言问题拆成槽位 + 子问题。

无 LLM 时使用确定性规则（正则 + 别名词典），保证 demo 与评测可复现；
有 LLM 时可切换为结构化 Prompt，输出仍受同一 Pydantic 槽位约束。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..kg import queries as Q
from ..kg.model import Node
from ..kg.store import GraphStore

MODEL_RE = re.compile(r"[Tt]\s?(\d{2})\s*(PRO|Pro|pro|TURBO|Turbo|turbo)?")
CODE_RE = re.compile(r"(?<![A-Za-z0-9])(?:E\s?(\d{2,3})|(100\d{2}))(?!\d)")
ENV_WORDS = {
    "PET": ["养猫", "养狗", "宠物", "猫毛", "狗毛"],
    "LONG-HAIR": ["长发", "掉头发"],
    "CARPET": ["地毯", "流苏"],
    "DUPLEX": ["复式", "两层", "楼上楼下"],
    "LARGE-HOME": ["大户型", "一百多平", "大面积"],
    "BALCONY": ["阳台", "推拉门", "门槛"],
}
SAFETY_WORDS = ["鼓包", "漏液", "冒烟", "起火", "过热", "烫手", "进水", "异味"]
REPLACE_WORDS = ["换轮组", "换件", "换主板", "换电池", "更换", "拆机", "自己修"]


@dataclass
class Slots:
    question: str = ""
    model: str | None = None
    code: str | None = None
    symptom: str | None = None
    environment: str | None = None
    case_no: str | None = None
    intent: str = "troubleshoot"
    safety_hint: bool = False
    replace_intent: bool = False
    # 用户明确提到、但图谱未收录的型号原文。与 model=None（压根没提型号）区分开：
    # 前者必须走 unknown_model 降级，后者才允许按错误码/症状单独检索。
    model_unknown: str | None = None
    constraints: dict[str, Any] = field(default_factory=dict)
    unresolved: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "model": self.model,
            "code": self.code,
            "symptom": self.symptom,
            "environment": self.environment,
            "caseNo": self.case_no,
            "intent": self.intent,
            "safetyHint": self.safety_hint,
            "replaceIntent": self.replace_intent,
            "unknownModel": self.model_unknown,
            "constraints": self.constraints,
            "unresolved": self.unresolved,
        }


def _resolve_model_prefix(store: GraphStore, candidate: str) -> Node | None:
    """补全省略后缀的型号（T20 → T20 PRO）。

    只在唯一命中时补全；命中 0 个或 ≥2 个都返回 None，避免猜错型号。
    """
    prefix = candidate.upper()
    hits = [
        n for n in store.nodes("ProductModel")
        if str(n.props.get("modelCode", "")).upper().startswith(prefix)
    ]
    return hits[0] if len(hits) == 1 else None


def extract_slots(store: GraphStore, question: str) -> Slots:
    slots = Slots(question=question)

    m = MODEL_RE.search(question)
    if m:
        suffix = (m.group(2) or "").upper()
        candidate = f"T{m.group(1)}" + (f" {suffix}" if suffix else "")
        node = Q.resolve_model(store, candidate) or _resolve_model_prefix(store, candidate)
        if node:
            slots.model = node.props.get("modelCode")
        else:
            # 保留原文而不是置 None：下游据此走 unknown_model 降级，
            # 否则「T30 PRO 的 E11」会被静默当成「T20 PRO 的 E11」作答。
            slots.model = candidate
            slots.model_unknown = candidate
            slots.unresolved.append(f"型号 {candidate} 不在图谱范围内")

    c = CODE_RE.search(question.upper().replace(" ", ""))
    if c:
        # 分支一捕获「E11 / E 11」里的数字，需补回 E 前缀；分支二本身即完整网络码（10010）
        slots.code = f"E{c.group(1)}" if c.group(1) else c.group(2)

    # 症状：优先匹配规范名与别名，取最长命中，避免「轮」这类短词误配
    best, best_len = None, 0
    for node in store.nodes("Symptom"):
        names = [node.name, str(node.props.get("code", ""))] + list(node.props.get("aliases") or [])
        for name in names:
            if name and name in question and len(name) > best_len:
                best, best_len = node, len(name)
    if best is not None:
        slots.symptom = best.name

    for code, words in ENV_WORDS.items():
        if any(w in question for w in words):
            slots.environment = code
            break

    if slots.code is None and slots.symptom is None:
        # 再尝试从错误码字面值兜底（如「报 10010」被写成中文数字以外的形式）
        slots.unresolved.append("未识别到错误码或症状")

    slots.safety_hint = any(w in question for w in SAFETY_WORDS)
    slots.replace_intent = any(w in question for w in REPLACE_WORDS)
    if "配网" in question or "连不上" in question or "wifi" in question.lower():
        slots.intent = "network"
    elif slots.safety_hint:
        slots.intent = "safety"
    return slots


def sub_questions(slots: Slots) -> list[dict[str, Any]]:
    """RefKG-lite 的 Query Decoupling 产物：带 requiredEvidence 与 toolHint 的子问题。"""
    model = slots.model or "该型号"
    code = slots.code or "该错误码"
    return [
        {
            "id": "q1",
            "question": f"{code} 在 {model} 下对应哪些部件与故障？",
            "requiredEvidence": ["ErrorCode", "Symptom", "Fault", "Component"],
            "toolHint": "run_rules",
        },
        {
            "id": "q2",
            "question": "可能根因与检查顺序是什么？",
            "requiredEvidence": ["Cause", "Check"],
            "toolHint": "run_rules",
        },
        {
            "id": "q3",
            "question": f"同型号近期是否有类似案例，处置结果如何？",
            "requiredEvidence": ["Ticket", "PublicCase", "Symptom"],
            "toolHint": "search_similar_cases",
        },
        {
            "id": "q4",
            "question": "需要更换的耗材/备件是否兼容，什么条件下才需要更换？",
            "requiredEvidence": ["Consumable", "SparePart", "COMPATIBLE_WITH"],
            "toolHint": "check_compatibility",
        },
        {
            "id": "q5",
            "question": "是否需要停用、转官方售后或走保修？",
            "requiredEvidence": ["Safety", "Warranty", "ServiceAgent"],
            "toolHint": "run_rules",
        },
    ]
