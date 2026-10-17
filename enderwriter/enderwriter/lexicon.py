"""词法工具：中文/英文混合的分词与倒排索引词表生成。

SQLite 的 FTS5 默认分词器不切分中文，而环境不保证有 jieba 等分词库。
因此本模块用**确定性字符 n-gram** 方案：CJK 连续串切 bigram（短串保留整串），
拉丁文按词切分。索引侧与查询侧共用同一函数，保证召回一致。
"""

from __future__ import annotations

import re
import unicodedata

__all__ = ["analyze", "term_weights", "ZH_STOP", "bigrams", "extract_entities"]

_LATIN = re.compile(r"[A-Za-z][A-Za-z0-9_'\-]*")
_CJK_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")
_DIGITS = re.compile(r"\d+")

#: 极短高频虚词：仅当作为**单字**出现时才丢弃，避免伤及“云”“寒”等实义字
ZH_STOP: frozenset[str] = frozenset(
    "的了是在和与也就都而及或被把给对为以于这那个我你他她它又不很还但却从到去来上下里外之其则若使让且并等于是"
)


def _latin_words(text: str) -> list[str]:
    return [w.lower() for w in _LATIN.findall(text) if len(w) >= 2]


def bigrams(run: str) -> list[str]:
    """CJK 连续串 -> 词项列表。长度 1 保留（若非虚词），长度 2~4 同时保留整串与 bigram。"""
    n = len(run)
    if n == 1:
        return [] if run in ZH_STOP else [run]
    out: list[str] = []
    if n <= 4:
        out.append(run)
    out.extend(run[i : i + 2] for i in range(n - 1))
    return out


def analyze(text: str) -> list[str]:
    """把文本切成词项列表（保留重复，供计频用）。"""
    if not text:
        return []
    text = unicodedata.normalize("NFKC", str(text))
    out: list[str] = []
    for run in _CJK_RUN.findall(text):
        out.extend(bigrams(run))
    out.extend(_latin_words(text))
    out.extend(_DIGITS.findall(text))
    return out


def term_weights(text: str, *, entity: str = "", title: str = "") -> dict[str, float]:
    """带字段权重的词频表：entity 3.0 / title 2.0 / 正文 1.0。"""
    weights: dict[str, float] = {}

    def add(src: str, w: float) -> None:
        for term in analyze(src):
            weights[term] = weights.get(term, 0.0) + w

    add(entity, 3.0)
    add(title, 2.0)
    add(text, 1.0)
    return weights


def extract_entities(text: str, vocab: frozenset[str] | set[str]) -> list[str]:
    """从流式生成文本中检出已知词表中的实体名（用于异步预取）。

    匹配策略：长度 >=2 的词表项做子串扫描，长词优先，命中即从待检文本中挖去，
    避免“青云宗宗主”同时命中“青云宗”与“青云”。
    """
    if not text or not vocab:
        return []
    hay = unicodedata.normalize("NFKC", str(text))
    hits: list[str] = []
    for term in sorted(vocab, key=len, reverse=True):
        if len(term) < 2:
            continue
        if term in hay:
            hits.append(term)
            hay = hay.replace(term, "\u3000")
    # 长词优先导致顺序不定，按在原文中的出现位置排序，保证确定性
    hits.sort(key=lambda t: (str(text).find(t), t))
    return hits
