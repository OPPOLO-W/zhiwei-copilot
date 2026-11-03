"""FAQ 适配器：一篇「问题 + 若干步骤」→ 症状、案例卡与有序处置链。

为什么单独一个模块
------------------
手册的故障表是**三列结构化数据**（故障 / 原因 / 方法），规则抽取即可；
FAQ 是**自然语言叙述**，但结构比想象中规整：一个问句 + 一串编号步骤。
这类文本不需要 LLM 也能稳定抽，前提是把「哪句是标题、哪句是步骤、哪句是注释」分清，
这件事在 ingest 层已经做完了（chunk.kind = heading / list_item / paragraph）。

抽出来的四类东西
----------------
1. **Symptom**：FAQ 标题就是用户口吻的现象，是自然语言进图的入口；
2. **FAQCase**：一篇 FAQ 一张案例卡，承载原文全文与出处链接，保证可回溯；
3. **Solution**：每个编号步骤一个方案，用 PRECEDES 串成执行顺序；
4. **INDICATES**：FAQ 症状与手册故障表做模糊对齐，这是双源对账的产物——
   相似度只作为**候选**（verification=pending），不冒充已确认的事实。
"""

from __future__ import annotations

import re
from typing import Any

from rapidfuzz import fuzz

from .candidates import CandidateEdge, CandidateNode, Extraction, NodeRef
from .extractors import (
    CONSUMABLE_LABELS,
    _CHECK_RE,
    _is_self_service,
    classify_category,
    components_in,
    norm_name,
)

# 标题以这些词开头，说明用户是在问「怎么做」而不是「坏了怎么办」
_OPERATION_PREFIX = ("如何", "怎么", "怎样", "什么是", "哪些", "能否")

# 只补充条件、不构成动作的行，作为症状备注而非处置步骤
_NOTE_PREFIX = ("注", "温馨提示", "提示", "说明", "备注", "注意")

_PUNCT = re.compile(r"[\s，。；、：:（）()【】\[\]“”\"'!！?？~·…—－]")

# 可执行动作。FAQ 里不少条目其实是**参数说明**（「单扫模式约 180min」「满电到电量低状态」），
# 混进步骤会让 Agent 输出一份「第 5 步：续航 180 分钟」的清单。
# 判据取动词而非句式：中文步骤常省略主语，「取下尘袋」四个字就是完整一步。
_ACTION_WORDS = (
    "打开", "关闭", "点击", "选择", "进入", "设置", "长按", "短按", "取出", "安装",
    "清理", "确认", "检查", "连接", "切换", "放置", "擦拭", "倾倒", "更换", "拔出",
    "启动", "等待", "观察", "联系", "下载", "登录", "扫描", "滑动", "按压", "提起",
    "翻转", "撕下", "装回", "调节", "调整", "开启", "插入", "重启", "复位", "重置",
    "关机", "开机", "取下", "拉出", "推入", "拆除", "拧", "擦干", "晾干", "充电",
    "配网", "绑定", "解绑", "唤醒", "说", "唤出", "保存", "填写", "输入",
)


def _looks_like_action(text: str) -> bool:
    return any(w in text for w in _ACTION_WORDS)


def slug(text: str, limit: int = 48) -> str:
    """业务键：去标点空白、保留中文，保证「同文同键」。"""
    return _PUNCT.sub("", text or "")[:limit]


def faq_type(title: str) -> str:
    """operation（操作指导）/ troubleshoot（故障排查）。

    两者都建成 Symptom——都是「用户带着一句话来求助」的入口，
    但用 faqType 区分，规则推理只走 troubleshoot 那一支。
    """
    t = (title or "").strip()
    return "operation" if t.startswith(_OPERATION_PREFIX) else "troubleshoot"


def _alias_of(title: str) -> str:
    """从问句里剥掉「如何处理」这类尾巴，留下现象本身，提升实体链接召回。

    剥完与原标题只差标点就没意义（那只是去了问号），返回空串不凑数。
    """
    core = re.sub(
        r"[，,]?\s*(?:该如何|应如何|怎么|如何)?(?:处理|解决|办|操作|设置)?\s*[？?]?$", "", title
    )
    core = norm_name(core)
    if not core or slug(core) == slug(title):
        return ""
    return core


def _solution_type(text: str) -> str:
    """方案类型（对应 Solution.type 枚举）；判不出就不写，不硬猜。"""
    if any(k in text for k in ("联系", "售后", "报修", "维修", "返厂")):
        return "escalate"
    if any(k in text for k in ("重启", "复位", "重置", "关机", "开机", "断电", "恢复出厂", "重新开机")):
        return "reset"
    # 只认「拆装类」动作；裸「安装」会把「下载 APP（安装时请授权定位）」误判成换件
    if any(k in text for k in ("更换", "换新", "装回", "重新安装", "拆下", "拆卸", "取下",
                               "撕下", "装到位")):
        return "replace"
    if any(k in text for k in ("设置", "APP", "app", "配网", "连接", "选择", "打开", "开启",
                               "切换", "进入", "下载", "登录")):
        return "reconfigure"
    if any(k in text for k in ("清洁", "清理", "擦", "清洗", "冲洗", "晾干", "倾倒")):
        return "clean"
    return ""


def _is_note(text: str) -> bool:
    return text.startswith(_NOTE_PREFIX) and len(text) >= 6


def _is_note_section(section: str) -> bool:
    """整节都处在「温馨提示 / 注 / 说明」之下：这些条目是前置条件与禁令，不是动作。

    例如配网一文里，「路由器不能隐藏 Wi-Fi 信号」是能否配网成功的前提，
    把它当成第 5 步会让 Agent 输出一份「按顺序执行就会失败」的清单。
    """
    s = (section or "").strip("*:： 　")
    return s.startswith(_NOTE_PREFIX) and len(s) <= 6


def _step_text(chunk: dict[str, Any]) -> str:
    t = (chunk.get("text") or "").strip()
    return norm_name(t)


def extract_faq_doc(
    doc: dict[str, Any],
    chunks: list[dict[str, Any]],
    product: str,
    fault_names: list[str],
    known_components: list[tuple[str, str, str]],
    min_fault_score: float = 0.62,
) -> Extraction:
    """一篇 FAQ → Symptom + FAQCase + 有序 Solution 链。"""
    ext = Extraction()
    doc_id = doc["doc_id"]
    title = norm_name(doc.get("title") or "")
    url = doc.get("url") or ""
    if not title or not chunks:
        return ext

    sym_code = slug(title)
    if not sym_code:
        return ext
    ftype = faq_type(title)
    locator_base = f"{title}"

    # ---- 症状 ----------------------------------------------------------
    notes = [
        c["text"] for c in chunks
        if c.get("kind") != "heading"
        and (_is_note(c["text"]) or _is_note_section(c.get("section", "")))
    ]
    # 先切出步骤，才能把「不是动作」的条目归到备注里（见 _looks_like_action）
    candidates = [
        c for c in chunks
        if c.get("kind") == "list_item"
        and not _is_note(c["text"]) and not _is_note_section(c.get("section", ""))
        and len(_step_text(c)) >= 4
    ]
    spec_lines = [c["text"] for c in candidates if not _looks_like_action(_step_text(c))]
    steps = [c for c in candidates if _looks_like_action(_step_text(c))]
    if not steps:  # 少数 FAQ 通篇是段落，退回用段落当步骤
        steps = [
            c for c in chunks
            if c.get("kind") == "paragraph" and not _is_note(c["text"])
            and not _is_note_section(c.get("section", ""))
            and 8 <= len(_step_text(c)) <= 160
        ]
        spec_lines = []
    notes.extend(spec_lines[:6])

    desc = next(
        (c["text"] for c in chunks
         if c.get("kind") == "paragraph" and not _is_note(c["text"]) and len(c["text"]) >= 10),
        "",
    )
    alias = _alias_of(title)
    ext.add_node(CandidateNode(
        label="Symptom", key=sym_code, name=title,
        props={
            "name": title, "code": sym_code,
            "faqType": ftype,
            "category": classify_category(title),
            "sourceDoc": doc_id, "docUrl": url,
            "evidenceMode": "verbatim",
            **({"description": desc} if desc else {}),
            **({"note": "；".join(notes)[:300]} if notes else {}),
        },
        aliases=[alias] if alias else [],
        evidence=title, locator=f"{locator_base} · 标题",
        doc_id=doc_id, chunk_id=(chunks[0].get("chunk_id") or ""),
        confidence=0.96, extractor="faq/symptom",
    ))
    ext.add_edge(CandidateEdge(
        type="DOCUMENTED_IN", head=NodeRef("Symptom", sym_code),
        tail=NodeRef("Document", doc_id),
        props={"evidenceMode": "verbatim", "url": url},
        evidence=title, locator=f"{locator_base} · 标题",
        doc_id=doc_id, confidence=0.96, extractor="faq/symptom",
    ))

    # 标题里点到的部件 → 症状发生于该部件
    for comp, label in components_in(title, known_components):
        ext.add_edge(CandidateEdge(
            type="OCCURS_IN", head=NodeRef("Symptom", sym_code),
            tail=NodeRef(label, comp),
            props={"evidenceMode": "verbatim"},
            evidence=title, locator=f"{locator_base} · 标题",
            doc_id=doc_id, confidence=0.75, extractor="faq/symptom",
        ))


    case_no = "FAQ-" + doc_id.rsplit("-", 1)[-1]
    raw_text = " ".join(c["text"] for c in chunks if c.get("kind") != "heading")
    prev_ref: NodeRef | None = None
    first_ref: NodeRef | None = None
    solved_faults = (
        _match_faults(title, fault_names, known_components, min_fault_score)
        if ftype == "troubleshoot" else []
    )
    solved_fault = solved_faults[0] if solved_faults else None

    for i, c in enumerate(steps, start=1):
        text = _step_text(c)
        code = f"{case_no}-S{i}"
        ref = NodeRef("Solution", code)
        stype = _solution_type(text)
        ext.add_node(CandidateNode(
            label="Solution", key=code, name=text,
            props={
                "name": text, "code": code,
                "stepIndex": i,
                "selfServiceable": _is_self_service(text),
                "sourceDoc": doc_id, "docUrl": url,
                "evidenceMode": "verbatim",
                **({"type": stype} if stype else {}),
                **({"section": c.get("section", "")} if c.get("section") else {}),
            },
            evidence=text, locator=f"{locator_base} · {c.get('section') or '正文'} 第{i}步",
            doc_id=doc_id, chunk_id=c.get("chunk_id") or "",
            confidence=0.9, extractor="faq/step",
        ))
        ext.add_edge(CandidateEdge(
            type="DOCUMENTED_IN", head=ref, tail=NodeRef("Document", doc_id),
            props={"evidenceMode": "verbatim", "url": url},
            evidence=text, locator=f"{locator_base} · 第{i}步",
            doc_id=doc_id, confidence=0.9, extractor="faq/step",
        ))

        # 步骤顺序：PRECEDES 把「先做什么、再做什么」显式化，
        # 这是 Agent 输出可执行清单而不是一堆方案的关键。
        if prev_ref is not None:
            ext.add_edge(CandidateEdge(
                type="PRECEDES", head=prev_ref, tail=ref,
                props={"caseNo": case_no, "evidenceMode": "verbatim"},
                evidence=f"{prev_ref.key} → {code}",
                locator=f"{locator_base} · 第{i - 1}→{i}步",
                doc_id=doc_id, confidence=0.92, extractor="faq/order",
            ))
        else:
            first_ref = ref
        prev_ref = ref

        # 步骤里用到的耗材/备件
        for comp, label in components_in(text, known_components):
            rel = "REQUIRES_CONSUMABLE" if label in CONSUMABLE_LABELS else (
                "REQUIRES_PART" if label == "SparePart" else "")
            if rel:
                ext.add_edge(CandidateEdge(
                    type=rel, head=ref, tail=NodeRef(label, comp),
                    props={"evidenceMode": "verbatim"},
                    evidence=text, locator=f"{locator_base} · 第{i}步",
                    doc_id=doc_id, confidence=0.75, extractor="faq/step",
                ))

        # 「检查…是否」→ 可执行鉴别项
        for m in _CHECK_RE.finditer(text):
            check_name = norm_name(m.group("body"))
            if len(check_name) < 4:
                continue
            chk_code = slug(check_name)
            ext.add_node(CandidateNode(
                label="Check", key=chk_code, name=check_name,
                props={"name": check_name, "code": chk_code,
                       "method": text, "sourceDoc": doc_id,
                       "evidenceMode": "verbatim"},
                evidence=m.group(0), locator=f"{locator_base} · 第{i}步",
                doc_id=doc_id, confidence=0.72, extractor="faq/check",
            ))
            if solved_fault:
                ext.add_edge(CandidateEdge(
                    type="DETECTS", head=NodeRef("Check", chk_code),
                    tail=NodeRef("Fault", solved_fault[0]),
                    props={"evidenceMode": "inferred", "verification": "pending"},
                    evidence=m.group(0), locator=f"{locator_base} · 第{i}步",
                    doc_id=doc_id, confidence=0.6, extractor="faq/check",
                ))

    # ---- 案例卡 --------------------------------------------------------
    ext.add_node(CandidateNode(
        label="FAQCase", key=case_no, name=title,
        props={
            "caseNo": case_no, "sourceType": "official_faq",
            "title": title,
            "rawText": raw_text[:1200],
            "resolution": (steps[-1]["text"] if steps else ""),
            "symptom": sym_code, "product": product,
            "docId": doc_id, "url": url,
            "stepCount": len(steps),
        },
        evidence=title, locator=f"{locator_base} · 案例卡",
        doc_id=doc_id, confidence=0.95, extractor="faq/case",
    ))
    ext.add_edge(CandidateEdge(
        type="OBSERVED", head=NodeRef("FAQCase", case_no),
        tail=NodeRef("Symptom", sym_code),
        props={"evidenceMode": "verbatim"},
        evidence=title, locator=f"{locator_base} · 案例卡",
        doc_id=doc_id, confidence=0.95, extractor="faq/case",
    ))
    ext.add_edge(CandidateEdge(
        type="ABOUT", head=NodeRef("FAQCase", case_no),
        tail=NodeRef("ProductModel", product),
        props={"evidenceMode": "inferred"},
        evidence=f"{product} 官方 FAQ",
        locator=f"{locator_base} · 案例卡",
        doc_id=doc_id, confidence=0.9, extractor="faq/case",
    ))
    if first_ref is not None:
        ext.add_edge(CandidateEdge(
            type="RESOLVED_BY", head=NodeRef("FAQCase", case_no),
            tail=first_ref,
            props={"evidenceMode": "verbatim", "note": "从第一步开始执行"},
            evidence=first_ref.key, locator=f"{locator_base} · 第1步",
            doc_id=doc_id, confidence=0.9, extractor="faq/case",
        ))

    # ---- 与手册故障表对账 ----------------------------------------------
    for fault_name, score, why in solved_faults:
        ext.add_edge(CandidateEdge(
            type="INDICATES", head=NodeRef("Symptom", sym_code),
            tail=NodeRef("Fault", fault_name),
            props={"evidenceMode": "inferred", "verification": "pending",
                   "matchScore": round(score, 3), "matchMethod": why},
            evidence=f"FAQ 标题「{title}」≈ 手册故障「{fault_name}」（{why}, {score:.2f}）",
            locator=f"{locator_base} · 标题",
            doc_id=doc_id, confidence=round(score, 3), extractor="faq/align",
        ))
    return ext


def _match_faults(
    title: str,
    fault_names: list[str],
    known_components: list[tuple[str, str, str]] | None = None,
    min_score: float = 0.65,
    max_keep: int = 2,
    margin: float = 0.05,
) -> list[tuple[str, float, str]]:
    """把 FAQ 标题对齐到手册故障名，返回若干 (故障名, 分数, 依据)，按分降序。

    只用字符相似度不够：中文短句换个说法就几乎不共享字符
    （「主机无法充电」vs「机器人充不进电」，逐字相似度只有 0.3），
    但这种显然是同一件事。因此按「部件 + 症状动作」两层判定，
    字面相似度退为兜底；判定依据一并返回，便于人工复核。

    保留 top-k 而不是只留最好的一条：手册里「自动集尘不成功」与
    「返回基站后未开始自动集尘」是两条独立故障，却对应同一篇 FAQ，
    只留一条会让另一条在覆盖统计里被误判成「FAQ 没写到」。
    """
    if not fault_names:
        return []
    scored = [
        (name, *_fault_similarity(title, name, known_components or []))
        for name in fault_names
    ]
    scored.sort(key=lambda x: -x[1])
    best = scored[0][1]
    if best < min_score:
        return []
    return [s for s in scored[:max_keep] if s[1] >= min_score and s[1] >= best - margin]


# 症状动作的等价说法。表里任一词命中即视为同一个症状类别。
SYMPTOM_PATTERNS: list[tuple[str, tuple[str, ...]]] = [
    ("不转", ("不转", "无法旋转", "时转时不转", "不转动", "转不动")),
    ("无法充电", ("无法充电", "充不进电", "充不上电", "不能充电", "充不了电")),
    ("无法返回", ("无法返回", "回不去", "找不到基站", "上不去", "未回到基站")),
    ("水路", ("水满", "无法吸出", "无法回收", "不出水", "漏水", "污水", "清水")),
    ("集尘", ("集尘失败", "自动集尘", "不集尘", "未集尘", "集尘不成功", "尘袋")),
    ("漏扫", ("漏扫", "漏灰", "漏尘")),
    ("地图", ("地图丢失", "地图混乱", "叠图", "建图", "创建地图", "地图")),
    ("连接", ("离线", "断开连接", "无法连接", "配网失败", "无法上网", "无法加入",
             "连接网络", "断开")),
    ("噪音", ("声音大", "异响", "噪音", "声音")),
    ("被困", ("陷入困境", "被阻挡", "运行困难", "卡住", "脱困", "卡在")),
    ("无响应", ("不响应", "无响应", "没反应", "不执行", "无法控制", "控制不了")),
    ("未完成", ("未清扫完毕", "返回充电", "断点续扫", "未完成")),
]


def _symptom_tags(text: str) -> set[str]:
    return {label for label, keys in SYMPTOM_PATTERNS if any(k in text for k in keys)}


def _fault_similarity(
    a: str, b: str, known: list[tuple[str, str, str]]
) -> tuple[float, str]:
    """两层判定：部件是否相同 + 症状动作是否同类。"""
    comps_a = {c for c, _ in components_in(a, known)}
    comps_b = {c for c, _ in components_in(b, known)}
    shared = comps_a & comps_b
    tags_a, tags_b = _symptom_tags(a), _symptom_tags(b)
    shared_tags = tags_a & tags_b

    if shared and shared_tags:
        return 0.92, f"部件{next(iter(shared))}+症状{next(iter(shared_tags))}"
    if shared_tags:
        return 0.76, f"症状{next(iter(shared_tags))}"
    if shared:
        return 0.52, f"仅部件{next(iter(shared))}"
    lit = fuzz.partial_ratio(a, b) / 100.0
    return round(lit * 0.9, 3), f"字面{lit:.2f}"
