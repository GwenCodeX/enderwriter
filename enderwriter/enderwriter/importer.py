"""从既有文档导入设定，反向建库。

## 为什么需要

冷启动最大的摩擦是：作者手里已经有一份几万字的设定稿，
但要一条条 ``remember`` 才能进库。

这个模块把「一份设定文档」拆成「一批条目」，按标题猜图层写进末影箱。

## 设计原则

**默认只预演，不写库。**

导入是有损的：分节方式、图层判断都可能出错，而写进库的东西
会参与检索、影响写作。所以 ``dry_run`` 默认为真，
必须显式传 ``dry_run=False`` 才真正写入。

预演会给出完整清单：每个分节叫什么、判成哪一层、多长、前几十字是什么。
人看过没问题再执行。

## 分节规则

识别三类标题（按优先级）：

    一、地理与气候          中文序号
    ## 标题                 Markdown 标题
    ===== / #####          分隔线后的下一行视为标题

没有标题的文档会按空行分段，每段取首行做标题。
"""

from __future__ import annotations

import io
import os
import re
from typing import Any, Sequence

from .common import LAYER_CN, clip_tokens, get_logger

__all__ = ["split_sections", "guess_layer", "plan_import", "run_import", "LAYER_HINTS"]

log = get_logger("importer")

#: 标题关键词 → 图层。按顺序匹配，先命中先得。
LAYER_HINTS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("人物", "角色", "登场", "主角", "配角", "人物速查"), "character"),
    (("伏笔", "铺垫", "悬念", "埋线", "回收"), "foreshadowing"),
    (("时间线", "年表", "大事记", "编年", "时间轴"), "timeline"),
    (("物品", "道具", "装备", "器具", "名录"), "item"),
    (
        (
            "地理", "气候", "历史", "机构", "体系", "势力", "组织",
            "规则", "设定", "世界", "残兽", "战力", "魔力", "魔法",
            "城市", "社会", "经济", "文化", "待定", "口径",
        ),
        "world",
    ),
)

#: 这些标题默认**不导入**——它们是写作产物，不是设定
_SKIP_HINTS: tuple[str, ...] = (
    "逐章细纲", "细纲", "章节列表", "验收自查", "自查", "目录",
)

_CN_NUM = "一二三四五六七八九十百零"
_RE_CN_HEADING = re.compile(rf"^([{_CN_NUM}]{{1,4}})、\s*(.+?)\s*$")
_RE_MD_HEADING = re.compile(r"^(#{1,6})\s*(.+?)\s*$")
_RE_RULE = re.compile(r"^[=\-#~*]{5,}$")


def guess_layer(title: str, body: str = "") -> str:
    """按标题（必要时参考正文）猜图层。"""
    t = str(title or "")
    for words, layer in LAYER_HINTS:
        for w in words:
            if w in t:
                return layer
    # 标题没线索时看正文开头的用词
    head = str(body or "")[:200]
    for words, layer in LAYER_HINTS:
        for w in words:
            if w in head:
                return layer
    return "world"


def _should_skip(title: str) -> bool:
    t = str(title or "")
    return any(k in t for k in _SKIP_HINTS)


def split_sections(text: str, *, min_chars: int = 12) -> list[dict[str, Any]]:
    """把文档拆成 [(title, body)] 形式的分节。

    :param min_chars: 少于这个字数的段落丢弃（多半是目录条目或残留）。
        默认 12——设定文档里「人物速查」这类小节可能只有一句话，
        阈值设太高会把有效内容整节丢掉。
    """
    raw = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = raw.split("\n")

    sections: list[dict[str, Any]] = []
    cur_title = ""
    cur_part = ""
    buf: list[str] = []

    def flush() -> None:
        nonlocal buf
        body = "\n".join(buf).strip()
        if body and len(body) >= min_chars:
            sections.append(
                {
                    "title": cur_title or body.split("\n")[0][:40],
                    "part": cur_part,
                    "body": body,
                }
            )
        buf = []

    i = 0
    while i < len(lines):
        line = lines[i]
        s = line.strip()

        # 分隔线：下一行非空行视为标题
        if _RE_RULE.match(s):
            nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
            if nxt and not _RE_RULE.match(nxt) and len(nxt) <= 60:
                flush()
                cur_title = nxt
                if "部分" in nxt:
                    cur_part = nxt
                i += 2
                continue
            i += 1
            continue

        m = _RE_CN_HEADING.match(s)
        if m and len(m.group(2)) <= 60:
            flush()
            cur_title = m.group(2).strip()
            i += 1
            continue

        m = _RE_MD_HEADING.match(s)
        if m and len(m.group(2)) <= 60:
            level = len(m.group(1))
            if level <= 3:
                flush()
                cur_title = m.group(2).strip()
                if level == 1:
                    cur_part = cur_title
            i += 1
            continue

        buf.append(line)
        i += 1

    flush()

    # 兜底：一个分节都没识别出来时按空行分段
    if not sections:
        chunks = re.split(r"\n\s*\n", raw)
        for ch in chunks:
            body = ch.strip()
            if len(body) >= min_chars:
                first = body.split("\n")[0].strip()
                sections.append(
                    {
                        "title": first[:40],
                        "part": "",
                        "body": body,
                    }
                )
    return sections


def plan_import(
    text: str,
    *,
    default_layer: str = "world",
    overrides: dict[str, str] | None = None,
    skip: Sequence[str] = (),
    min_chars: int = 12,
) -> dict[str, Any]:
    """生成导入计划（**不写库**）。

    :param overrides: 标题子串 → 强制图层
    :param skip: 标题包含这些子串的分节跳过
    """
    secs = split_sections(text, min_chars=min_chars)
    ov = overrides or {}
    skip_list = list(skip)

    items: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    seen: dict[str, int] = {}

    for sec in secs:
        title = sec["title"]
        if _should_skip(title) or any(k in title for k in skip_list):
            skipped.append({"title": title, "reason": "写作产物或显式跳过"})
            continue

        layer = ""
        for key, val in ov.items():
            if key in title:
                layer = val
                break
        if not layer:
            layer = guess_layer(title, sec["body"]) or default_layer

        # 同名去重：加序号
        base = title
        n = seen.get(base, 0) + 1
        seen[base] = n
        entity = base if n == 1 else f"{base}（{n}）"

        body = sec["body"].strip()
        items.append(
            {
                "entity": entity,
                "layer": layer,
                "layer_cn": LAYER_CN.get(layer, layer),
                "chars": len(body),
                "preview": clip_tokens(body, 60),
                "part": sec.get("part", ""),
                "body": body,
            }
        )

    by_layer: dict[str, int] = {}
    for it in items:
        by_layer[it["layer"]] = by_layer.get(it["layer"], 0) + 1

    return {
        "sections": len(secs),
        "to_import": len(items),
        "skip_count": len(skipped),
        "by_layer": by_layer,
        "items": items,
        "skipped": skipped,
        "total_chars": sum(it["chars"] for it in items),
    }


def render_plan(plan: dict[str, Any], *, preview_items: int = 40) -> str:
    """把导入计划渲染成可读文本。"""
    lines = [
        "【导入计划】（预演，尚未写入）",
        f"  识别分节 {plan.get('sections', 0)} 个 → 将导入 {plan.get('to_import', 0)} 条"
        f"，跳过 {plan.get('skip_count', 0)} 条",
        f"  合计 {plan.get('total_chars', 0)} 字",
    ]
    by_layer = plan.get("by_layer") or {}
    if by_layer:
        parts = [f"{LAYER_CN.get(k, k)} {v}" for k, v in sorted(by_layer.items())]
        lines.append("  分布：" + " · ".join(parts))

    items = plan.get("items") or []
    if items:
        lines.append("\n▸ 将导入的条目：")
        for it in items[:preview_items]:
            lines.append(
                f"  [{it['layer_cn']}] {it['entity']}  {it['chars']}字"
            )
        if len(items) > preview_items:
            lines.append(f"  … 还有 {len(items) - preview_items} 条")

    skipped = plan.get("skipped") or []
    if skipped:
        lines.append("\n▸ 跳过：")
        for s in skipped[:12]:
            lines.append(f"  {s['title']}（{s['reason']}）")

    lines.append(
        "\n确认无误后，用 dry_run=false 真正写入。"
        "写入的条目会参与检索，之后可用 enderwriter_organize 再整理。"
    )
    return "\n".join(lines)


def run_import(
    chest: Any,
    plan: dict[str, Any],
    *,
    chapter_id: str | None = None,
    keyword: str = "",
) -> dict[str, Any]:
    """按计划写库。每条一个版本，可覆写可回滚。"""
    written: list[str] = []
    failed: list[dict[str, str]] = []
    for it in plan.get("items") or []:
        try:
            chest.upsert(
                it["layer"],
                it["entity"],
                it["body"],
                data={"imported": True, "source": keyword} if keyword else {"imported": True},
                chapter_id=chapter_id,
                commit=False,
            )
            written.append(it["entity"])
        except Exception as exc:  # noqa: BLE001
            failed.append({"entity": it["entity"], "error": str(exc)})
    try:
        chest.conn.commit()
    except Exception:  # noqa: BLE001
        pass
    return {"written": written, "failed": failed}
