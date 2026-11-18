"""轻量文本检索：中文按字符 bigram、英文数字按词切，零依赖 BM25。

从 `backend/app/eval/rag.py` 抽出来，供「RAG 基线」与「Agent 的文档检索工具」共用：
两边用的是同一个检索器，差别在**用法**——基线是一次性 top-k 拼接，Agent 是自己决定
什么时候查、查几个、看完再决定下一步。
"""

from __future__ import annotations

import math
import re
from collections import Counter

_ASCII_WORD = re.compile(r"[a-z0-9]+")
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")


def tokenize(text: str) -> list[str]:
    """中文按字符 bigram、英文数字按词切。

    不引第三方分词器：课程环境装 jieba 会把依赖变重，而检索质量在「短句 + 领域词」
    上对分词器不敏感（实测 E11 / 左驱动轮 / 配网 都能稳定命中）。
    """
    body = str(text or "").lower()
    tokens = _ASCII_WORD.findall(body)
    for run in _CJK_RUN.findall(body):
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[i:i + 2] for i in range(len(run) - 1))
    return tokens


class BM25:
    """Okapi BM25，参数取常用默认值（k1=1.5, b=0.75）。"""

    def __init__(self, docs: list[str], k1: float = 1.5, b: float = 0.75) -> None:
        self.k1, self.b = k1, b
        self.tfs: list[Counter[str]] = []
        self.lengths: list[int] = []
        df: Counter[str] = Counter()
        for doc in docs:
            tf = Counter(tokenize(doc))
            self.tfs.append(tf)
            self.lengths.append(sum(tf.values()))
            df.update(tf.keys())
        self.n = len(docs)
        self.avgdl = sum(self.lengths) / self.n if self.n else 0.0
        self.idf = {t: math.log(1 + (self.n - d + 0.5) / (d + 0.5)) for t, d in df.items()}

    def scores(self, query: str) -> list[float]:
        q = Counter(tokenize(query))
        out: list[float] = []
        for tf, dl in zip(self.tfs, self.lengths):
            s = 0.0
            for term, qn in q.items():
                f = tf.get(term)
                if not f:
                    continue
                denom = f + self.k1 * (1 - self.b + self.b * dl / (self.avgdl or 1))
                s += self.idf.get(term, 0.0) * f * (self.k1 + 1) / denom * qn
            out.append(s)
        return out

    def search(self, query: str, k: int = 6) -> list[tuple[int, float]]:
        scored = list(enumerate(self.scores(query)))
        scored.sort(key=lambda p: (-p[1], p[0]))
        return [(i, s) for i, s in scored[:k] if s > 0]
