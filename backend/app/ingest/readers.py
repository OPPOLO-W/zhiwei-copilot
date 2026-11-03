"""文档读取器：PDF 与 Markdown 两条通道，统一产出 Doc / Table / Chunk。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pdfplumber

from .corpus import Chunk, Doc, Table, cell_flat, clean_cell, clean_text

# 目录行：「1. 产品组成 .......... 5」/「3.3 浮动滚刷与双边刷 ....... 8」
# 第二个捕获组非空即二级小节——比靠缩进猜层级稳，因为双栏排版的右栏
# 天然整列右移，用 x0 判定会把右栏的一级章节全当成小节。
_TOC = re.compile(r"^(\d{1,2})(?:\.(\d{1,2}))?\.?\s*(.+?)\s*[.·]{3,}\s*(\d{1,3})\s*$")
# 章节标题行：「3. 部件保养」「4. 常见问题排查」
_CHAPTER = re.compile(r"^(\d{1,2})\.\s*([\u4e00-\u9fa5A-Za-z][^\n]{1,20})$")
# 页码行
_PAGENO = re.compile(r"^\d{1,3}$")

# 同一 y 上相隔多远算「另一栏」。正常字间距 < 5pt，双栏间距 30pt 以上，
# 取 15 既不切开行内空格，也不会把两栏粘成一句。
_COLUMN_GAP = 15.0


def _page_lines(page: Any) -> list[tuple[float, str]]:
    """按 y 坐标把字符聚成行，再按横向大间距切成栏段，返回 (首个非空白字符 x0, 文本)。

    两件事都不能省：
    1. **跳过行首空白字符再取坐标**——PDF 里缩进常以空格字形实现，
       若取 `chars[0]` 会一律拿到同一个 x，层级信息就丢了；
    2. **按横向间距切栏**——T20 手册是双栏排版，同一 y 上左右两栏并存，
       不切开会把「1. 产品组成 ... 5」和「3.3 浮动滚刷与双边刷 ... 8」粘成一行，
       目录解析直接报废，后续所有 section 归属跟着错。
    """
    buckets: dict[int, list[dict]] = {}
    for ch in page.chars:
        buckets.setdefault(round(ch["top"] / 2), []).append(ch)

    out: list[tuple[float, str]] = []
    for key in sorted(buckets):
        chs = sorted(buckets[key], key=lambda c: c["x0"])
        segment: list[dict] = [chs[0]]
        for prev, cur in zip(chs, chs[1:]):
            if cur["x0"] - prev["x1"] > _COLUMN_GAP:
                out.append(_seg_text(segment))
                segment = [cur]
            else:
                segment.append(cur)
        out.append(_seg_text(segment))
    return [(x0, text) for x0, text in out if text]


def _seg_text(chars: list[dict]) -> tuple[float, str]:
    text = "".join(c["text"] for c in chars).strip()
    x0 = next((c["x0"] for c in chars if c["text"].strip()), chars[0]["x0"])
    return x0, text


def _parse_toc(pages: list[Any]) -> list[tuple[int, str, int]]:
    """从目录页解析一级章节 (章号, 章名, 起始页)。

    层级靠**编号形态**判定（`3.` 是一级，`3.3` 是二级），不再靠横向缩进：
    双栏排版的右栏整列右移，用 x0 聚类会把右栏的一级章节误判成小节；
    反过来单栏但顶格的二级小节又会被误判成章节。编号本身没有歧义。

    若不区分层级，「3.9 关机存放」会被当成第 9 章，
    导致第 28 页之后的所有内容都挂到错误章节下。
    """
    chapters: dict[int, tuple[int, str, int]] = {}
    for page in pages[:8]:
        for _x0, text in _page_lines(page):
            m = _TOC.match(text)
            if not m:
                continue
            num, sub, name, page_no = m.groups()
            if sub:                     # 「3.3 浮动滚刷」这类二级小节
                continue
            chapters.setdefault(int(num), (int(num), name.strip(), int(page_no)))
    return [chapters[k] for k in sorted(chapters)]


def _section_of(page_no: int, toc: list[tuple[int, str, int]]) -> str:
    name = ""
    for num, title, start in toc:
        if page_no >= start:
            name = f"{num}. {title}"
        else:
            break
    return name or "前言"


def _split_merged_header(row: list[str]) -> tuple[list[str], list[str]] | None:
    """恢复「表头与首条数据被压在同一个单元格」的情形。

    PDF 表格无可见表头分隔线时，pdfplumber 会把表头文字和首行数据放进同一格，
    形如 `['部件\\n可水洗拖布', '维护频率\\n/', '更换频率\\n每1-2个月']`。
    判定条件：每个非空单元格都含换行、各格行数一致、且首段都是短词（≤6 字）。
    """
    filled = [c for c in row if c.strip()]
    if len(filled) < 2 or not all("\n" in c for c in filled):
        return None
    parts = [c.split("\n") for c in row]
    if len({len(p) for p in parts if len(p) > 1}) != 1:
        return None
    heads = [p[0].strip() for p in parts if p]
    if any(not h or len(h) > 6 for h in heads):
        return None
    first = ["\n".join(p[1:]).strip() for p in parts]
    return heads, first


_BULLET = re.compile(r"^[*·•]\s*|^[-–—]\s+")
_SENT_END = ("。", "！", "？", "；", "!", "?", ";", "：", ":")
_NEW_ITEM = re.compile(r"^(\d{1,2}[.、)）]|[①-⑩]|[一二三四五六七八九十]{1,2}[、.．])")
_HAS_PUNCT = re.compile(r"[，。；：、,.;:！？!?]")


def _merge_lines(lines: list[str]) -> list[str]:
    """把 PDF 的排版折行还原成段落。

    每一行都是一个 chunk 会把「1. 针对…清理地面上的电源」和「线和细小物品…」
    拆成两条互不相干的语料，检索时命中后一条根本看不懂。
    合并规则保守——只在「上一行没写完（无句末标点）」且「本行不是新条目」时拼接，
    短且无标点的行当作标题单独保留，避免把小标题粘进正文。
    """
    out: list[str] = []
    for raw in lines:
        t = raw.strip()
        if not t:
            continue
        if _TOC.match(t):
            out.append(t)
            continue
        if not out:
            out.append(t)
            continue
        prev = out[-1]
        prev_is_heading = len(prev) <= 14 and not _HAS_PUNCT.search(prev)
        prev_closed = prev.endswith(_SENT_END)
        if _NEW_ITEM.match(t) or prev_closed or prev_is_heading or len(prev) < 4:
            out.append(t)
        else:
            out[-1] = prev + t          # 中文折行直接拼接，不插空格
    return out


def _order_by_column(lines: list[tuple[float, str]], width: float) -> list[str]:
    """按阅读顺序输出：双栏页先读左栏再读右栏，单栏页按 y 顺序。

    `extract_text()` 对双栏面板是按 y 逐行拼接的，左右栏交错成一句
    （「1. 针对产品…清理地面上的电源1. 仅适用制造商专配的充电座」），
    语义直接毁掉。这里改为按栏聚合，代价是段落被拆得比原来碎一点，
    但至少每句是完整的。
    """
    if not lines:
        return []
    mid = width / 2
    left = [t for x0, t in lines if x0 < mid - 10]
    right = [t for x0, t in lines if x0 >= mid - 10]
    if len(left) >= 5 and len(right) >= 5:
        return left + right
    return [t for _x0, t in lines]


def read_pdf(path: Path, doc_id: str, doc_type: str = "user_manual",
             authority: int = 5) -> tuple[Doc, list[Table], list[Chunk]]:
    """读取 PDF：按页切块，同时抽取表格。

    每个表格的每一行都会生成一个 `kind="table_row"` 的 chunk，
    `text` 为「列名: 值 | 列名: 值」的形式便于检索，
    原始单元格同时保存在 Table.rows 中以保持列对齐语义。
    """
    tables: list[Table] = []
    chunks: list[Chunk] = []

    with pdfplumber.open(str(path)) as pdf:
        toc = _parse_toc(list(pdf.pages))
        pages_lines: list[list[tuple[float, str]]] = []
        pages_width: list[float] = []
        pages_tables: list[list[list[list[str | None]]]] = []
        for page in pdf.pages:
            pages_lines.append(_page_lines(page))
            pages_width.append(float(page.width))
            try:
                pages_tables.append(page.extract_tables() or [])
            except Exception:
                pages_tables.append([])

    doc = Doc(
        doc_id=doc_id,
        title=path.stem,
        doc_type=doc_type,
        source_path=str(path),
        authority=authority,
        page_count=len(pages_lines),
        sections=[f"{n}. {t}" for n, t, _ in toc],
    )

    for idx, lines in enumerate(pages_lines):
        page_no = idx + 1
        section = _section_of(page_no, toc)
        ordered = [
            _BULLET.sub("", t).strip()
            for t in _order_by_column(lines, pages_width[idx]) if t.strip()
        ]
        body = clean_text("\n".join(_merge_lines(ordered)))
        if not body:
            continue

        # 表格行单独成 chunk；正文里属于表格单元格的文字不再重复计入
        page_tables = pages_tables[idx]
        cell_texts: set[str] = set()
        for t_i, raw_table in enumerate(page_tables):
            rows = [[clean_cell(c) for c in row] for row in raw_table if any((c or "").strip() for c in row)]
            if len(rows) < 2:
                continue
            for row in rows:
                for cell in row:
                    if cell:
                        cell_texts.add(re.sub(r"\s+", "", cell))
            table_id = f"{doc_id}#p{page_no}t{t_i + 1}"
            note = ""
            # 表头判定：先试「表头与首行数据被合并」的恢复，再退回常规判定
            head_idx = 0 if any(rows[0]) else 1
            recovered = _split_merged_header(rows[head_idx]) if head_idx < len(rows) else None
            if recovered:
                headers, first_row = recovered
                data_rows = [first_row] + rows[head_idx + 1:]
                note = "表头与首行数据被合并，已按换行拆分恢复"
            elif not any(rows[0]):
                headers = [f"列{i + 1}" for i in range(len(rows[0]))]
                data_rows = rows[1:]
                note = "未识别到表头，按位置编号列"
            else:
                headers = rows[0]
                data_rows = rows[1:]

            prose_cols = Table.detect_prose_cols(data_rows)
            table = Table(table_id=table_id, doc_id=doc_id, page=page_no,
                          headers=headers, rows=data_rows, note=note,
                          prose_cols=prose_cols)
            tables.append(table)

            for r_i, row in enumerate(table.normalized_rows()):
                pairs = [
                    f"{h}: {cell_flat(v, prose=prose_cols[i] if i < len(prose_cols) else None)}"
                    for i, (h, v) in enumerate(zip(headers, row))
                    if v and not h.startswith("列")
                ]
                line = " | ".join(pairs) if pairs else " | ".join(cell_flat(v) for v in row if v)
                if not line.strip():
                    continue
                chunks.append(Chunk(
                    chunk_id=f"{doc_id}#p{page_no}r{r_i + 1}",
                    doc_id=doc_id, doc_title=doc.title, doc_type=doc_type,
                    page=page_no, section=section, kind="table_row",
                    text=line, table_id=table_id, row_index=r_i, headers=headers,
                ))

        for line in body.split("\n"):
            if len(line) < 4 or _PAGENO.fullmatch(line):
                continue
            if _TOC.match(line) or line.startswith("目录"):
                continue
            if re.sub(r"\s+", "", line) in cell_texts:
                continue
            chunks.append(Chunk(
                chunk_id="",  # 稍后统一编号
                doc_id=doc_id, doc_title=doc.title, doc_type=doc_type,
                page=page_no, section=section, kind="paragraph", text=line,
            ))

    # 正文 chunk 统一编号（表格行已用 r 前缀，避免冲突）
    seq = 0
    for c in chunks:
        if not c.chunk_id:
            seq += 1
            c.chunk_id = f"{doc_id}#p{c.page}c{seq}"
    return doc, tables, chunks


def _is_bold_heading(s: str) -> bool:
    """判断「整行加粗」是层级小标题还是被加粗的正文。

    FAQ 正文里两类行长得一样：
        **二：抹布支架一个转、一个不转；如何处理？**      ← 小标题
        **注：①使用前，请确认清水箱内有适量的清水。**      ← 正文强调
    早期只看「首尾都是 **」就判成标题，结果「注：…」这类整段被当成 section
    吞掉，正文少了一句且后续所有步骤都挂到了错误的小节下。

    判据取四条同时成立：恰好一对星号、够短、不以句末标点收尾、不是注释引导词。
    """
    if not (s.startswith("**") and s.endswith("**")) or s.count("**") != 2:
        return False
    inner = s.strip("*").strip(" 　:：")
    if not inner or len(inner) > 26:
        return False
    if inner[-1] in "。！？；，、":
        return False
    return not inner.startswith(("注", "温馨提示", "提示", "说明", "备注", "以下"))


def read_markdown(path: Path, doc_id: str, doc_type: str = "faq",
                  authority: int = 4, url: str = "") -> tuple[Doc, list[Table], list[Chunk]]:
    """读取 FAQ Markdown：按标题分节，按列表项/段落切块。

    FAQ 的价值在于「一个问句 + 一串有序操作」，因此保留列表项边界，
    这样抽出来的 Solution 的 steps 才能保持正确顺序。
    一级标题是问题本身，二级及以下标题（`##`/`###` 或整行加粗）作为小节名。
    """
    raw = path.read_text(encoding="utf-8", errors="replace")
    lines = raw.replace("\r\n", "\n").split("\n")

    title = ""
    chunks: list[Chunk] = []
    section = "正文"
    seq = 0

    def emit(kind: str, text: str, sec: str) -> None:
        nonlocal seq
        seq += 1
        chunks.append(Chunk(
            chunk_id=f"{doc_id}#c{seq}", doc_id=doc_id,
            doc_title=title or path.stem, doc_type=doc_type,
            page=None, section=sec, kind=kind, text=text,
        ))

    for line in lines:
        s = line.strip()
        if not s:
            continue

        # 1) Markdown 标题：一级=问题，二三级=小节
        if s.startswith("#"):
            text = s.lstrip("#").strip()
            level = len(s) - len(s.lstrip("#"))
            if level == 1 and not title:
                title = text
            elif text:
                section = text
                emit("heading", text, section)
            continue

        # 2) 以加粗开头：可能是小标题，也可能是被加粗的正文/注释
        #    注意「**注：**正文…」这种只有开头加粗的行——若放它走到列表判定，
        #    行首的 `*` 会被当成项目符号，正文变成「*注：…」这样的假步骤。
        if s.startswith("**"):
            if _is_bold_heading(s):
                section = s.strip("*").strip(" 　:：")
                emit("heading", section, section)
                continue
            inner = s.replace("**", "").strip(" 　")
            label = inner.strip(" 　:：")
            # 「温馨提示：」「注：」这类标签行本身没有动作，但它**划出了一个注意事项区**：
            # 后面的条目是前置条件与禁令，不是操作步骤。因此升格成小节标记，
            # 下游靠 section 就能把这一区与真正的步骤区分开。
            if label in ("温馨提示", "提示", "注", "注意", "说明", "备注"):
                section = label
                emit("heading", label, section)
                continue
            if len(inner) >= 2:
                emit("paragraph", inner, section)
            continue

        # 3) 普通行
        kind = "list_item" if re.match(r"^([-*•]|[①-⑩]|\d+[.、）)])\s*", s) else "paragraph"
        text = re.sub(r"^([-*•]|[①-⑩]|\d+[.、）)])\s*", "", s) if kind == "list_item" else s
        text = text.replace("**", "").strip()
        if len(text) < 2:
            continue
        emit(kind, text, section)

    doc = Doc(
        doc_id=doc_id, title=title or path.stem, doc_type=doc_type,
        source_path=str(path), authority=authority, url=url,
        page_count=0, sections=sorted({c.section for c in chunks}),
    )
    return doc, [], chunks
