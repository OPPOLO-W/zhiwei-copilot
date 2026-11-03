"""抽取器：从结构化表格与正文中抽出本体实例。

设计取舍
--------
**表格走确定性规则，正文才考虑 LLM。**
手册第 29–32 页的故障排查表本身已经是「故障 / 可能原因 / 解决方法」三列结构，
这类知识用 LLM 抽纯属浪费且不可复现——同样的输入每次可能抽出不同措辞，
反而破坏 evidence 的 verbatim 属性。规则抽取在这里能做到 100% 可核对。
正文（尤其 FAQ 的操作步骤）语义松散，才需要 LLM 兜底。

**词表只用来定位，不用来发明。**
组件词表是我按售后常识列的候选词，但只有**在原文中出现过**的才会入库
（见 `verified_components`），避免把「想象中该有的部件」写进图谱。
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from .candidates import CandidateEdge, CandidateNode, Extraction, NodeRef
from ..ingest.corpus import cell_flat

# ---------------------------------------------------------------------------
# 文本归一化
# ---------------------------------------------------------------------------

_TAIL_PUNCT = "。；;，,、：: 　\u3000"
_SPACE = re.compile(r"[ \t\u3000]+")

# 系列代号 = 型号名里数字前的字母前缀（T20 PRO → T）。
# 不写死任何具体系列，后续接入新机型手册时按同一规则自动归属。
_SERIES_RE = re.compile(r"^([A-Za-z]+)\s*\d")


def series_code(model: str) -> str:
    """从型号名推断系列代号；无法判断时返回空串（不臆造）。"""
    m = _SERIES_RE.match((model or "").strip())
    return m.group(1).upper() if m else ""


def norm_name(text: str) -> str:
    """实体名归一化：去句末标点、压空格、去星号注记。

    只做表层清理，**不改写语序与用词**——归一化一旦过度，
    证据与实体名就对不上，述谓就失去了可核查性。
    """
    t = _SPACE.sub(" ", text or "").strip()
    t = t.replace("*", "").replace("＊", "")
    t = t.strip(_TAIL_PUNCT)
    return t


def dedupe_key(text: str) -> str:
    """去重键：忽略标点与空白差异，避免「。检查」和「检查」重复建点。"""
    return re.sub(r"[\s，。；、：:（）()【】\[\]“”\"'!！?？]", "", text or "")


def _flat_cell(value: str, prose: bool | None) -> str:
    return cell_flat(value, prose=prose)


# ---------------------------------------------------------------------------
# 组件与耗材词表（仅作定位用，必须经原文验证）
# ---------------------------------------------------------------------------

# (规范名, 类别, 别名/原文变体)
COMPONENT_LEXICON: list[tuple[str, str, tuple[str, ...]]] = [
    ("主机", "Component", ()),
    ("全能基站", "Component", ("基站",)),
    ("基站斜坡底座", "Component", ("斜坡底座",)),
    ("边刷", "Consumable", ()),
    ("滚刷", "Consumable", ("主刷",)),
    ("滚刷盖板", "Component", ()),
    ("尘盒", "Component", ()),
    ("滤芯", "Consumable", ("滤网", "过滤材料")),
    ("可水洗拖布", "Consumable", ("拖布",)),
    ("拖布支架", "Component", ("拖布组件",)),
    ("尘袋", "Consumable", ("集尘袋",)),
    ("集尘仓", "Component", ()),
    ("清水箱", "Component", ()),
    ("污水箱", "Component", ()),
    ("清洁槽", "Component", ("清洁槽可拆卸底盘",)),
    ("上下水模块", "SparePart", ()),
    ("驱动轮", "Component", ()),
    ("万向轮", "Component", ()),
    ("下视感应器", "Component", ("下视传感器",)),
    ("沿边传感器", "Component", ("沿边传感器",)),
    ("缓冲防撞板", "Component", ("缓冲防撞板",)),
    ("充电极片", "Component", ("主机充电极片", "全能基站充电极片")),
    ("d-ToF 激光雷达模块", "Component", ("激光雷达模块", "d-ToF")),
    ("TrueDetect 3D 结构光传感器", "Component", ("TrueDetect 3D", "结构光传感器")),
    ("地毯传感器", "Component", ()),
    ("防撞传感器", "Component", ()),
    ("麦克风", "Component", ()),
    ("面盖", "Component", ()),
    ("清洁消毒剂", "Consumable", ("清洁剂",)),
]

# 同一部件的不同叫法（手册里「下视传感器」与「下视感应器」并存）统一到规范名，
# 否则同一实体会被建成两个节点，推理链会在这一处分叉。
_ALIAS_TO_CANON: dict[str, str] = {}
for _canon, _label, _aliases in COMPONENT_LEXICON:
    _ALIAS_TO_CANON[_canon] = _canon
    for _a in _aliases:
        _ALIAS_TO_CANON[_a] = _canon


def canonical_component(name: str) -> str:
    """把部件别名归一到规范名。"""
    return _ALIAS_TO_CANON.get(name.strip(), name.strip())


CONSUMABLE_LABELS = {"Consumable"}
PART_LABELS = {"SparePart"}

# 故障大类，按关键词优先级匹配（顺序即优先级）
CATEGORY_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("水路与上下水", ("清水箱", "污水箱", "清洁槽", "上水", "下水", "水压", "水路", "排污", "水满")),
    ("基站与集尘", ("集尘", "尘袋", "尘盒", "基站")),
    ("充电与续航", ("充电", "续航", "断点续扫", "电池", "电源开关", "电量")),
    ("地图与导航", ("地图", "建图", "定位", "路线", "分区", "虚拟墙", "漏扫", "导航", "脱困", "卡住", "困境")),
    ("网络与连接", ("Wi-Fi", "WiFi", "App", "APP", "配网", "网络", "蓝牙", "远程")),
    ("清洁能力", ("声音", "吸力", "滚刷", "边刷", "扫", "拖布", "漏灰", "污渍")),
    ("安全与报警", ("报警", "过热", "异味", "漏电", "安全")),
]

# 检查项抽取：只认「检查/确认…是否」这类可判定的动作。
# 必须把逗号也排除在主体之外，否则会把后续处置句一起吞进来
# （如「充电极片是否脏污, 可参照说明书…清理」）。
_CHECK_RE = re.compile(r"(?:请)?(?:检查|确认|核实)(?P<body>[^。；，,]{2,36}?(?:是否|有无)[^。；，,]{2,26})")


def classify_category(text: str) -> str:
    for name, keys in CATEGORY_RULES:
        if any(k in text for k in keys):
            return name
    return "其他"


def verified_components(corpus_text: str) -> list[tuple[str, str, str]]:
    """返回在语料中**实际出现**的组件 (规范名, 类别, 命中词)。"""
    out: list[tuple[str, str, str]] = []
    for canon, label, aliases in COMPONENT_LEXICON:
        hit = ""
        for cand in (canon, *aliases):
            if cand and cand in corpus_text:
                hit = cand
                break
        if hit:
            out.append((canon, label, hit))
    return out


def components_in(text: str, known: Iterable[tuple[str, str, str]]) -> list[tuple[str, str]]:
    """找出文本中提及的组件 (规范名, 类别)，按出现位置排序。"""
    found: list[tuple[int, str, str]] = []
    for canon, label, hit in known:
        pos = text.find(hit)
        if pos >= 0:
            found.append((pos, canon, label))
    found.sort()
    return [(c, l) for _, c, l in found]


def _hits_any(text: str, known: Iterable[tuple[str, str, str]]) -> str:
    for canon, _label, hit in known:
        if hit in text:
            return hit
    return ""


# ---------------------------------------------------------------------------
# 表格适配器：故障排查表（手册 p29–p32）
# ---------------------------------------------------------------------------


def _forward_fill(rows: list[list[str]], cols: int = 2) -> list[list[str]]:
    """纵向合并单元格前向填充，还原「一个故障对多条原因」。"""
    out: list[list[str]] = []
    prev: list[str] = []
    for row in rows:
        cells = list(row) + [""] * max(0, len(prev) - len(row))
        if not prev:
            prev = [""] * len(cells)
        for i in range(min(cols, len(cells))):
            if cells[i].strip():
                prev[i] = cells[i].strip()
            else:
                cells[i] = prev[i] if i < len(prev) else ""
        out.append(cells)
    return out


def extract_fault_table(
    table: dict[str, Any],
    doc_title: str,
    product: str,
    known_components: list[tuple[str, str, str]],
) -> Extraction:
    """故障排查表 → Fault / Cause / Solution / Check + 四条关系链。

    表结构本身已给出「故障 → 原因 → 方法」的映射，无需猜测：
      Fault  CAUSED_BY      Cause
      Cause  RESOLVED_BY    Solution
      Fault  DOCUMENTED_IN  Document
      Check  DETECTS        Fault
    """
    ext = Extraction()
    headers: list[str] = table["headers"]
    if "故障情况" not in headers:
        return ext

    col = {h: i for i, h in enumerate(headers)}
    prose = table.get("prose_cols") or []
    page = table.get("page") or 0
    doc_id = table["doc_id"]
    rows = _forward_fill(table["rows"], cols=2)

    def txt(row: list[str], name: str) -> str:
        i = col.get(name, -1)
        if i < 0 or i >= len(row):
            return ""
        return _flat_cell(row[i], prose[i] if i < len(prose) else None)

    fault_seen: dict[str, str] = {}
    for r_i, row in enumerate(rows):
        fault_raw = txt(row, "故障情况")
        cause_raw = txt(row, "可能原因")
        sol_raw = txt(row, "解决方法")
        if not fault_raw:
            continue
        fault_name = norm_name(fault_raw)
        locator = f"{doc_title} 第{page}页 故障表第{r_i + 1}行"

        if fault_name not in fault_seen:
            category = classify_category(fault_name)
            fault_seen[fault_name] = category
            excluded = _EXCLUDE_RE.findall(fault_name)
            ext.add_node(CandidateNode(
                label="Fault", key=fault_name, name=fault_name,
                props={"category": category, "sourceDoc": doc_id, "evidenceMode": "verbatim",
                       **({"excludedModels": excluded} if excluded else {})},
                evidence=fault_raw, locator=locator, doc_id=doc_id,
                chunk_id=table["table_id"], confidence=0.98, extractor="table/fault",
            ))
            ext.add_node(CandidateNode(
                label="FaultCategory", key=category, name=category,
                evidence=fault_raw, locator=locator, doc_id=doc_id,
                confidence=0.8, extractor="table/fault",
            ))
            ext.add_edge(CandidateEdge(
                type="IN_CATEGORY", head=NodeRef("Fault", fault_name),
                tail=NodeRef("FaultCategory", category),
                evidence=fault_raw, locator=locator, doc_id=doc_id,
                confidence=0.8, extractor="table/fault",
            ))
            # 故障名里点到的部件即故障发生部件
            for comp, _label in components_in(fault_name, known_components):
                ext.add_edge(CandidateEdge(
                    type="OCCURS_IN", head=NodeRef("Fault", fault_name),
                    tail=NodeRef(_label_of(comp, known_components), comp),
                    evidence=fault_raw, locator=locator, doc_id=doc_id,
                    confidence=0.75, extractor="table/fault",
                ))

        if not cause_raw:
            continue
        cause_name = norm_name(cause_raw)
        ext.add_node(CandidateNode(
            label="Cause", key=cause_name, name=cause_name,
            props={"sourceDoc": doc_id, "evidenceMode": "verbatim"},
            evidence=cause_raw, locator=locator, doc_id=doc_id,
            chunk_id=table["table_id"], confidence=0.95, extractor="table/fault",
        ))
        ext.add_edge(CandidateEdge(
            type="CAUSED_BY", head=NodeRef("Fault", fault_name),
            tail=NodeRef("Cause", cause_name),
            props={"evidenceMode": "verbatim"},
            evidence=cause_raw, locator=locator, doc_id=doc_id,
            chunk_id=table["table_id"], confidence=0.95, extractor="table/fault",
        ))

        # 原因里出现的「检查…是否」抽成独立检查项，供 Agent 输出「先查什么」
        for m in _CHECK_RE.finditer(sol_raw):
            check_name = norm_name(m.group("body"))
            if len(check_name) < 4:
                continue
            ext.add_node(CandidateNode(
                label="Check", key=check_name, name=check_name,
                props={"sourceDoc": doc_id, "evidenceMode": "verbatim"},
                evidence=m.group(0), locator=locator, doc_id=doc_id,
                confidence=0.7, extractor="table/check",
            ))
            ext.add_edge(CandidateEdge(
                type="DETECTS", head=NodeRef("Check", check_name),
                tail=NodeRef("Fault", fault_name),
                evidence=m.group(0), locator=locator, doc_id=doc_id,
                confidence=0.7, extractor="table/check",
            ))

        if not sol_raw:
            continue
        # 解决方法可能含多条指令，按句号切成独立方案（各自都可独立执行）
        for seg in _split_actions(sol_raw):
            sol_name = norm_name(seg)
            if len(sol_name) < 3:
                continue
            ext.add_node(CandidateNode(
                label="Solution", key=sol_name, name=sol_name,
                props={"sourceDoc": doc_id, "evidenceMode": "verbatim",
                       "selfServiceable": _is_self_service(sol_name)},
                evidence=seg, locator=locator, doc_id=doc_id,
                chunk_id=table["table_id"], confidence=0.9, extractor="table/fault",
            ))
            ext.add_edge(CandidateEdge(
                type="RESOLVED_BY", head=NodeRef("Cause", cause_name),
                tail=NodeRef("Solution", sol_name),
                props={"evidenceMode": "verbatim"},
                evidence=seg, locator=locator, doc_id=doc_id,
                chunk_id=table["table_id"], confidence=0.9, extractor="table/fault",
            ))
            for comp, label in components_in(sol_name, known_components):
                rel = "REQUIRES_CONSUMABLE" if label in CONSUMABLE_LABELS else (
                    "REQUIRES_PART" if label in PART_LABELS else "")
                if rel:
                    ext.add_edge(CandidateEdge(
                        type=rel, head=NodeRef("Solution", sol_name),
                        tail=NodeRef(label, comp),
                        evidence=seg, locator=locator, doc_id=doc_id,
                        confidence=0.75, extractor="table/fault",
                    ))
    return ext


def _label_of(name: str, known: list[tuple[str, str, str]]) -> str:
    for canon, label, _hit in known:
        if canon == name:
            return label
    return "Component"


_SERVICE_HINT = ("联系", "售后", "维修", "更换", "拆机")

# 「拖布盘无法自动升降。（除T20 机型外）」——这类注解决定了该故障**不适用于**某型号。
# 若原样入库，推理会把 T20 根本没有的功能当成它的故障来诊断。
_EXCLUDE_RE = re.compile(
    r"除\s*([A-Z]{1,2}\d{1,3}[A-Za-z0-9]*(?:\s+(?:PRO|MAX|PLUS|ULTRA|TURBO))?)\s*机型外"
)


def _is_self_service(sol: str) -> bool:
    """能自己动手的方案 vs 需报修。用于回答「我要不要叫售后」。"""
    return not any(h in sol for h in _SERVICE_HINT)


_LEADING_NUM = re.compile(r"^\s*(?:\d+[.、）)]|[①-⑩])\s*")


def _split_actions(text: str) -> list[str]:
    """把一条解决方法拆成可独立执行的步骤。

    只在句号处切分，且要求切出的片段足够长——否则「约 5h。」这类尾巴会被误当方案。
    """
    parts = [p for p in re.split(r"(?<=[。；])", text) if p.strip()]
    out: list[str] = []
    for p in parts:
        p = _LEADING_NUM.sub("", p.strip())
        if len(norm_name(p)) >= 4:
            out.append(p)
    return out or [text]


# ---------------------------------------------------------------------------
# 表格适配器：部件保养频率（手册 p19）
# ---------------------------------------------------------------------------


def extract_maintenance_table(
    table: dict[str, Any], doc_title: str, product: str,
    known_components: list[tuple[str, str, str]],
) -> Extraction:
    """保养表 → 组件节点的维护频率 / 更换频率属性 + 与型号的归属关系。

    这张表是「一格多值」：多个部件共享同一维护频率，
    因此部件单元格需再按顿号拆开，否则会把六个传感器粘成一个不存在的部件名。
    """
    ext = Extraction()
    headers: list[str] = table["headers"]
    if "维护频率" not in headers:
        return ext
    col = {h: i for i, h in enumerate(headers)}
    page = table.get("page") or 0
    doc_id = table["doc_id"]
    label_by_name = {c: l for c, l, _ in known_components}

    for r_i, row in enumerate(table["rows"]):
        if len(row) <= max(col.values()):
            continue
        parts_cell = row[col["部件"]]
        if not parts_cell.strip():
            continue
        maintain = norm_name(row[col["维护频率"]]) if "维护频率" in col else ""
        replace = norm_name(row[col["更换频率"]]) if "更换频率" in col else ""
        locator = f"{doc_title} 第{page}页 保养表第{r_i + 1}行"

        for raw in parts_cell.split("\n"):
            name = norm_name(raw)
            if len(name) < 2:
                continue
            label = label_by_name.get(name, "Component")
            props: dict[str, Any] = {"sourceDoc": doc_id, "evidenceMode": "verbatim"}
            if maintain and maintain != "/":
                props["maintenanceInterval"] = maintain
            if replace and replace != "/":
                props["replaceInterval"] = replace
            ext.add_node(CandidateNode(
                label=label, key=name, name=name, props=props,
                evidence=f"{raw.strip()} / 维护 {maintain} / 更换 {replace}",
                locator=locator, doc_id=doc_id, confidence=0.95,
                extractor="table/maintenance",
            ))
            ext.add_edge(CandidateEdge(
                type="HAS_COMPONENT", head=NodeRef("ProductModel", product),
                tail=NodeRef(label, name),
                evidence=f"{raw.strip()} / 维护 {maintain}", locator=locator,
                doc_id=doc_id, confidence=0.9, extractor="table/maintenance",
            ))
    return ext


# ---------------------------------------------------------------------------
# 表格适配器：部件功能描述（手册 p9）
# ---------------------------------------------------------------------------


def extract_component_function_table(
    table: dict[str, Any], doc_title: str, product: str,
    known_components: list[tuple[str, str, str]],
) -> Extraction:
    """部件功能表 → 组件节点补 `function` 属性，并抽出量程等可核验参数。"""
    ext = Extraction()
    headers: list[str] = table["headers"]
    if "功能描述" not in headers:
        return ext
    col = {h: i for i, h in enumerate(headers)}
    page = table.get("page") or 0
    doc_id = table["doc_id"]
    label_by_name = {c: l for c, l, _ in known_components}
    prose = table.get("prose_cols") or []

    for r_i, row in enumerate(table["rows"]):
        if len(row) <= max(col.values()):
            continue
        raw_name = norm_name(_flat_cell(row[col["部件名称"]], False))
        desc = _flat_cell(row[col["功能描述"]], prose[col["功能描述"]] if len(prose) > col["功能描述"] else True)
        if not raw_name or not desc:
            continue
        # 表里的名字与词表可能差一个「器」（下视传感器 / 下视感应器），先归一再查标签
        name = canonical_component(raw_name)
        label = label_by_name.get(name, "Component")
        props: dict[str, Any] = {
            "function": desc, "sourceDoc": doc_id, "evidenceMode": "verbatim",
        }
        rng = re.search(r"测量范围为\s*([\d.]+\s*m)", desc)
        if rng:
            props["range"] = rng.group(1).replace(" ", "")
        locator = f"{doc_title} 第{page}页 部件功能表第{r_i + 1}行"
        ext.add_node(CandidateNode(
            label=label, key=name, name=name, props=props,
            evidence=desc, locator=locator, doc_id=doc_id,
            confidence=0.95, extractor="table/component",
        ))
        ext.add_edge(CandidateEdge(
            type="HAS_COMPONENT", head=NodeRef("ProductModel", product),
            tail=NodeRef(label, name), evidence=desc, locator=locator,
            doc_id=doc_id, confidence=0.9, extractor="table/component",
        ))
    return ext


# ---------------------------------------------------------------------------
# 表格适配器：产品参数（手册 p33）
# ---------------------------------------------------------------------------


def extract_param_table(table: dict[str, Any], doc_title: str, product: str) -> Extraction:
    """参数表 → ProductModel 属性（型号代码、充电时长、尺寸等）。"""
    ext = Extraction()
    rows = table["rows"]
    page = table.get("page") or 0
    doc_id = table["doc_id"]
    props: dict[str, Any] = {}

    # 参数表没有独立表头：pdfplumber 把「产品型号 / DDX19 / DDX29」整行当成了 header，
    # 于是真正的首行数据反而落在了 headers 里。判据是「首列为参数名、次列含数字」。
    headers = table.get("headers") or []
    if len(headers) >= 2 and any(ch.isdigit() for ch in str(headers[1])):
        props[norm_name(headers[0])] = norm_name(headers[1])

    for row in rows:
        if len(row) < 2:
            continue
        key = norm_name(row[0])
        val = norm_name(row[1])
        if not key or not val:
            continue
        props[key] = val

    if not props:
        return ext
    model_code = props.get("产品型号", "")
    series = series_code(product)
    ext.add_node(CandidateNode(
        label="ProductModel", key=product, name=f"地宝 DEEBOT {product}",
        props={**props, "modelCode": product, "sourceDoc": doc_id,
               "evidenceMode": "verbatim", "deviceCodes": model_code,
               **({"series": series} if series else {})},
        evidence=f"产品型号 {model_code}",
        locator=f"{doc_title} 第{page}页 产品参数表",
        doc_id=doc_id, confidence=0.95, extractor="table/params",
    ))
    return ext
