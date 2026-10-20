"""关系抽取：把「谁提到谁」变成可检索的结构。

## 为什么需要

没有关系的记忆库只能做**字面匹配**：

    查「残兽」→ 只返回标题含"残兽"的条目

但设定之间真实的关联远不止于此：

    残兽机制 ← 分级 / 成长 / 击杀规则 / 结界
    白杨    ← 白露（同一人两形态）
    作战等级 ← 各牌级处理上限

这些关系一旦建起来，检索就能做**多跳召回**：问残兽，连带把它的子条目一起带出来。

## 抽取策略：只写强边

自动抽取的通病是产出大量弱边（"文中顺带提了一句"也算关系），
多跳检索会被噪声稀释，反而比不建边更差。

所以分两步：

1. **抽候选**：扫每条正文，看它提到了哪些其他实体，记录出现次数与首次位置
2. **只写强边**：出现 ≥``min_weight`` 次、或出现在开头（标题句）才算强关系

实体名有包含关系时（「残兽」⊂「残兽分级」）先按长度降序匹配并屏蔽已匹配区间，
避免同一个词被算成两次。

## 关系类型

只识别几种高置信模式，其余一律 ``mentions``：

    part_of     "X 属于 Y" / "X 是 Y 的一部分" / "X 之下"
    owned_by    "X 由 Y 保管" / "X 在 Y 手上"
    supersedes  "X 取代 Y" / "X 废止 Y"
    mentions    默认

不追求语义准确——**边的主要价值是连通性，不是标签**。
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Sequence

from .common import LAYERS, get_logger

__all__ = [
    "REL_TYPES",
    "Candidate",
    "extract_candidates",
    "build_edges",
    "edge_stats",
    "render",
    "REL_CN",
]

log = get_logger("relations")

REL_TYPES = ("part_of", "owned_by", "supersedes", "mentions")

REL_CN = {
    "part_of": "从属",
    "owned_by": "持有",
    "supersedes": "取代",
    "mentions": "提及",
}

#: 参与关系抽取的图层。
#: style / assertion 是跨项目的元知识，彼此之间没有叙事关系，建边只会产生噪声。
_REL_LAYERS: tuple[str, ...] = tuple(
    la for la in LAYERS if la not in ("style", "assertion")
)

#: 关系类型推断模式（按优先级匹配正文里两个字之间的连接词）
_REL_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"属于|的一部分|之一|之下|下属|下辖"), "part_of"),
    (re.compile(r"由.{1,10}保管|在.{1,10}手上|由.{1,10}持有|所持"), "owned_by"),
    (re.compile(r"取代|替代|废止|作废|改为"), "supersedes"),
)

#: 提到就建边的加权：开头出现视为"主题级"关系
_HEAD_BONUS_CHARS = 80


class Candidate:
    """一条候选关系（尚未入库）。"""

    __slots__ = ("src", "dst", "rel", "weight", "first_pos", "src_layer", "dst_layer")

    def __init__(
        self,
        src: str,
        dst: str,
        rel: str = "mentions",
        *,
        weight: int = 1,
        first_pos: int = 0,
        src_layer: str = "",
        dst_layer: str = "",
    ) -> None:
        self.src = src
        self.dst = dst
        self.rel = rel
        self.weight = weight
        self.first_pos = first_pos
        self.src_layer = src_layer
        self.dst_layer = dst_layer

    def to_dict(self) -> dict[str, Any]:
        return {
            "src": self.src,
            "dst": self.dst,
            "rel": self.rel,
            "weight": self.weight,
            "first_pos": self.first_pos,
            "src_layer": self.src_layer,
            "dst_layer": self.dst_layer,
        }

    def __repr__(self) -> str:  # pragma: no cover
        return f"Candidate({self.src}-{self.rel}->{self.dst}, w={self.weight})"


def _entities(chest: Any) -> list[tuple[str, str]]:
    """取出可参与关系抽取的 (entity, layer)，按名称长度降序。"""
    out: list[tuple[str, str]] = []
    for layer in _REL_LAYERS:
        try:
            names = chest.all_entities([layer])
        except Exception:  # noqa: BLE001
            continue
        for name in names:
            n = str(name or "").strip()
            if len(n) >= 2:
                out.append((n, layer))
    # 长名优先，保证「残兽分级」先于「残兽」被匹配
    out.sort(key=lambda x: -len(x[0]))
    return out


def _infer_rel(content: str, start: int, end: int) -> str:
    """猜关系类型。

    窗口**同时包含词前与词后**：中文的连接词常常在两者之间或之后，
    只看前缀会漏掉「残兽分级**属于**残兽机制」这种写法。
    """
    window = content[max(0, start - 20):min(len(content), end + 18)]
    for pattern, rel in _REL_PATTERNS:
        if pattern.search(window):
            return rel
    return "mentions"


def extract_candidates(
    chest: Any,
    *,
    max_content_chars: int = 20000,
) -> list[Candidate]:
    """扫描全库，抽「某条记录提到了哪些其他实体」。

    返回按 weight 降序的候选列表。**不写库**——写不写由调用方决定。
    """
    entities = _entities(chest)
    if not entities:
        return []

    # 名称 -> (layer)，用于回填
    layer_of: dict[str, str] = {}
    for name, layer in entities:
        layer_of.setdefault(name, layer)

    names = [n for n, _ in entities]
    # 一张正则一次匹配所有实体名（长的在前，保证优先匹配）
    big = re.compile("|".join(re.escape(n) for n in names))

    candidates: dict[tuple[str, str, str], Candidate] = {}

    for layer in _REL_LAYERS:
        try:
            records = chest.all_entities([layer])
        except Exception:  # noqa: BLE001
            continue
        for entity in records:
            try:
                rec = chest.get(layer, entity)
            except Exception:  # noqa: BLE001
                continue
            if rec is None:
                continue
            content = str(rec.content or "")[:max_content_chars]
            if not content:
                continue

            me = str(rec.entity or "")
            hits: dict[str, tuple[int, int]] = {}  # dst -> (count, first_pos)

            for m in big.finditer(content):
                other = m.group(0)
                if other == me:
                    continue
                # 避免把「白杨」里的字算进「白杨树」之类的重叠
                if _overlaps_self(me, other, m.start(), content):
                    continue
                cnt, first = hits.get(other, (0, m.start()))
                hits[other] = (cnt + 1, min(first, m.start()))

            for other, (count, first) in hits.items():
                rel = _infer_rel(content, first, first + len(other))
                key = (me, rel, other)
                cand = candidates.get(key)
                if cand is None:
                    candidates[key] = Candidate(
                        me,
                        other,
                        rel,
                        weight=count,
                        first_pos=first,
                        src_layer=layer,
                        dst_layer=layer_of.get(other, ""),
                    )
                else:
                    cand.weight += count
                    cand.first_pos = min(cand.first_pos, first)

    out = list(candidates.values())
    out.sort(key=lambda c: (-c.weight, c.src, c.dst))
    return out


def _overlaps_self(me: str, other: str, pos: int, content: str) -> bool:
    """``other`` 是否其实是被更长实体名遮住的一部分。

    例：正文里是「残兽分级」，同时库里也有「残兽」。
    正则按长度降序，会先匹配到「残兽分级」，所以这里主要防御
    「me 是 other 的子串」这类自指。
    """
    if not me or not other:
        return False
    if me == other:
        return True
    # me 包含 other 且 other 出现在 me 里 → 是自指的一部分
    if other in me:
        return True
    return False


def build_edges(
    chest: Any,
    *,
    min_weight: int = 2,
    max_per_src: int = 12,
    include_head: bool = True,
) -> dict[str, Any]:
    """抽取并写入强边。

    :param min_weight: 至少被提到几次才算强边（默认 2）
    :param max_per_src: 每条记录最多保留多少条出边，防止枢纽爆炸
    :param include_head: 是否额外保留「开头提到」的边（视为主题级关系）

    返回统计信息（新增、跳过、总数）。
    """
    candidates = extract_candidates(chest)

    kept: list[Candidate] = []
    dropped_weak = 0
    for c in candidates:
        strong = c.weight >= min_weight
        # 开头提及视为主题级关系：即使只提一次也保留
        headed = include_head and c.first_pos <= _HEAD_BONUS_CHARS
        if strong or headed:
            kept.append(c)
        else:
            dropped_weak += 1

    # 每个 src 只保留权重最高的 N 条，避免"什么都有关系"的枢纽节点
    by_src: dict[str, list[Candidate]] = {}
    for c in kept:
        by_src.setdefault(c.src, []).append(c)
    trimmed: list[Candidate] = []
    dropped_hub = 0
    for src, items in by_src.items():
        items.sort(key=lambda c: (-c.weight, c.dst))
        trimmed.extend(items[:max_per_src])
        dropped_hub += max(0, len(items) - max_per_src)

    written = 0
    for c in trimmed:
        try:
            chest.add_edge(
                c.src,
                c.rel,
                c.dst,
                data={"weight": c.weight, "auto": True, "first_pos": c.first_pos},
                commit=False,
            )
            written += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("写入边失败 %s->%s：%s", c.src, c.dst, exc)
    try:
        chest.conn.commit()
    except Exception:  # noqa: BLE001
        pass

    return {
        "candidates": len(candidates),
        "written": written,
        "dropped_weak": dropped_weak,
        "dropped_hub": dropped_hub,
        "min_weight": min_weight,
        "total_edges": edge_stats(chest)["total"],
    }


def edge_stats(chest: Any) -> dict[str, Any]:
    """库里现有边的概况。"""
    total = 0
    by_rel: dict[str, int] = {}
    try:
        rows = chest.conn.execute("SELECT rel, COUNT(*) AS n FROM edges GROUP BY rel").fetchall()
        for r in rows:
            by_rel[str(r["rel"])] = int(r["n"])
            total += int(r["n"])
    except Exception:  # noqa: BLE001
        pass
    return {"total": total, "by_rel": by_rel}


def render(stats: dict[str, Any], *, sample: Sequence[Candidate] = ()) -> str:
    """把抽取结果渲染成可读文本。"""
    lines = [
        f"【关系抽取】写入 {stats.get('written', 0)} 条边"
        f"（候选 {stats.get('candidates', 0)} 条）",
        f"  强度阈值：被提到 ≥{stats.get('min_weight', 2)} 次，或出现在开头",
        f"  丢弃：弱关系 {stats.get('dropped_weak', 0)} 条"
        + (f"，枢纽裁剪 {stats.get('dropped_hub', 0)} 条" if stats.get("dropped_hub") else ""),
        f"  库内现有边：{stats.get('total_edges', 0)} 条",
    ]
    if sample:
        lines.append("\n示例：")
        for c in sample[:8]:
            lines.append(
                f"  {c.src} --[{REL_CN.get(c.rel, c.rel)}]--> {c.dst}  (提到 {c.weight} 次)"
            )
    return "\n".join(lines)
