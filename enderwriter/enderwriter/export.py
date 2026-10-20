"""整理与导出：把末影箱变成一份能用的设定集。

## 为什么导出需要「整理」

直接 dump 43 条记录得到的是一份流水账：按图层平铺，没有层级，
读者不知道哪条属于哪条、哪条已经作废、哪条从没被用过。

真正能用的设定集要有：

    * 层级    —— 残兽机制 > 分级 / 成长 / 击杀（靠 edges 的 part_of）
    * 状态    —— 现行 / 已废止 / 已回收
    * 交叉引用 —— 这条设定和哪些条目相关（靠 edges）

## 整理与合并的底线

**不自动改文字。**

整理只做三件事：
  1. **指出**哪些条目高度重复（给相似度分数，交给人判断）
  2. **指出**哪些条目孤立无关联、哪些从没被用过
  3. 导出时按结构组织，原文一字不动

合并必须由人确认；即使合并，也走版本号（旧版全部保留，可回看）。
理由：一条设定被自动改写后，作者可能几个月后才发现少了半句话。
"""

from __future__ import annotations

import re
import time
from typing import Any, Iterable, Sequence

from .common import LAYER_CN, LAYERS, get_logger, loads

__all__ = [
    "organize",
    "export_markdown",
    "render_organize",
    "DUP_THRESHOLD",
]

log = get_logger("export")

#: 内容相似度超过这个值就算「高度重复」候选
DUP_THRESHOLD = 0.55

#: 导出顺序：叙事上先看人，再看世界
_EXPORT_ORDER: tuple[tuple[str, str], ...] = (
    ("character", "人物"),
    ("world", "世界观"),
    ("timeline", "时间线"),
    ("foreshadowing", "伏笔"),
    ("item", "物品"),
)


def _bigrams(text: str) -> set[str]:
    """汉字二元组集合，用于算相似度。"""
    s = re.sub(r"\s+", "", str(text or ""))
    if len(s) < 2:
        return {s} if s else set()
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _load_edges(chest: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        rows = chest.conn.execute(
            "SELECT src, rel, dst, data FROM edges"
        ).fetchall()
    except Exception:  # noqa: BLE001
        return out
    for r in rows:
        d = loads(r["data"], {}) or {}
        out.append(
            {
                "src": r["src"],
                "rel": r["rel"],
                "dst": r["dst"],
                "weight": int(d.get("weight") or 1),
            }
        )
    return out


def organize(chest: Any) -> dict[str, Any]:
    """分析整库，给出「该整理什么」的清单。**不改任何内容。**

    返回：
      duplicates   高度重复的条目对（带相似度）
      orphans      没有任何关系的条目
      superseded   已废止标记的条目
      unused       从未被检索过的条目
      stats        各图层条目数、边数
    """
    usage = chest.usage_map()
    edges = _load_edges(chest)

    # 邻接：参与过任何边的实体
    linked: set[str] = set()
    for e in edges:
        linked.add(e["src"])
        linked.add(e["dst"])

    by_layer: dict[str, list[Any]] = {}
    superseded: list[dict[str, Any]] = []
    unused: list[dict[str, Any]] = []
    orphans: list[dict[str, Any]] = []

    for layer, _cn in _EXPORT_ORDER:
        try:
            names = sorted(chest.all_entities([layer]))
        except Exception:  # noqa: BLE001
            names = []
        recs = []
        for name in names:
            try:
                rec = chest.get(layer, name)
            except Exception:  # noqa: BLE001
                continue
            if rec is None:
                continue
            recs.append(rec)
            u = usage.get((layer, rec.entity)) or {}
            data = rec.data or {}
            if data.get("superseded") is True or str(data.get("status") or "").lower() in (
                "superseded",
                "deprecated",
                "obsolete",
            ):
                superseded.append({"layer": layer, "entity": rec.entity})
            if not u.get("hit_count"):
                unused.append({"layer": layer, "entity": rec.entity})
            if rec.entity not in linked:
                orphans.append({"layer": layer, "entity": rec.entity})
        by_layer[layer] = recs

    # 重复检测：只在同图层内比，跨图层（人物 vs 世界）不该合并
    duplicates: list[dict[str, Any]] = []
    for layer, recs in by_layer.items():
        bags = [(r, _bigrams(r.content)) for r in recs]
        for i in range(len(bags)):
            for j in range(i + 1, len(bags)):
                sim = _jaccard(bags[i][1], bags[j][1])
                if sim >= DUP_THRESHOLD:
                    duplicates.append(
                        {
                            "layer": layer,
                            "a": bags[i][0].entity,
                            "b": bags[j][0].entity,
                            "similarity": round(sim, 3),
                            "a_chars": len(bags[i][0].content or ""),
                            "b_chars": len(bags[j][0].content or ""),
                        }
                    )
    duplicates.sort(key=lambda d: -d["similarity"])

    return {
        "stats": {
            "layers": {layer: len(recs) for layer, recs in by_layer.items()},
            "total_records": sum(len(v) for v in by_layer.values()),
            "total_edges": len(edges),
            "superseded": len(superseded),
            "unused": len(unused),
            "orphans": len(orphans),
        },
        "duplicates": duplicates[:40],
        "orphans": orphans[:60],
        "superseded": superseded,
        "unused": unused[:60],
    }


def render_organize(rep: dict[str, Any]) -> str:
    """把整理报告渲染成可读文本。"""
    st = rep.get("stats") or {}
    lines = [
        "【整理报告】",
        f"  条目 {st.get('total_records', 0)} 条 · 关系 {st.get('total_edges', 0)} 条",
        f"  已废止 {st.get('superseded', 0)} · 从未使用 {st.get('unused', 0)} · 孤立无关联 {st.get('orphans', 0)}",
    ]

    dups = rep.get("duplicates") or []
    if dups:
        lines.append(f"\n▸ 高度重复候选 {len(dups)} 对（需人工确认，不会自动合并）")
        for d in dups[:10]:
            lines.append(
                f"  {d['similarity']:.0%}  {d['a']}（{d['a_chars']}字） ↔ {d['b']}（{d['b_chars']}字）"
            )
    else:
        lines.append("\n▸ 没有发现高度重复的条目")

    if st.get("total_edges") == 0:
        lines.append("\n⚠ 库里还没有关系数据：先跑一次 enderwriter_build_edges（或 buildEdges），")
        lines.append("  导出才会有层级，否则是平铺流水账。")

    orp = rep.get("orphans") or []
    if orp:
        lines.append(f"\n▸ 孤立条目 {len(orp)} 条（没有与任何条目建立关系）")
        for o in orp[:8]:
            lines.append(f"  [{o['layer']}] {o['entity']}")

    return "\n".join(lines)


def export_markdown(
    chest: Any,
    *,
    title: str = "设定集",
    include_superseded: bool = True,
    include_history: bool = False,
    include_relations: bool = True,
    include_unused: bool = True,
) -> str:
    """导出为结构化 Markdown 设定集。

    组织方式：
      * 顶层按图层分卷（人物 / 世界观 / 时间线 / 伏笔 / 物品）
      * 卷内按 ``part_of`` 关系做两级嵌套：父条目在前，子条目缩进列出
      * 其余条目平铺，并在末尾列出交叉引用

    原文一字不改；已废止的条目会保留但加标记。
    """
    usage = chest.usage_map()
    edges = _load_edges(chest)

    # 父子关系，两个来源：
    #   1. 显式 ``part_of`` 边（正文里写了「X 属于 Y」）
    #   2. **名字前缀包含**：``残兽强度数字分级`` 以 ``残兽`` 开头 → 视为子条目
    #
    # 第 2 条是为了适配实际写法：多数设定稿不会写「X 属于 Y」，
    # 但会自然地用「残兽」「残兽分级」「残兽成长」这样的命名层级。
    # 这比强行平铺更接近作者心里的结构。
    parent_of: dict[str, str] = {}
    children: dict[str, list[str]] = {}
    refs: dict[str, list[tuple[str, str, int]]] = {}
    for e in edges:
        if e["rel"] == "part_of":
            parent_of[e["src"]] = e["dst"]
            children.setdefault(e["dst"], []).append(e["src"])
        refs.setdefault(e["src"], []).append((e["rel"], e["dst"], e["weight"]))
        refs.setdefault(e["dst"], []).append((e["rel"], e["src"], e["weight"]))

    # 名字前缀层级：只在同图层内、且父条目本身存在时生效
    for layer, _cn in _EXPORT_ORDER:
        try:
            names = sorted(chest.all_entities([layer]))
        except Exception:  # noqa: BLE001
            continue
        pool = set(names)
        for name in names:
            if name in parent_of:
                continue
            # 找最短的、是它前缀且不等于它自己的已有条目
            best = ""
            for cand in pool:
                if cand == name or len(cand) < 2:
                    continue
                if name.startswith(cand):
                    if not best or len(cand) < len(best):
                        best = cand
            if best:
                parent_of[name] = best
                children.setdefault(best, []).append(name)

    lines: list[str] = []
    lines.append(f"# {title}")
    lines.append("")

    # ---- 统计头 ----
    total = 0
    for layer, _cn in _EXPORT_ORDER:
        try:
            total += len(chest.all_entities([layer]))
        except Exception:  # noqa: BLE001
            pass
    lines.append(f"> 导出时间：{time.strftime('%Y-%m-%d %H:%M')}")
    lines.append(f"> 条目 {total} 条 · 关系 {len(edges)} 条")
    lines.append("")

    # ---- 目录 ----
    lines.append("## 目录")
    lines.append("")
    for layer, cn in _EXPORT_ORDER:
        try:
            n = len(chest.all_entities([layer]))
        except Exception:  # noqa: BLE001
            n = 0
        if n:
            anchor = cn.lower()
            lines.append(f"- [{cn}](#{anchor})（{n}）")
    lines.append("")

    # ---- 分卷 ----
    for layer, cn in _EXPORT_ORDER:
        try:
            names = sorted(chest.all_entities([layer]))
        except Exception:  # noqa: BLE001
            names = []
        recs = []
        for name in names:
            try:
                rec = chest.get(layer, name)
            except Exception:  # noqa: BLE001
                continue
            if rec is not None:
                recs.append(rec)
        if not recs:
            continue

        lines.append(f"## {cn}")
        lines.append("")

        by_name = {r.entity: r for r in recs}
        rendered: set[str] = set()

        def emit(rec: Any, depth: int) -> None:
            if rec.entity in rendered:
                return
            rendered.add(rec.entity)
            data = rec.data or {}
            dead = data.get("superseded") is True or str(data.get("status") or "").lower() in (
                "superseded",
                "deprecated",
                "obsolete",
            )
            if dead and not include_superseded:
                return

            u = usage.get((layer, rec.entity)) or {}
            marks = []
            if dead:
                marks.append("⚠ 已废止")
            if u.get("archived"):
                marks.append(f"已回收×{u.get('hit_count', 0)}")
            if rec.chapter_id:
                marks.append(rec.chapter_id)
            if rec.version > 1:
                marks.append(f"v{rec.version}")

            head = "#" * (3 + depth)
            suffix = f"　`{' / '.join(marks)}`" if marks else ""
            lines.append(f"{head} {rec.entity}{suffix}")
            lines.append("")
            body = str(rec.content or "").strip()
            if body:
                # 引用块保留原文，避免与结构标题混淆
                for para in body.split("\n"):
                    lines.append(para)
                lines.append("")

            if include_relations:
                my_refs = [r for r in refs.get(rec.entity, []) if r[1] != rec.entity]
                if my_refs:
                    my_refs.sort(key=lambda x: -x[2])
                    items = [
                        f"{'提及' if rel == 'mentions' else _rel_cn(rel)} {dst}×{w}"
                        for rel, dst, w in my_refs[:8]
                    ]
                    lines.append(f"*关联：{' · '.join(items)}*")
                    lines.append("")

            if include_history:
                try:
                    hist = chest.history(layer, rec.entity)
                except Exception:  # noqa: BLE001
                    hist = []
                if len(hist) > 1:
                    lines.append("<details><summary>历史版本</summary>")
                    lines.append("")
                    for hv in hist:
                        lines.append(f"- v{hv.version}（{hv.last_updated}，{len(hv.content)} 字）")
                    lines.append("")
                    lines.append("</details>")
                    lines.append("")

            for child_name in sorted(children.get(rec.entity, [])):
                child = by_name.get(child_name)
                if child is not None:
                    emit(child, depth + 1)

        # 先渲染「没有父条目」的顶层条目
        for rec in sorted(recs, key=lambda r: r.entity):
            if parent_of.get(rec.entity) in by_name:
                continue
            emit(rec, 0)
        # 兜底：成环或父条目不在本卷的，直接平铺
        for rec in sorted(recs, key=lambda r: r.entity):
            if rec.entity not in rendered:
                emit(rec, 0)

    # ---- 附录 ----
    if include_superseded:
        dead = []
        for layer, _cn in _EXPORT_ORDER:
            try:
                names = sorted(chest.all_entities([layer]))
            except Exception:  # noqa: BLE001
                continue
            for name in names:
                rec = chest.get(layer, name)
                if rec is None:
                    continue
                data = rec.data or {}
                if data.get("superseded") is True or str(data.get("status") or "").lower() in (
                    "superseded",
                    "deprecated",
                    "obsolete",
                ):
                    dead.append((layer, rec.entity))
        if dead:
            lines.append("## 附录：已废止条目")
            lines.append("")
            for layer, name in dead:
                lines.append(f"- [{LAYER_CN.get(layer, layer)}] {name}")
            lines.append("")

    if include_unused and usage:
        never = [
            f"[{LAYER_CN.get(la, la)}] {en}"
            for (la, en), u in sorted(usage.items())
            if not u.get("hit_count")
        ]
        if never:
            lines.append("## 附录：尚未被引用的设定")
            lines.append("")
            lines.append("> 这些条目从未被检索进写作上下文。不是错误——长篇里有些设定要留到后面才用。")
            lines.append("")
            for n in never[:80]:
                lines.append(f"- {n}")
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _rel_cn(rel: str) -> str:
    from .relations import REL_CN

    return REL_CN.get(rel, rel)
