"""大模型后端：抽象协议 + 离线确定性 Stub + Ollama HTTP 后端。

设计约束（来自环境实测）：
  * 运行环境**无网络、无 GPU、无 Ollama**，因此一切默认走 ``StubBackend``，
    整条 demo 流水线必须能完全离线跑通；
  * 只用 Python 3.12 标准库（``urllib.request`` 调 Ollama，不引入 requests）；
  * 任何后端输出都必须能通过 ``common`` 里的冻结 Schema（§3.1 细纲 / §3.5 回写报告），
    校验失败时抛出**可直接读懂**的 ``SchemaError`` 包装。

四个职责（与规范 §3 一一对应）：
  ``plan``             细纲规划        -> §3.1 chapter_outline
  ``extract_keywords`` 关键词抽取      -> 供 ``common.keyword_table`` / 检索使用
  ``generate``         正文生成        -> 纯文本
  ``writeback``        回写报告        -> §3.5 writeback_report

对外入口：
  ``get_backend(name=None)``：按 ``EW_LLM``（stub|ollama，缺省 stub）选择后端；
  Ollama 不可用时**优雅降级**到 ``StubBackend``（stderr 打印一行中文警告）。
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import sys
import urllib.error
import urllib.request
from typing import Any, Sequence

from . import common as C

__all__ = [
    "LLMBackend",
    "StubBackend",
    "OllamaBackend",
    "get_backend",
    "draft_request",
    "DEFAULT_OLLAMA_URL",
    "DEFAULT_OLLAMA_MODEL",
    "BACKENDS",
]

#: Ollama 默认地址（可用环境变量 ``EW_OLLAMA_URL`` 覆盖）
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434/api/generate"

#: 默认模型（可用环境变量 ``EW_OLLAMA_MODEL`` 覆盖）；3B 级别才能在普通机器上跑得动
DEFAULT_OLLAMA_MODEL = "qwen2.5:3b-instruct"

#: 可选后端名
BACKENDS: tuple[str, ...] = ("stub", "ollama", "llamacpp")

log = C.get_logger("llm")

_IMPORTANCE_CN: dict[str, str] = {"core": "核心", "secondary": "次要", "minor": "路人"}


def warn(msg: str) -> None:
    """打印一行中文警告到 stderr（用 print 而非 logging，保证默认可见）。"""
    sys.stderr.write(f"[警告] {msg}\n")
    sys.stderr.flush()


def schema_error(stage: str, exc: BaseException, payload: Any = None) -> C.SchemaError:
    """把校验失败包装成人类可读的 SchemaError（带阶段名与出错位置）。"""
    detail = str(exc).strip() or exc.__class__.__name__
    preview = ""
    if payload is not None:
        try:
            text = payload if isinstance(payload, str) else C.dumps(payload)
        except Exception:  # pragma: no cover - 防御性兜底
            text = repr(payload)
        preview = f"；原始输出前 200 字：{text[:200]}"
    return C.SchemaError(f"[{stage}] 大模型返回结构不符合 §3 Schema：{detail}{preview}")


# ==========================================================================
# 1. 抽象基类
# ==========================================================================
class LLMBackend:
    """大模型后端协议（抽象基类）。

    所有方法都必须**确定性可复现**地工作，或在无法工作时抛出人类可读的异常；
    任何返回的 dict 都必须满足对应冻结 Schema。
    """

    #: 后端标识（stub / ollama / ...）
    name: str = "base"

    #: 是否真实联网（Stub 为 False，供 CLI 打印与降级判断）
    online: bool = False

    # ---- 四个核心职责 ----
    def plan(self, outline_req: dict) -> dict:
        """§3.1 规划：入参是章节需求 dict，返回合法 ``chapter_outline``。"""
        raise NotImplementedError

    def extract_keywords(self, text: str) -> list[str]:
        """从文本抽取关键词（供检索/细纲派生）。"""
        raise NotImplementedError

    def generate(self, prompt: str, *, max_tokens: int = 512) -> str:
        """生成正文/片段。"""
        raise NotImplementedError

    def writeback(self, text: str, outline: dict) -> dict:
        """§3.5 回写：从正文 + 细纲产出合法 ``writeback_report``。"""
        raise NotImplementedError

    # ---- 生命周期 ----
    def close(self) -> None:
        """释放资源（HTTP 连接等）。默认无操作。"""

    def __enter__(self) -> "LLMBackend":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ---- 工具 ----
    def __repr__(self) -> str:  # pragma: no cover - 仅调试
        return f"<{type(self).__name__} name={self.name!r} online={self.online}>"

    @staticmethod
    def _check_outline(obj: Any) -> dict:
        try:
            return C.validate(obj, C.schema("chapter_outline"))
        except C.SchemaError as exc:
            raise schema_error("plan/chapter_outline", exc, obj) from None

    @staticmethod
    def _check_report(obj: Any) -> dict:
        try:
            return C.validate(obj, C.schema("writeback_report"))
        except C.SchemaError as exc:
            raise schema_error("writeback/writeback_report", exc, obj) from None


# ==========================================================================
# 2. 离线确定性 Stub 后端
# ==========================================================================
#: 生成段落的最小句数：低于此值时只有"场景库耗尽"才会停，避免过短
_MIN_SENTENCES: int = 6
#: 中文玄幻题材的确定性语料（按 hash 种子轮换，保证"可复现"而非"随机"）
_SCENES: tuple[str, ...] = (
    "{who_a}立在{loc}的断壁前，指尖还残留着方才那道灵光的余温。",
    "风从{loc}另一端掠过，把香灰与冷铁的气味一起推到他面前。",
    "{who_b}没有回头，只把手里的{it}轻轻转了一圈：火还在，人就还没输。",
    "远处的钟声敲到第三下时，{who_a}终于听见自己心跳的回响，一下，又一下。",
    "他没有念出那个词，只把它压在舌尖底下，像含着一枚太烫的铁。",
    "黑暗里有什么东西挪动了一寸。{who_a}屏住呼吸，把{it}横在胸前。",
    "光从缝隙中泄下来，照亮{loc}地面上一道陌生的刻痕与半枚烧焦的符纸。",
    "{who_a}忽然笑了。他想起很久以前有人说过，命是可以改的，只要舍得。",
    "血落在石上，声音比想象中要轻；而{loc}深处，有什么沉睡了太久的东西睁开了眼。",
    "他往前走了一步，又一步，直到背影被那团光完全吞没。",
    "{who_b}把声音压得很低，低到只有这处{loc}听得见。",
    "石阶上多了一道新痕。刻痕很浅，却正对着他自己的名字。",
    "他听见灯芯毕剥响了一声，那声音不像火，像有人在很远的地方数着数。",
    "雾散开一点，露出半截断剑和一地没人收殓的旧衣。",
    "{who_a}把{it}收进怀里时，指腹被烫出一道浅白的印子。",
    "有人在背后叫他的名字，用的是他不愿承认的那个称呼。",
)

_OPENINGS: tuple[str, ...] = (
    "夜还未散。",
    "雪下了一整夜，至今没有停。",
    "那是他第一次真正害怕。",
    "旧账总要在某一天被翻出来。",
)

_CLOSINGS: tuple[str, ...] = (
    "这一夜，无人安睡。",
    "而真正的局，才刚刚开始。",
    "他合上门，把答案留在了门外。",
    "远处的山脊上，有人点亮了一盏灯。",
)

#: 回写报告里可能出现的状态词 -> 规范状态机取值
_STATUS_WORDS: dict[str, str] = {
    "伏笔": "planted",
    "埋下": "planted",
    "埋": "planted",
    "强化": "reinforced",
    "复现": "reinforced",
    "回收": "resolved",
    "兑现": "resolved",
    "废弃": "abandoned",
}

_FS_ID_RE = re.compile(r"(fs_[A-Za-z0-9_]+)")


def _stable_seed(text: str) -> int:
    """确定性种子：blake2b 摘要 -> 整数（跨进程一致，不受 PYTHONHASHSEED 影响）。"""
    return int.from_bytes(hashlib.blake2b(text.encode("utf-8"), digest_size=8).digest(), "little")


def _first(seq: Sequence[Any], default: Any) -> Any:
    return seq[0] if seq else default


def _next_fs_id(taken: set[str], start: int = 101) -> str:
    """在 ``taken`` 之外找一个可用的 ``fs_NNN`` 占位 ID（从 fs_101 起，避开示例数据）。"""
    n = start
    while C.make_id("fs", n) in taken:
        n += 1
    return C.make_id("fs", n)


#: 关键词过滤：这些双字组没有信息量
_STOP_BIGRAMS: frozenset[str] = frozenset(
    {
        "一下", "又一", "一步", "一下", "一个", "什么", "已经", "自己", "的时", "他的", "她的",
        "在了", "是一", "的是", "这个", "那个", "可以", "没有", "时候", "起来", "出来", "一直",
        "了一", "了一", "像是", "像在", "直到", "终于", "忽然", "仍然", "还是", "只是", "但是",
        "然后", "于是", "所以", "因为", "而且", "或者", "如果", "虽然", "不过", "这样", "那样",
        "的话", "地方", "东西", "他们", "我们", "你们", "看见", "听见", "低声", "远处",
    }
)


class StubBackend(LLMBackend):
    """完全离线的确定性后端：不联网、不随机（用输入 hash 做种子）。

    它既是 demo 的默认后端，也是单元测试的"金标准"——同样的输入永远得到同样的输出。
    """

    name = "stub"
    online = False

    #: 默认章节字数（``max_tokens`` 未给出或过大时使用）
    default_chars = 420

    def __init__(
        self,
        *,
        vocabulary: Sequence[str] = (),
        default_importance: str = "core",
        known_foreshadowings: Sequence[str] = (),
    ) -> None:
        self.vocabulary = list(C.dedupe([v for v in vocabulary if v]))
        self.default_importance = default_importance if default_importance in _IMPORTANCE_CN else "core"
        #: 已知伏笔 ID：``writeback`` 给"待埋新伏笔"分配占位 ID 时要避开它们
        self.known_foreshadowings: list[str] = list(C.dedupe([f for f in known_foreshadowings if f]))
        self.calls: dict[str, int] = {"plan": 0, "extract_keywords": 0, "generate": 0, "writeback": 0}

    # ------------------------------------------------------------------
    # 2.1 规划 -> §3.1 chapter_outline
    # ------------------------------------------------------------------
    def plan(self, outline_req: dict) -> dict:
        """把"章节需求"铺成一份合法细纲（9 个必填字段齐全）。

        需求 dict 支持的键（都可缺省，缺省时从 ``vocabulary`` 取）：
          ``chapter_id`` / ``chapter_goal`` / ``goal`` / ``summary`` / ``focus``
          ``characters`` / ``active_characters`` / ``locations`` / ``items``
          ``resolve`` / ``foreshadowing_to_resolve`` / ``plant`` /
          ``foreshadowing_to_plant`` / ``mood`` / ``style_keywords`` / ``seed``
        """
        self.calls["plan"] += 1
        req = dict(outline_req or {})
        seed_text = C.dumps(req)
        rng = random.Random(_stable_seed("plan:" + seed_text))

        chapter_id = self._norm_chapter_id(
            req.get("chapter_id") or req.get("chapter") or req.get("id") or "ch_001"
        )
        goal = str(
            req.get("chapter_goal")
            or req.get("goal")
            or req.get("summary")
            or req.get("focus")
            or f"承前启后：推进「{chapter_id}」的核心冲突"
        ).strip()
        if not goal:
            goal = f"推进「{chapter_id}」的核心冲突"

        chars = self._as_named_list(
            req.get("active_characters") or req.get("characters") or req.get("entities")
        )
        if not chars and self.vocabulary:
            chars = [{"name": self.vocabulary[0], "importance": self.default_importance}]

        locs = self._as_name_list(req.get("locations") or req.get("location"))
        if not locs:
            locs = [self._place_fallback(req)]

        items = self._as_named_list(req.get("items") or req.get("item"))

        to_resolve = self._as_fs_list(
            req.get("foreshadowing_to_resolve") or req.get("resolve") or req.get("resolves")
        )
        to_plant = self._as_fs_list(
            req.get("foreshadowing_to_plant") or req.get("plant") or req.get("plants")
        )

        mood = str(req.get("mood") or "").strip() or self._mood_from(goal, rng)
        styles = [str(s) for s in (req.get("style_keywords") or req.get("styles") or []) if str(s)]
        if not styles:
            styles = ["冷峻", "短句", "留白"]

        outline = {
            "chapter_id": chapter_id,
            "chapter_goal": goal,
            "active_characters": chars,
            "locations": locs,
            "items": items,
            "foreshadowing_to_resolve": to_resolve,
            "foreshadowing_to_plant": to_plant,
            "mood": mood,
            "style_keywords": styles[:6],
        }
        # 附加元信息（Schema 允许 additionalProperties）
        outline["planned_by"] = "stub"
        outline["pov"] = self._first_char_name(chars)
        return self._check_outline(outline)

    # ------------------------------------------------------------------
    # 2.2 关键词抽取
    # ------------------------------------------------------------------
    def extract_keywords(self, text: str, *, limit: int = 24) -> list[str]:
        """确定性关键词：优先词表实体（按出现顺序），再补中文双字组高频词。"""
        self.calls["extract_keywords"] += 1
        text = str(text or "")
        if not text:
            return []
        out: list[str] = []
        # 1) 词表实体（长词优先由 lexicon.extract_entities 保证）
        if self.vocabulary:
            from . import lexicon

            try:
                out.extend(lexicon.extract_entities(text, frozenset(self.vocabulary)))
            except Exception:  # pragma: no cover - 词表异常时退化为纯子串
                out.extend(v for v in self.vocabulary if v in text)
        # 2) 中文双字组按频次
        try:
            from . import lexicon as _lex

            terms = _lex.analyze(text)
        except Exception:  # pragma: no cover
            terms = []
        freq: dict[str, int] = {}
        for t in terms:
            if len(t) < 2 or t.isdigit() or t in _STOP_BIGRAMS:
                continue
            if t in out:
                continue
            freq[t] = freq.get(t, 0) + 1
        ranked = sorted(freq.items(), key=lambda kv: (-kv[1], kv[0]))
        out.extend(t for t, _ in ranked if freq[t] > 1)
        known = set(out) | set(self.vocabulary)
        # 3) 组合词去重：若某词是更长已知词的子串（"断魂" ⊂ "示例卷"），丢弃它
        out = [
            t for t in out
            if not any(t != k and t in k for k in known)
        ]
        return C.dedupe(out)[:limit]

    # ------------------------------------------------------------------
    # 2.3 正文生成
    # ------------------------------------------------------------------
    def generate(self, prompt: str, *, max_tokens: int = 512) -> str:
        """用输入 hash 做种子拼出可复现的中文段落，字数约为 ``max_tokens``。"""
        self.calls["generate"] += 1
        prompt = str(prompt or "")
        target = int(max_tokens or 0)
        # 中文 ≈ 1 token/字，估保守一点：目标字数 = 0.85 ~ 1.0 倍
        if target <= 0:
            target = self.default_chars
        chars_budget = max(80, int(target * random.Random(_stable_seed("len:" + prompt)).uniform(0.85, 1.0)))

        info = self._parse_prompt(prompt)
        rng = random.Random(_stable_seed("gen:" + prompt))

        who_a = _first(info["characters"], "他")
        who_b = info["characters"][1] if len(info["characters"]) > 1 else who_a
        loc = _first(info["locations"], "山谷")
        item = _first(info["items"], "残刃")
        goal = info["goal"] or "活下去"
        mood = info["mood"] or "压抑"

        parts: list[str] = [rng.choice(_OPENINGS)]
        used = C.est_tokens(parts[0])
        text_so_far = parts[0]
        used_indices: list[int] = []
        emitted: set[str] = {parts[0]}
        guard = 0
        # 主角必须登场：先把一条含 who_a 的句子放到前面（正文再短也不漏主角）
        hero_pool = [i for i in range(len(_SCENES)) if "{who_a}" in _SCENES[i]]
        if hero_pool and who_a:
            idx = hero_pool[rng.randrange(len(hero_pool))]
            hero = _SCENES[idx].format(who_a=who_a, who_b=who_b, loc=loc, it=item, goal=goal, mood=mood)
            if used + C.est_tokens(hero) <= chars_budget:
                parts.append(hero)
                used_indices.append(idx)
                emitted.add(hero)
                used += C.est_tokens(hero)
        while used < chars_budget * 0.92 and guard < 120:
            guard += 1
            pool = [i for i in range(len(_SCENES)) if i not in used_indices]
            if not pool:
                # 场景库已用尽：宁可提前收尾，也不复制粘贴同一句
                break
            if used > chars_budget * 0.5 and len(used_indices) >= _MIN_SENTENCES:
                # 已经写够体量，且主角尚未登场时继续抽（补齐出场），否则收尾
                if not info["characters"] or all(c in text_so_far for c in info["characters"][:1]):
                    break
            idx = pool[rng.randrange(len(pool))]
            used_indices.append(idx)
            tpl = _SCENES[idx]
            sentence = tpl.format(who_a=who_a, who_b=who_b, loc=loc, it=item, goal=goal, mood=mood)
            if sentence in emitted:
                continue
            cost = C.est_tokens(sentence)
            if used + cost > chars_budget:
                # 剩余空间只够收尾，别硬塞
                continue
            parts.append(sentence)
            emitted.add(sentence)
            text_so_far += sentence
            used += cost
        closing = rng.choice(_CLOSINGS)
        if closing not in emitted:
            parts.append(closing)
        text = "".join(parts)
        return C.clip_tokens(text, chars_budget)

    # ------------------------------------------------------------------
    # 2.4 回写报告 -> §3.5 writeback_report
    # ------------------------------------------------------------------
    def writeback(self, text: str, outline: dict) -> dict:
        """从正文 + 细纲推导回写报告（确定性）。

        规则：
          * 出场的 active_characters -> ``updated.characters``；
          * items / 正文提及的伏笔 ID -> ``updated.items`` / ``updated.foreshadowing``；
          * ``foreshadowing_to_resolve`` 中的 ID 若在正文里出现 -> status=resolved，
            否则 status=reinforced（只提及未兑现）；
          * ``foreshadowing_to_plant`` -> status=planted，按细纲顺序分配 ``fs_NNN`` 占位 ID；
          * 细纲中的地点 -> ``updated.world``；风格关键词 -> ``updated.style``；
          * 未在正文中出现的关键实体 -> ``warnings``（供回写器提示"细纲没落地"）。
        """
        self.calls["writeback"] += 1
        outline = dict(outline or {})
        text = str(text or "")
        chapter_id = self._norm_chapter_id(outline.get("chapter_id") or "ch_001")

        characters: list[str] = []
        missing: list[str] = []
        for ch in self._as_named_list(outline.get("active_characters")):
            characters.append(ch["name"])
            if text and ch["name"] not in text:
                missing.append(ch["name"])

        items: list[str] = []
        for it in self._as_named_list(outline.get("items")):
            items.append(it["name"])

        foreshadowing: list[dict[str, Any]] = []
        mentioned: set[str] = set(_FS_ID_RE.findall(text))
        for rec in self._as_fs_list(outline.get("foreshadowing_to_resolve")):
            fs_id = rec["id"]
            status = "resolved" if (not text or fs_id in mentioned) else "reinforced"
            foreshadowing.append({"id": fs_id, "status": status})
        taken = set(mentioned) | {f["id"] for f in foreshadowing} | set(self.known_foreshadowings)
        for rec in self._as_fs_list(outline.get("foreshadowing_to_plant")):
            fs_id = rec["id"]
            if fs_id is None:
                fs_id = _next_fs_id(taken)
            taken.add(fs_id)
            item: dict[str, Any] = {"id": fs_id, "status": "planted"}
            if rec.get("content"):
                item["content"] = rec["content"]
            if rec.get("planned_resolve"):
                item["planned_resolve"] = rec["planned_resolve"]
            foreshadowing.append(item)
        n_extra = len([f for f in foreshadowing if f["status"] == "resolved"])

        report: dict[str, Any] = {
            "chapter_id": chapter_id,
            "updated": {
                "characters": C.dedupe(characters),
                "items": C.dedupe(items),
                "timeline": [chapter_id],
                "foreshadowing": foreshadowing,
                "world": [str(x) for x in (outline.get("locations") or []) if str(x)],
                "style": [str(x) for x in (outline.get("style_keywords") or []) if str(x)],
            },
            "warnings": [f"{name} 在细纲中登场，但正文未出现（疑似漏写）" for name in missing],
            "smoke": {"backend": "stub", "chars": len(text), "mentioned_ids": sorted(mentioned)},
        }
        if n_extra:
            report["patrol"] = [f"{f['id']} 已回收" for f in foreshadowing if f["status"] == "resolved"]
        return self._check_report(report)

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    @staticmethod
    def _norm_chapter_id(value: Any) -> str:
        if isinstance(value, str) and re.fullmatch(r"ch_\d+", value.strip()):
            return value.strip()
        n = C.chapter_index(value if not isinstance(value, bool) else None)
        if n <= 0 or (n == 0 and str(value).strip() in ("0", "ch_0", "ch_000")):
            return "ch_001"
        return f"ch_{n:03d}"

    def _place_fallback(self, req: dict) -> str:
        for key in ("location", "place", "world"):
            v = req.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return "无名山路"

    @staticmethod
    def _mood_from(goal: str, rng: random.Random) -> str:
        moods = ("压抑", "紧张", "苍凉", "炽烈", "诡谲")
        if "战" in goal or "杀" in goal:
            return "肃杀"
        if "别" in goal or "死" in goal:
            return "悲怆"
        return moods[rng.randrange(len(moods))]

    @staticmethod
    def _first_char_name(chars: Sequence[dict]) -> str:
        for c in chars:
            if c.get("name"):
                return c["name"]
        return ""

    def _as_named_list(self, value: Any) -> list[dict[str, str]]:
        """把 ["甲","乙"] / [{"name":"甲"}] / "甲,乙" 统一成 [{"name","importance"}]。"""
        out: list[dict[str, str]] = []
        seen: set[str] = set()
        for raw in self._iter_items(value):
            if isinstance(raw, dict):
                name = str(raw.get("name") or raw.get("id") or "").strip()
                imp = str(raw.get("importance") or self.default_importance)
            else:
                name = str(raw).strip()
                imp = self.default_importance
            if not name or name in seen:
                continue
            if imp not in _IMPORTANCE_CN:
                imp = self.default_importance
            seen.add(name)
            item: dict[str, str] = {"name": name, "importance": imp}
            if isinstance(raw, dict):
                for extra_key in ("role", "note", "why"):
                    if raw.get(extra_key):
                        item[extra_key] = str(raw[extra_key])
            out.append(item)
        return out

    @staticmethod
    def _as_name_list(value: Any) -> list[str]:
        out: list[str] = []
        for raw in StubBackend._iter_items(value):
            name = str(raw.get("name") if isinstance(raw, dict) else raw).strip()
            if name and name not in out:
                out.append(name)
        return out

    @staticmethod
    def _as_fs_list(value: Any) -> list[dict[str, Any]]:
        """把伏笔需求统一成 ``[{"id": "fs_001"|None, "content": "..."}]``。

        没给出合法 ID 的条目 ``id`` 为 ``None``——由调用方决定分配什么占位 ID
        （如 ``writeback`` 会避开末影箱里已占用的编号）。
        """
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in StubBackend._iter_items(value):
            if isinstance(raw, dict):
                fs_id = str(raw.get("id") or "").strip()
                content = str(raw.get("content") or "").strip()
            else:
                fs_id = str(raw).strip() if str(raw).startswith("fs_") else ""
                content = str(raw).strip()
            if not re.fullmatch(r"fs_[A-Za-z0-9_]+", fs_id):
                m = _FS_ID_RE.search(content)
                fs_id = m.group(1) if m else ""
            if fs_id and fs_id in seen:
                continue
            if fs_id:
                seen.add(fs_id)
            rec: dict[str, Any] = {"id": fs_id or None}
            if content:
                rec["content"] = content
            if isinstance(raw, dict) and raw.get("planned_resolve"):
                rec["planned_resolve"] = str(raw["planned_resolve"])
            out.append(rec)
        return out

    @staticmethod
    def _iter_items(value: Any) -> list[Any]:
        if value is None:
            return []
        if isinstance(value, str):
            return [v.strip() for v in re.split(r"[,，、;；\n]+", value) if v.strip()]
        if isinstance(value, (list, tuple, set)):
            return list(value)
        return [value]

    @staticmethod
    def _parse_prompt(prompt: str) -> dict[str, Any]:
        """从（可能由编排层拼装的）prompt 里尽量提取生成要素。

        约定标记（大小写不敏感）::

            角色: 张三, 李四
            地点: 归墟古战场
            物品: 断魂钉
            目标: 夺取残魂
            情绪: 压抑
        """
        info: dict[str, Any] = {
            "characters": [],
            "locations": [],
            "items": [],
            "goal": "",
            "mood": "",
        }
        keys = {
            "角色": "characters", "人物": "characters", "出场": "characters", "characters": "characters",
            "地点": "locations", "场景": "locations", "locations": "locations",
            "物品": "items", "道具": "items", "items": "items",
            "目标": "goal", "chapter_goal": "goal", "本章目标": "goal",
            "情绪": "mood", "氛围": "mood", "mood": "mood",
        }
        for line in prompt.splitlines():
            line = line.strip().lstrip("#-*　 ").strip()
            m = re.match(r"^([^:：]{1,12})[:：]\s*(.+)$", line)
            if not m:
                continue
            label = m.group(1).strip().lower()
            body = m.group(2).strip()
            field = keys.get(label)
            if field is None:
                continue
            if field in ("characters", "locations", "items"):
                # 只取中文/拉丁名，掐掉括号里的解释
                names = [
                    re.sub(r"[（(].*?[)）]", "", x).strip()
                    for x in re.split(r"[,，、/;；]+", body)
                ]
                info[field] = [n for n in names if n]
            else:
                info[field] = body
        return info


# ==========================================================================
# 3. Ollama HTTP 后端
# ==========================================================================
class OllamaBackend(LLMBackend):
    """用标准库 ``urllib`` 调 Ollama ``/api/generate``。

    * 地址：``EW_OLLAMA_URL``（缺省 ``http://127.0.0.1:11434/api/generate``）
    * 模型：``EW_OLLAMA_MODEL``（缺省 ``qwen2.5:3b-instruct``）
    * 请求失败（连不上 / 超时 / 非 200）一律抛 ``RuntimeError``，消息可直接读懂；
    * 模型返回的 JSON 结构用 ``common.validate`` 校验，失败抛包装后的 ``SchemaError``；
    * 规划失败时退回 ``StubBackend`` 的细纲骨架（保证 demo 不会因模型跑偏而中断），
      但**结构化接口（generate）失败不吞异常**——调用方需要知道模型真的没回话。
    """

    name = "ollama"
    online = True

    def __init__(
        self,
        url: str | None = None,
        model: str | None = None,
        *,
        timeout: float = 120.0,
        temperature: float = 0.7,
        allow_stub_fallback: bool = True,
    ) -> None:
        self.url = str(url or os.environ.get("EW_OLLAMA_URL") or DEFAULT_OLLAMA_URL)
        self.model = str(model or os.environ.get("EW_OLLAMA_MODEL") or DEFAULT_OLLAMA_MODEL)
        self.timeout = float(timeout)
        self.temperature = float(temperature)
        self.allow_stub_fallback = bool(allow_stub_fallback)
        self._fallback = StubBackend()
        self.last_meta: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # 3.1 底层 HTTP
    # ------------------------------------------------------------------
    def _post(self, prompt: str, *, system: str = "", json_schema: dict | None = None,
              max_tokens: int = 512) -> str:
        payload: dict[str, Any] = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": self.temperature,
                "num_predict": int(max_tokens),
            },
        }
        if system:
            payload["system"] = system
        if json_schema is not None:
            payload["format"] = json_schema
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.url,
            data=data,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:  # 4xx/5xx
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="replace")[:300]
            except Exception:  # pragma: no cover
                pass
            raise RuntimeError(
                f"Ollama 返回 HTTP {exc.code}（{self.url}，模型 {self.model}）：{body or exc.reason}"
            ) from None
        except urllib.error.URLError as exc:
            raise RuntimeError(
                f"无法连接 Ollama：{self.url}（模型 {self.model}）；原因：{exc.reason}。"
                f"请先运行 `ollama serve` 并 `ollama pull {self.model}`，或改用 --llm stub。"
            ) from None
        except (TimeoutError, OSError) as exc:
            raise RuntimeError(f"调用 Ollama 超时或网络异常（{self.url}）：{exc}") from None
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Ollama 返回非 JSON 内容（{self.url}）：{raw[:200]}；{exc}") from None
        if not isinstance(obj, dict):
            raise RuntimeError(f"Ollama 返回结构异常（期望 object，实得 {type(obj).__name__}）")
        if obj.get("error"):
            raise RuntimeError(f"Ollama 报告错误：{obj['error']}")
        response = obj.get("response")
        if not isinstance(response, str):
            raise RuntimeError(f"Ollama 返回缺少 response 字段：{str(obj)[:200]}")
        self.last_meta = {
            "model": obj.get("model", self.model),
            "done_reason": obj.get("done_reason"),
            "eval_count": obj.get("eval_count"),
            "total_duration": obj.get("total_duration"),
        }
        return response

    # ------------------------------------------------------------------
    # 3.2 四个职责
    # ------------------------------------------------------------------
    def generate(self, prompt: str, *, max_tokens: int = 512) -> str:
        system = "你是中文长篇小说写作助手。只输出正文段落，不要解释、不要小标题、不要 Markdown。"
        text = self._post(prompt, system=system, max_tokens=max_tokens).strip()
        if not text:
            raise RuntimeError(f"Ollama（{self.model}）返回了空正文；可降低 --window 或换模型。")
        return text

    def plan(self, outline_req: dict) -> dict:
        """把章节需求交给模型，要求它按 §3.1 填一份细纲 JSON。"""
        req_text = C.dumps(outline_req)
        prompt = (
            "你是长篇小说的章节规划器。请严格按下面的 JSON Schema 输出**一个 JSON 对象**，"
            "不要输出任何解释或 Markdown 代码块。\n"
            f"JSON Schema：{C.dumps(C.schema('chapter_outline'))}\n"
            f"章节需求：{req_text}\n"
        )
        raw = self._post(prompt, max_tokens=700, json_schema=C.schema("chapter_outline"))
        obj = _loads_json_loose(raw)
        try:
            return self._check_outline(obj)
        except C.SchemaError:
            if not self.allow_stub_fallback:
                raise
            log.warning("Ollama 细纲未通过 Schema 校验，退回 Stub 骨架（原始输出：%s）", raw[:200])
            warn(f"Ollama 细纲不符合 §3.1 Schema，已退回离线 Stub 骨架：{raw[:80]}…")
            return self._fallback.plan(outline_req)

    def extract_keywords(self, text: str) -> list[str]:
        prompt = (
            "抽取下面文本的关键词（人名、地名、物品、事件），每行一个，不要编号，不要解释。\n"
            f"文本：\n{str(text)[:2000]}"
        )
        raw = self._post(prompt, max_tokens=200)
        out = [
            re.sub(r"^[\s\d.、\-*·•)）]+", "", line).strip()
            for line in raw.splitlines()
        ]
        out = [x for x in out if 1 < len(x) <= 30]
        if out:
            return C.dedupe(out)[:24]
        # 模型没给出东西就退回确定性的数字词法（不联网也能用）
        return self._fallback.extract_keywords(text)

    def writeback(self, text: str, outline: dict) -> dict:
        prompt = (
            "你是小说回写器。请根据正文与细纲，严格按下面的 JSON Schema 输出**一个 JSON 对象**，"
            "不要输出解释或 Markdown。\n"
            f"JSON Schema：{C.dumps(C.schema('writeback_report'))}\n"
            f"细纲：{C.dumps(outline)}\n"
            f"正文：\n{str(text)[:4000]}\n"
        )
        raw = self._post(prompt, max_tokens=700, json_schema=C.schema("writeback_report"))
        obj = _loads_json_loose(raw)
        try:
            return self._check_report(obj)
        except C.SchemaError:
            if not self.allow_stub_fallback:
                raise
            warn(f"Ollama 回写报告不符合 §3.5 Schema，已改用离线 Stub 推导：{raw[:80]}…")
            return self._fallback.writeback(text, outline)

    def close(self) -> None:
        self.last_meta = {}


def _loads_json_loose(raw: str) -> Any:
    """宽松解析模型输出：去掉 ```json 围栏，截取第一个 {...} 片段。"""
    text = (raw or "").strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        snippet = text[start : end + 1]
        try:
            return json.loads(snippet)
        except json.JSONDecodeError as exc:
            raise schema_error("ollama/json", C.SchemaError(f"模型输出不是合法 JSON：{exc}"), raw) from None
    raise schema_error("ollama/json", C.SchemaError("模型输出中找不到 JSON 对象"), raw)


# ==========================================================================
# 4. 工厂：EW_LLM / 参数 / 优雅降级
# ==========================================================================
def _probe(url: str, *, timeout: float = 0.8) -> str | None:
    """探测 Ollama 是否在线。返回 None 表示可用，否则返回中文原因。

    ``/api/generate`` 只接受 POST，所以拿不含 ``model`` 的空请求去探：
    服务不在 -> URLError；服务在 -> 400 Bad Request（说明端口是活的）。
    """
    try:
        req = urllib.request.Request(
            url,
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: F841
            return None
    except urllib.error.HTTPError:
        return None  # 端口活着（通常是 400/404），就当它可用
    except urllib.error.URLError as exc:
        return f"连接失败：{exc.reason}"
    except (TimeoutError, OSError) as exc:
        return f"连接失败：{exc}"


def _get_llamacpp_backend(*, offline_ok: bool = True) -> LLMBackend:
    """llama.cpp 本地 GGUF 后端；不可用时按 ``offline_ok`` 降级或抛错。"""
    try:
        from .llamacpp import load_gguf_backend  # 惰性导入，避免无谓依赖
    except ImportError as exc:  # pragma: no cover
        if not offline_ok:
            raise RuntimeError(f"llamacpp 模块不可用：{exc}") from exc
        warn("llamacpp 模块不可用；已降级为离线 StubBackend。")
        return StubBackend()

    backend = load_gguf_backend()
    if backend is not None:
        return backend

    msg = (
        "llama.cpp 后端不可用（缺少 llama-server 或 GGUF 模型）。"
        "运行 `python -m enderwriter install --engine llamacpp` 自动安装"
    )
    if not offline_ok:
        raise RuntimeError(msg + "；或改用 --llm stub。")
    warn(msg + "；已自动降级为离线 StubBackend。")
    return StubBackend()


def get_backend(
    name: str | None = None,
    *,
    model: str | None = None,
    url: str | None = None,
    timeout: float | None = None,
    offline_ok: bool = True,
) -> LLMBackend:
    """按名字/环境变量选择后端，并做**优雅降级**。

    优先级：显式参数 ``name`` > 环境变量 ``EW_LLM`` > ``"stub"``。
    ``ollama`` 不可用且 ``offline_ok=True`` 时，stderr 打印一行中文警告并返回 StubBackend。
    未知名字抛 ``ValueError``。
    """
    choice = (name or os.environ.get("EW_LLM") or "stub").strip().lower()
    if choice in ("", "none"):
        choice = "stub"
    if choice not in BACKENDS:
        raise ValueError(f"未知 LLM 后端：{choice!r}；可选 {BACKENDS}（或设置环境变量 EW_LLM）")
    if choice == "stub":
        return StubBackend()

    if choice == "llamacpp":
        return _get_llamacpp_backend(offline_ok=offline_ok)

    target_url = str(url or os.environ.get("EW_OLLAMA_URL") or DEFAULT_OLLAMA_URL)
    reason = _probe(target_url)
    if reason is not None:
        msg = f"Ollama 不可用（{target_url}）：{reason}"
        if not offline_ok:
            raise RuntimeError(msg + "；请启动 ollama 或改用 --llm stub。")
        warn(msg + "；已自动降级为离线 StubBackend。")
        return StubBackend()
    try:
        return OllamaBackend(
            target_url,
            model,
            timeout=120.0 if timeout is None else float(timeout),
        )
    except Exception as exc:  # pragma: no cover - 构造期异常罕见
        if not offline_ok:
            raise
        warn(f"初始化 OllamaBackend 失败：{exc}；已自动降级为离线 StubBackend。")
        return StubBackend()


# ==========================================================================
# 5. 末影箱 -> 章节需求（规划入参）的最小胶水
# ==========================================================================
def next_chapter_id(chest: Any) -> str:
    """下一章的规范 ID（``ch_NNN``）。

    注：``INTERFACES.md`` 里列了 ``EnderChest.next_chapter_id()``，但当前落盘的
    ``chest/store.py`` 并未实现它，因此这里在应用层补一个只读版：
    取已有章节最大序号 + 1（无章节则为 ``ch_001``）。
    """
    fn = getattr(chest, "next_chapter_id", None)
    if callable(fn):
        try:
            return str(fn())
        except Exception:  # pragma: no cover - 旧实现异常时走兜底
            pass
    try:
        chapters = list(chest.iter_chapters())
    except Exception:  # pragma: no cover
        chapters = []
    top = 0
    for ch in chapters:
        top = max(top, C.chapter_index(ch.get("chapter_id") if isinstance(ch, dict) else ch))
    return f"ch_{max(1, top + 1):03d}"


def draft_request(chest: Any, chapter_id: str | None = None, *, focus: str = "") -> dict:
    """把末影箱的当前状态压成一份「章节需求」（可直接喂给 ``LLMBackend.plan``）。

    **只读**：不写任何东西。取用内容：
      * 章节号（缺省 ``chest.next_chapter_id()``）；
      * 未完伏笔（最多 2 条）作为 ``resolve`` 候选；
      * 人物格中的角色（首位 core，其余 secondary）；
      * 这些角色状态里的 ``location``（或世界格里的地点）；
      * 物品格前 3 件、风格格前 3 条作为 ``style_keywords``。

    :param chest: ``EnderChest`` 实例
    :param chapter_id: 指定章节号
    :param focus: 本章焦点，拼进 ``chapter_goal``
    """
    cid = str(chapter_id or next_chapter_id(chest))
    open_fs = chest.all_foreshadowings(only_open=True)
    resolve = [f.id for f in open_fs[:2]]

    named = [str(e) for e in chest.all_entities(layers=["character"])]
    named.sort(key=lambda n: (0 if str(chest.get_state(n, "alive")) == "True" else 1, len(n)))
    locations: list[str] = []
    for ent in named:
        loc = chest.get_state(ent, "location")
        if isinstance(loc, str) and loc and loc not in locations:
            locations.append(loc)
    if not locations:
        locations = [str(e) for e in chest.all_entities(layers=["world"])][:2]
    items = [str(e) for e in chest.all_entities(layers=["item"])][:3]
    styles = [str(e) for e in chest.all_entities(layers=["style"])][:3]

    goal = focus or (f"推进「{named[0]}」的主线" if named else f"推进第 {C.chapter_index(cid)} 章")
    due = [f.id for f in open_fs if f.planned_resolve == cid]
    if due:
        goal = f"{goal}（本章按计划回收 {'、'.join(due)}）"

    return {
        "chapter_id": cid,
        "chapter_goal": goal,
        "characters": [
            {"name": n, "importance": "core" if i == 0 else "secondary"}
            for i, n in enumerate(named[:3])
        ],
        "locations": locations[:2],
        "items": items,
        "resolve": resolve,
        "plant": [],
        "style_keywords": styles,
    }
