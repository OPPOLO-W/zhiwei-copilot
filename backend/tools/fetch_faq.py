"""抓取科沃斯官网 T20 PRO 常见问题，落盘为干净的 Markdown（去图片）。

站点结构：
    https://www.ecovacs.cn/deebot/t20/welcome-p{1..6}.html   列表页，每页 10 条
    https://www.ecovacs.cn/deebot/t20/question-{id}.html     详情页，正文在 .service-content

用法：
    python -m backend.tools.fetch_faq                 # 全量抓取
    python -m backend.tools.fetch_faq --pages 1       # 只抓第 1 页（调试）
    python -m backend.tools.fetch_faq --dry-run       # 只列清单不落盘
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

from bs4 import BeautifulSoup, NavigableString, Tag

BASE = "https://www.ecovacs.cn/deebot/t20"
OUT_DIR = Path("documents/Questions/地宝 DEEBOT T20 PRO")
MANIFEST = Path("data/curated/faq_manifest.json")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

# 需要整块丢弃的标签（图片是最主要的）
DROP_TAGS = ("img", "picture", "figure", "figcaption", "script", "style", "iframe", "video", "source", "noscript")
BLOCK_TAGS = ("p", "div", "section", "article", "blockquote", "tr")


def fetch(url: str, retries: int = 3) -> str:
    last: Exception | None = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read()
            return raw.decode("utf-8", errors="ignore")
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(1.0 * (i + 1))
    raise RuntimeError(f"抓取失败 {url}: {last}")


def collect(pages: range) -> list[dict]:
    """遍历列表页，拿到 (qid, title) 的有序去重清单。"""
    seen: dict[str, dict] = {}
    for p in pages:
        html = fetch(f"{BASE}/welcome-p{p}.html")
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.select('a[href*="question-"]'):
            m = re.search(r"question-(\d+)\.html", a.get("href", ""))
            if not m:
                continue
            qid = m.group(1)
            title = a.get_text(" ", strip=True)
            if qid in seen:
                continue
            seen[qid] = {"id": qid, "title": title, "page": p}
    return list(seen.values())


def _render(node: Tag | NavigableString) -> str:
    """把节点渲染成文本：只在块级元素与 <br> 处换行，行内标签直接拼。"""
    if isinstance(node, NavigableString):
        return str(node)
    out: list[str] = []
    for child in node.children:
        if isinstance(child, NavigableString):
            out.append(str(child))
            continue
        name = child.name
        if name == "br":
            out.append("\n")
        elif name in ("strong", "b"):
            inner = _render(child).strip()
            if inner:
                # 嵌套加粗（strong 套 strong）时不再重复加星号
                out.append(inner if inner.startswith("**") else f"**{inner}**")
        elif name in ("h1", "h2", "h3", "h4", "h5"):
            inner = _render(child).strip()
            if inner:
                level = min(int(name[1]), 4) + 1
                out.append(f"\n\n{'#' * level} {inner}\n\n")
        elif name == "li":
            inner = _render(child).strip()
            if inner:
                out.append(f"\n- {inner}")
        elif name == "td":
            inner = _render(child).strip()
            out.append(f"{inner} | ")
        elif name == "tr":
            out.append("\n" + _render(child).strip())
        elif name in BLOCK_TAGS:
            inner = _render(child).strip()
            if inner:
                out.append(f"\n\n{inner}\n\n")
        else:
            out.append(_render(child))
    return "".join(out)


# 纯媒体占位行：原页面此处是一张图或一段视频的标题，删图后只剩这行字。
# 判定要点是「整行除媒体词外没有别的内容」——允许带编号与加粗，
# 但必须排除动作动词，否则「请观看视频」这种正文会被误删。
_MEDIA_WORD = re.compile(r"(视频|影片|动图|图片|图示|示意图|长图)(指导|演示|教程)?$")
_MEDIA_ORDER = re.compile(r"^(\d{1,2}[.、)）]|[①-⑩]|[-–—*·])\s*")
_MEDIA_VERB = re.compile(r"[请看观播放点击查]")


def _is_media_placeholder(line: str) -> bool:
    """判断一行是不是删图后残留的空媒体标题。

    实际遇到过的三种写法：`3. 视频指导`、`**配网前准备视频指导：**`、`充电指导视频`。
    共同点是去掉编号、加粗、标点后，整行只剩「X + 媒体词」，没有宾语也没有动词。
    """
    s = line.strip().strip("*").strip()
    s = _MEDIA_ORDER.sub("", s).strip("* ").strip()
    # 末尾的括注（「指导视频（仅供参考）」）不承载信息，先去掉再判断
    s = re.sub(r"[（(][^）)]{0,16}[）)]\s*$", "", s)
    s = s.strip("*:：（）() ")
    if not s or len(s) > 16:
        return False
    if not _MEDIA_WORD.search(s):
        return False
    if _MEDIA_VERB.search(s):  # 「请观看视频」是正文，不是占位
        return False
    return bool(
        re.fullmatch(
            r"[\u4e00-\u9fa5A-Za-z0-9 ]{0,12}(视频|影片|动图|图片|图示|示意图|长图)(指导|演示|教程)?",
            s,
        )
    )


def _tidy(text: str) -> str:
    lines = []
    for line in text.split("\n"):
        line = line.replace("\u00a0", " ").replace("\u3000", " ").strip()
        line = re.sub(r"[ \t]{2,}", " ", line)
        # 相邻两段加粗（<strong>注：</strong><strong>正文</strong>）会拼出 ****，
        # 先合并成空再处理三连星，否则会留下 `**注：**正文**` 这种断开的标记。
        line = re.sub(r"\*{4,}", "", line)
        line = re.sub(r"\*{3}", "**", line)  # 嵌套加粗产生的 ***
        line = re.sub(r"^\*\*\s*$", "", line)  # 删图后残留的孤立加粗符
        line = re.sub(r"^(\d+)[.、](?=[^\s])", r"\1. ", line)  # 序号后补空格
        line = re.sub(r"^([①②③④⑤⑥⑦⑧⑨⑩])(?=[^\s、：:])", r"\1 ", line)
        lines.append(line)

    out: list[str] = []
    for line in lines:
        if not line:
            if out and out[-1] == "":
                continue
            out.append("")
            continue
        if _is_media_placeholder(line):
            continue
        if re.fullmatch(r"[\*\|\-\s]+", line):
            continue
        out.append(line)
    while out and out[0] == "":
        out.pop(0)
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out)


def parse_question(qid: str) -> tuple[str, str]:
    html = fetch(f"{BASE}/question-{qid}.html")
    soup = BeautifulSoup(html, "html.parser")
    node = soup.select_one(".service-content") or soup.select_one("article") or soup.body
    if node is None:
        raise RuntimeError(f"未找到正文容器 {qid}")

    title_tag = node.find("h1")
    title = title_tag.get_text(strip=True) if title_tag else ""
    if title_tag:
        title_tag.decompose()

    for name in DROP_TAGS:
        for tag in node.find_all(name):
            tag.decompose()

    body = _tidy(_render(node))
    if not title:
        t = soup.find("title")
        title = (t.get_text(strip=True).split("-")[0] if t else qid).strip()
    return title, body


def safe_name(title: str) -> str:
    name = title.strip().rstrip("？?").strip()
    name = re.sub(r'[\\/:*"<>|]', "-", name)
    name = re.sub(r"\s+", " ", name)
    return name[:80]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", default="1-6", help="页码范围，如 1-6 或 1")
    ap.add_argument("--out", default=str(OUT_DIR))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--sleep", type=float, default=0.4)
    args = ap.parse_args(argv)

    if "-" in args.pages:
        a, b = args.pages.split("-", 1)
        pages = range(int(a), int(b) + 1)
    else:
        pages = range(int(args.pages), int(args.pages) + 1)

    items = collect(pages)
    print(f"[collect] {len(items)} 条问题", file=sys.stderr)
    if args.dry_run:
        for it in items:
            print(f"  p{it['page']}  {it['id']}  {it['title']}")
        return 0

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest: list[dict] = []
    ok = fail = 0

    for it in items:
        try:
            title, body = parse_question(it["id"])
            fname = safe_name(title or it["title"]) + ".md"
            text = f"# {title}\n\n{body}\n"
            (out_dir / fname).write_text(text, encoding="utf-8")
            manifest.append(
                {
                    "id": it["id"],
                    "title": title,
                    "file": fname,
                    "page": it["page"],
                    "url": f"{BASE}/question-{it['id']}.html",
                    "chars": len(body),
                    "lines": body.count("\n") + 1,
                }
            )
            ok += 1
            print(f"[ok] {it['id']} {len(body):5d}字  {fname}", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001
            fail += 1
            print(f"[fail] {it['id']} {exc}", file=sys.stderr)
        time.sleep(args.sleep)

    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(
        json.dumps(
            {"source": f"{BASE}/welcome-p1.html", "count": len(manifest), "items": manifest},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"[done] 成功 {ok} 失败 {fail} → {out_dir}（清单 {MANIFEST}）", file=sys.stderr)
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
