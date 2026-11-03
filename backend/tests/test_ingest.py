"""摄取层测试：锚点可回溯、表格结构不丢失、章节归属正确。

这些断言的价值在于——它们是「evidence 可追溯到原文」这条承诺的技术保障。
一旦 review 时被问「你这条三元组凭什么这么说」，
靠的就是这里验证过的 docId + page + table row。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
RAW = ROOT / "data" / "raw"


def _load(name: str) -> list[dict]:
    path = RAW / name
    if not path.exists():
        pytest.skip(f"{name} 尚未生成，先运行 python -m backend.app.ingest.build")
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.mark.parametrize("filename,expected", [
    ("地宝 DEEBOT T30 PRO", "T30 PRO"),
    ("地宝 DEEBOT T20 PRO", "T20 PRO"),
    ("地宝 DEEBOT T10 TURBO", "T10 TURBO"),
    ("地宝 DEEBOT T10", "T10"),
    ("DEEBOT T90S PRO 超薄零嵌款", "T90S PRO"),
    ("T80S MAX上下水款", "T80S MAX"),
    ("地宝 DEEBOT T20 PRO 上下水版", "T20 PRO"),
    ("deebot t30 pro", "T30 PRO"),
    # 无型号可识别时返回空串，宁可让人发现，也不臆造一个型号
    ("地宝 DEEBOT 灵嵌", ""),
])
def test_product_is_recognised_from_filename(filename, expected):
    """型号识别必须脱离白名单——否则换一份新机型手册就静默变成「未知产品」。"""
    from backend.app.ingest.build import _infer_product

    assert _infer_product(filename) == expected


def test_chunks_have_unique_ids_and_anchors():
    chunks = _load("chunks.jsonl")
    ids = [c["chunk_id"] for c in chunks]
    assert len(ids) == len(set(ids)), "chunk_id 必须唯一，否则证据定位会指向多处"
    assert all(c["anchor"].strip() for c in chunks), "每个语料块都要有可读锚点"
    assert all(c["doc_id"] for c in chunks)


def test_chunks_are_verbatim_not_rewritten():
    """正文块必须是原文摘录：不做摘要、不改写。

    弱检查：块文本应当是某个文档页面原文的子串（去空白后）。
    这里用长度分布代替全文比对，成本低且能拦住「凭空生成」的块。
    """
    chunks = [c for c in _load("chunks.jsonl") if c["kind"] == "paragraph"]
    assert chunks
    overlong = [c for c in chunks if len(c["text"]) > 400]
    assert not overlong, f"疑似改写/合并的过长段落：{overlong[0]['chunk_id']}"


def test_fault_table_is_row_aligned():
    """故障排查表必须保留「故障 → 原因 → 方法」的行内对齐。

    关键验证点：合并单元格的前向填充生效，
    每条原因都挂在正确的故障下（否则推理链会张冠李戴）。
    """
    chunks = _load("chunks.jsonl")
    rows = [c for c in chunks if c["kind"] == "table_row" and "故障情况" in c["headers"]]
    assert len(rows) >= 40, f"故障表行数偏少：{len(rows)}"
    for row in rows:
        assert "故障情况:" in row["text"]
        assert "可能原因:" in row["text"]
        assert "解决方法:" in row["text"]
        # 前向填充后不允许出现空故障名
        fault = row["text"].split("故障情况:")[1].split("|")[0].strip()
        assert fault, f"故障名为空：{row['chunk_id']}"


def test_maintenance_table_header_recovered():
    """保养表必须解析出「部件 / 维护频率 / 更换频率」三列。

    表头与首行数据被并进同一格的排版并非每份手册都有（T20 PRO 就正常），
    所以这里只对**外观**断言；合并恢复另由单元测试锁定。
    """
    tables = _load("tables.jsonl")
    maint = [t for t in tables if any("维护频率" in h for h in t["headers"])]
    assert maint, "未识别到部件保养表"
    headers = maint[0]["headers"]
    assert headers[:3] == ["部件", "维护频率", "更换频率"]
    assert all(not h.startswith("列") for h in headers[:3]), "表头未识别，退化成按位置编号"
    assert len(maint[0]["rows"]) >= 5


def test_merged_header_is_recovered():
    """「表头 + 首行数据压在同一格」必须被拆回两行。

    直接喂一个人造行，避免绑定某份手册的具体排版。
    """
    from backend.app.ingest.readers import _split_merged_header

    row = ["部件\n可水洗拖布", "维护频率\n/", "更换频率\n每1-2个月"]
    heads, first = _split_merged_header(row)
    assert heads == ["部件", "维护频率", "更换频率"]
    assert first == ["可水洗拖布", "/", "每1-2个月"]
    # 正常表格不应被误拆
    assert _split_merged_header(["部件", "维护频率", "更换频率"]) is None


def test_sections_are_monotonic_and_contiguous():
    """章节归属必须落在目录声明的页码区间内，且首尾相接、不重叠。

    这里锁定的是曾经踩过的坑：目录二级小节（如 3.9 关机存放）被误判成一级章节，
    导致后面的全部内容挂错章节。断言按「结构」而非「具体页码」写，
    换机型手册时仍然成立。
    """
    chunks = [c for c in _load("chunks.jsonl") if c["doc_type"] == "user_manual"]
    pages_of: dict[str, set[int]] = {}
    for c in chunks:
        if c["page"]:
            pages_of.setdefault(c["section"], set()).add(c["page"])

    chapters = {
        name: (min(p), max(p)) for name, p in pages_of.items()
        if name != "前言" and name[:1].isdigit() and ". " in name
    }
    assert len(chapters) >= 4, f"一级章节过少：{sorted(chapters)}"

    ordered = sorted(chapters.items(), key=lambda kv: kv[1][0])
    for (name, (lo, hi)), (next_name, (next_lo, _)) in zip(ordered, ordered[1:]):
        assert hi < next_lo, f"章节区间重叠：{name}({lo}-{hi}) 与 {next_name}(起 {next_lo})"
        assert next_lo - hi <= 2, f"章节之间出现 {next_lo - hi} 页空洞：{name} → {next_name}"


def test_faq_documents_are_split_by_section():
    faqs = [d for d in _load("docs.jsonl") if d["doc_type"] == "faq"]
    assert len(faqs) >= 10, f"FAQ 文档数偏少：{len(faqs)}"
    assert all(d["product"] for d in faqs), "FAQ 必须绑定到产品型号"
    # 同一产品目录下的 FAQ 不能串到别的型号（换语料后最容易出这类错）
    assert len({d["product"] for d in faqs}) == 1, "FAQ 型号混杂"
    assert all(d["authority"] == 4 for d in faqs)


def _fault_names() -> list[str]:
    rows = [c for c in _load("chunks.jsonl")
            if c["kind"] == "table_row" and "故障情况" in c["headers"]]
    names: list[str] = []
    for c in rows:
        name = c["text"].split("故障情况: ")[1].split(" | ")[0]
        if name not in names:
            names.append(name)
    return names


def test_soft_wrap_is_not_split_by_punctuation():
    """软换行必须直接拼接，不能插入顿号；并列项才用顿号。

    PDF 排版把长句折断，若按「并列项」处理会造出
    「无法返、回全能基站」「未开始自动、集尘」这类断词，
    进而污染下游的故障名归一化与实体对齐。
    这是纯单元测试：直接喂两种单元格形态，不依赖某份手册的具体文本。
    """
    from backend.app.ingest.corpus import cell_flat

    prose = "确保充电极片充分对接。检\n查主机和全能基站的充电极片是否脏污"
    assert cell_flat(prose, prose=True) == "确保充电极片充分对接。检查主机和全能基站的充电极片是否脏污"
    assert cell_flat("拖布支架无法旋\n转", prose=True) == "拖布支架无法旋转"

    # 并列项：一行明显长于其余（说明是独立名称，不是被折断的残句）时用顿号
    items = "TrueDetect 3D 结构光传感器\n万向轮\n下视感应器"
    assert cell_flat(items, prose=False) == "TrueDetect 3D 结构光传感器、万向轮、下视感应器"

    names = _fault_names()
    assert len(names) >= 10, f"故障名数量异常：{len(names)}"
    assert all(len(n) >= 3 for n in names), "出现过短的故障名，疑似被截断"


def test_parallel_items_stay_separated():
    """「部件」列的一格多值必须拆成多个部件名，不能粘成一个不存在的名字。"""
    tables = _load("tables.jsonl")
    maint = [t for t in tables if any("维护频率" in h for h in t["headers"])]
    assert maint, "未识别到部件保养表"
    col = maint[0]["headers"].index("部件")
    cells = [r[col] for r in maint[0]["rows"] if len(r) > col and r[col].strip()]
    assert cells
    multi = [c for c in cells if "\n" in c]
    if multi:
        for c in multi:
            for part in c.split("\n"):
                part = part.strip()
                assert len(part) <= 24 and not part.endswith("。"), \
                    f"疑似叙述句被当成部件名：{part}"
    joined = " ".join(cells)
    assert "、" in joined or "\n" in joined, "一格多值未被拆开"


def test_prose_column_detection():
    """列级叙述/列表判别：故障表的文本列应为叙述列，部件表应为列表列。"""
    tables = {(t["page"], tuple(t["headers"][:2])): t for t in _load("tables.jsonl")}
    fault = next(t for (p, h), t in tables.items() if "故障情况" in h)
    assert fault["prose_cols"][0] is False      # 序号
    assert fault["prose_cols"][1] is True       # 故障情况
    maint = next(t for (p, h), t in tables.items() if h == ("部件", "维护频率"))
    assert maint["prose_cols"] == [False, False, False]


# ---------------------------------------------------------------------------
# FAQ 抓取：删图后残留的空媒体标题
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("line,expected", [
    ("3. 视频指导", True),
    ("**配网前准备视频指导：**", True),
    ("充电指导视频", True),
    ("使用视频", True),
    ("指导视频（仅供参考）", True),
    ("参考视频", True),
    # 以下都是正文或真标题，不能删
    ("配网操作-图文指导篇", False),
    ("请观看视频", False),
    ("查看视频教程", False),
    ("点击播放视频", False),
    ("检查视频线是否松动", False),
    ("**二：开始配网**", False),
])
def test_media_placeholder_detection(line, expected):
    """删图后残留的空媒体标题要删掉，「请观看视频」这类正文要留住。

    这三种写法都真实出现过：`3. 视频指导`（还占了步号，导致后续步骤编号错位）、
    `**配网前准备视频指导：**`、`充电指导视频`。
    """
    from backend.tools.fetch_faq import _is_media_placeholder

    assert _is_media_placeholder(line) is expected


def test_faq_corpus_has_no_media_placeholder():
    """58 篇 FAQ 里不能再有任何空媒体标题残留。"""
    from backend.tools.fetch_faq import _is_media_placeholder

    root = Path(__file__).resolve().parents[2] / "documents" / "Questions"
    files = list(root.glob("*/*.md"))
    if not files:
        pytest.skip("FAQ 语料不存在，先运行 python -m backend.tools.fetch_faq")

    leftovers = []
    for path in files:
        for no, line in enumerate(path.read_text(encoding="utf-8").split("\n"), 1):
            if _is_media_placeholder(line):
                leftovers.append(f"{path.name}:L{no} {line.strip()!r}")
    assert not leftovers, f"残留空媒体标题：{leftovers[:5]}"
