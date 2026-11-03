"""语料摄取层：把原始文档（PDF/ Markdown）转成带定位锚点的规范化语料。

设计原则
--------
1. **锚点必须可回溯**：每个 chunk 都带 `docId + page/section + 序号`，
   下游抽取出的每条三元组都要能指回「第几页、哪个表格行」，
   这是 evidence 从 paraphrase 升级为 verbatim 的前提。
2. **表格不拍平**：故障排查表是「故障 → 可能原因 → 解决方法」的三元组富矿，
   若按正文拍平会丢失行内对齐关系。因此表格单独保留 `headers + rows`，
   并做**纵向合并单元格的向下填充**（forward fill），还原「一个故障对应多条原因」。
3. **不改写原文**：chunk.text 一律为原文摘录（仅去首尾空白与多余换行），
   不做摘要、不做同义替换——改写会让 evidence 失去证明力。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Iterable


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class Table:
    """文档中的一张表。`rows` 为原始单元格文本，`normalized_rows` 做合并填充。"""

    table_id: str
    doc_id: str
    page: int | None
    headers: list[str]
    rows: list[list[str]]
    caption: str = ""
    # 解析备注：如 "表头与首行数据被合并，已按换行拆分恢复"
    note: str = ""
    # 每列是否为「连续叙述」列（True）还是「名词列表」列（False）。
    # 决定单元格内换行的解释方式：叙述列做软换行拼接，列表列用顿号分隔。
    prose_cols: list[bool] = field(default_factory=list)

    @staticmethod
    def detect_prose_cols(rows: list[list[str]]) -> list[bool]:
        """按列统计判断该列是叙述文还是名词列表。

        单个单元格无法判别——「集尘」与「清洁槽」都很短，一个是被折断的残句、
        一个是独立部件名。但整列一放就清楚了：
          * 故障情况/可能原因/解决方法 → 单元格多以「。」收尾且较长；
          * 部件 → 单元格多是短名词，极少有句末标点。
        """
        if not rows:
            return []
        width = max(len(r) for r in rows)
        flags: list[bool] = []
        for col in range(width):
            cells = [r[col] for r in rows if col < len(r) and r[col].strip()]
            if not cells:
                flags.append(False)
                continue
            flat = [cell for cell in (c.replace("\n", "") for c in cells)]
            prose = sum(1 for c in flat if len(c) > 20 or c.endswith(("。", "！", "？", "；")))
            flags.append(prose / len(flat) >= 0.5)
        return flags

    def normalized_rows(self, fill_cols: int = 2) -> list[list[str]]:
        """纵向合并单元格向下填充。

        PDF 表格里 `序号` / `故障情况` 这类列只在首行出现，续行留空。
        直接解析会得到「无主的原因」，因此把前 `fill_cols` 列做前向填充。
        """
        out: list[list[str]] = []
        prev = [""] * len(self.headers)
        for row in self.rows:
            cells = list(row) + [""] * (len(self.headers) - len(row))
            for i in range(min(fill_cols, len(cells))):
                if cells[i].strip():
                    prev[i] = cells[i].strip()
                else:
                    cells[i] = prev[i]
            out.append(cells)
        return out

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Chunk:
    """最小可检索语料单元：一段正文、一个列表项、或一行表格。"""

    chunk_id: str
    doc_id: str
    doc_title: str
    doc_type: str            # user_manual | faq
    page: int | None
    section: str
    kind: str                # paragraph | list_item | table_row | heading
    text: str
    table_id: str = ""
    row_index: int = -1
    headers: list[str] = field(default_factory=list)

    @property
    def anchor(self) -> str:
        """人能读的定位串，直接写进 evidence 的 locator 字段。"""
        if self.table_id and self.row_index >= 0:
            return f"{self.doc_title} 第{self.page}页 表{self.row_index + 1}行"
        if self.page:
            return f"{self.doc_title} 第{self.page}页"
        return f"{self.doc_title} · {self.section}"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["anchor"] = self.anchor
        return d


@dataclass
class Doc:
    doc_id: str
    title: str
    doc_type: str
    source_path: str
    version: str = ""
    updated_at: str = ""
    authority: int = 3
    page_count: int = 0
    sections: list[str] = field(default_factory=list)
    # 该文档归属的产品型号（如 "T20 PRO"）：文档内的事实默认绑定到这个型号
    product: str = ""
    # 原始出处（官网链接）：FAQ 抓取后靠它把证据链一路指回原文
    url: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# 文本清洗（只做空白规整，不改写内容）
# ---------------------------------------------------------------------------

_WS = re.compile(r"[ \t\u3000]+")
_MULTI_NL = re.compile(r"\n{2,}")


def clean_text(text: str) -> str:
    """去页眉页码残留、压缩空白。不改动任何实词。"""
    if not text:
        return ""
    t = text.replace("\r\n", "\n").replace("\r", "\n")
    t = _MULTI_NL.sub("\n", t)
    lines = []
    for line in t.split("\n"):
        line = _WS.sub(" ", line).strip()
        if not line:
            continue
        # 纯页码行（"29" / "第 29 页"）无信息量
        if re.fullmatch(r"第?\s*\d{1,3}\s*页?", line):
            continue
        lines.append(line)
    return "\n".join(lines)


def clean_cell(text: str | None) -> str:
    """清洗单元格：压缩空格但**保留换行**。

    换行在 PDF 表格里是重要的结构信号：
      * 「表头 + 首条数据」被压在同一个单元格时，靠换行拆分；
      * 「多个部件共享同一频率」时，靠换行枚举部件。
    把换行压平会同时丢掉这两种语义。
    """
    if not text:
        return ""
    out = []
    for line in text.replace("\r", "").split("\n"):
        line = _WS.sub(" ", line).strip()
        if line:
            out.append(line)
    return "\n".join(out)


def cell_flat(text: str, sep: str | None = None, prose: bool | None = None) -> str:
    """把单元格的多行压成一行，正确区分「软换行」与「并列项」。

    PDF 单元格里的换行有两种完全不同的含义，混为一谈会毁掉语义：
      * **软换行**：一句话被排版折断，如
        `确保充电极片充分对接。检` + `查主机和全能基站的充电极片是否脏污`
        → 必须直接拼接；若用「、」会造出「无法返、回全能基站」这种断词。
      * **并列项**：多个部件共享同一属性，如
        `TrueDetect 3D 结构光传感器` / `万向轮` / `下视感应器`
        → 必须用「、」分隔，否则会粘成一个不存在的部件名。

    `prose` 由列级统计给出（见 `Table.detect_prose_cols`）：叙述列做直接拼接，
    列表列用顿号。缺省时退回行长分布启发式，仅供无表上下文的场景使用。
    """
    lines = [p.strip() for p in text.split("\n") if p.strip()]
    if not lines:
        return ""
    if sep is not None:
        return sep.join(lines)
    if len(lines) == 1:
        return lines[0]

    if prose is True:
        return "".join(lines)
    if prose is False:
        # 注意：列表列内部仍有软换行——p9 的「d-ToF / 激光雷达模块」是一个部件被折行，
        # 而 p19 的「激光雷达模块 / 清洁槽」是两个部件。列级判别只能排除叙述列，
        # 列内仍需按行长分布再分一次。
        pass

    lens = sorted(len(p) for p in lines)
    median = max(lens[len(lens) // 2], 1)
    if lens[-1] >= 2.5 * median:
        return "、".join(lines)
    return _join_wrapped(lines)


def _join_wrapped(lines: list[str]) -> str:
    """拼接被排版折断的长名称，中英文交界处补一个空格。

    「d-ToF」+「激光雷达模块」直接连会得到 `d-ToF激光雷达模块`，
    与原文「d-ToF 激光雷达模块」不一致；补空格才保持与文档同形。
    """
    out = ""
    for part in lines:
        if not out:
            out = part
            continue
        prev, nxt = out[-1], part[0]
        need_space = prev.isascii() and prev.isalnum() and not nxt.isascii()
        out += (" " if need_space else "") + part
    return out


# ---------------------------------------------------------------------------
# 落盘
# ---------------------------------------------------------------------------


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            n += 1
    return n


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]
