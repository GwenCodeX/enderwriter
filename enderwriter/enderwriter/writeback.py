"""回写代理（WriteBack）与伏笔巡检（patrol）。

职责（对应规范 §3.5 与第四类矛盾检测）：

* :class:`WriteBackAgent` —— 把大模型产出的 ``writeback_report`` 落到末影箱：
  人物/物品/时间/世界/风格格子 ``upsert`` + 状态索引 ``set_state``；
  伏笔按 ``status`` 走状态机（planted / reinforced / resolved / abandoned）；
  报告带 ``text`` / ``summary`` 时顺带 ``put_chapter``。
* :meth:`WriteBackAgent.detect_contradictions` —— 四类矛盾检测（死而复生、
  时间倒错、能力/规则违规、称呼漂移），只产出人类可读中文 warning，不阻断。
* :func:`patrol` —— 伏笔巡检：超期（overdue）/ 陈旧（stale）/ 已解决仍挂账。

本模块只依赖冻结契约 ``common`` 与 ``chest.store`` 的公开签名，不修改它们。

**关于冻结层的一个实测陷阱**（本模块用 ``extra=`` 规避，详见 :func:`_foreshadow_extra`）：
``store.set_foreshadow`` 先构造 ``data`` 再执行 ``data.update(extra)``，而
``reinforce_foreshadow`` / ``resolve_foreshadow`` / ``abandon_foreshadow`` 传入的是
**位置性的默认值**（``reinforced_chapters=[]``、``resolved_chapter=None``），于是
``data.update(extra)`` 会用 ``extra`` 里**旧的**值把它们覆盖回去——返回值看着对，
但**落盘后读回**会丢掉 ``reinforced_chapters`` 与 ``resolved_chapter``。
本模块因此把权威值同时写进 ``extra``，保证「返回值 == 读回值」。
"""

from __future__ import annotations

import copy
import re
import unicodedata
from typing import Any, Mapping, Sequence

from .common import (
    LAYER_CN,
    SchemaError,
    chapter_index,
    dedupe,
    dumps,
    get_logger,
    normalize_name,
    schema,
    validate,
)
from .chest import Foreshadow

__all__ = [
    "WriteBackAgent",
    "patrol",
    "SEVERITY_OVERDUE",
    "SEVERITY_STALE",
    "SEVERITY_UNRESOLVED",
]

log = get_logger("writeback")

#: patrol 返回项的严重度权重（升序 = 越先返回）
SEVERITY_OVERDUE = 0
SEVERITY_STALE = 1
SEVERITY_UNRESOLVED = 2
#: 未定回收计划——不是写作出错，而是"这条伏笔无法被巡检"。
#: 排在最后：它不紧急，但它是 patrol 失灵的根因，必须让人看见。
SEVERITY_UNPLANNED = 3


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------
def _fs_data(fs: Foreshadow | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(fs, Foreshadow):
        return dict(fs.data or {})
    data = fs.get("data")
    return dict(data) if isinstance(data, Mapping) else {}


def _foreshadow_extra(
    fs: Foreshadow | Mapping[str, Any],
    *,
    status: str,
    planted_chapter: str | None,
    planned_resolve: str | None,
    reinforced_chapters: Sequence[str],
    resolved_chapter: str | None,
    **more: Any,
) -> dict[str, Any]:
    """构造 ``set_foreshadow(extra=...)`` 的权威载荷。

    冻结层的 ``data.update(extra)`` 是**最后一次写入机会**，因此把所有需要在
    落盘后保持正确的字段都放进来（见模块文档「实测陷阱」）。
    """
    extra: dict[str, Any] = dict(_fs_data(fs))
    if status != "abandoned":
        # 非废弃状态不应残留「废弃候选」标记（patrol 打过的话）
        extra.pop("abandon_candidate", None)
    extra.update(
        {
            "status": status,
            "planted_chapter": planted_chapter,
            "planned_resolve": planned_resolve,
            "reinforced_chapters": list(reinforced_chapters),
            "resolved_chapter": resolved_chapter,
        }
    )
    extra.update(more)
    return extra


def _as_text(value: Any) -> str:
    """把状态值转成可读文本（``set_state`` 对非 str 存的是 JSON）。"""
    if isinstance(value, str):
        return value
    return dumps(value)


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        v = normalize_name(value)
        if v in ("true", "1", "yes", "是", "真"):
            return True
        if v in ("false", "0", "no", "否", "假"):
            return False
    return None


def _norm_entity(chest: Any, name: Any) -> str:
    """实体名归一化：先 ``resolve_entity``（别名→标准名），返回可读形式。"""
    raw = str(name).strip()
    if not raw:
        return ""
    return chest.resolve_entity(raw) or normalize_name(raw)


#: 境界/阶位序表（从低到高）。用于「已有更高境界却宣称更低」的能力违规检测。
REALM_LADDER: tuple[str, ...] = (
    # 斗气体系（斗破）
    "斗之气", "斗者", "斗师", "大斗师", "斗灵", "斗王", "斗皇", "斗宗", "斗尊", "斗圣", "斗帝",
    # 通用修真体系
    "炼气", "筑基", "结丹", "金丹", "元婴", "化神", "炼虚", "合体", "大乘", "渡劫",
    # 玄幻常见阶位
    "武者", "武师", "武灵", "武宗", "武尊", "武圣", "武帝",
)

#: 常见「层级修饰」后缀，剥掉后仍按基础境界比较（如「斗宗巅峰」≈「斗宗」）。
_REALM_SUFFIX = re.compile(
    r"(初期|中期|后期|巅峰|圆满|大成|小成|大圆满|初段|中段|高段|一段|二段|三段|四段|五段|"
    r"六段|七段|八段|九段|十段|\d+段|\d+重|\d+层|\d+级|[一二三四五六七八九十]+重)$"
)

#: 状态键别名：报告里可能用中文/其它拼写，统一到规范键名。
_STATE_KEYS: dict[str, str] = {
    "alive": "alive", "存活": "alive", "是否存活": "alive", "活着": "alive",
    "realm": "realm", "境界": "realm", "修为": "realm", "等级": "realm", "实力": "realm",
    "location": "location", "位置": "location", "所在地": "location", "地点": "location",
    "holder": "holder", "持有者": "holder", "归属": "holder", "拥有者": "holder",
    "status": "status", "状态": "status",
}

#: 报告里「这是硬事实状态」的字段名候选（写进 state 索引）。
_STATE_FIELDS: tuple[str, ...] = (
    "alive", "realm", "location", "holder", "status",
    "存活", "境界", "修为", "位置", "所在地", "持有者", "归属", "状态",
)


def _canon_state_key(key: Any) -> str:
    k = str(key).strip()
    return _STATE_KEYS.get(k, _STATE_KEYS.get(normalize_name(k), k))


#: 敬称/亲属称谓：出现在角色名后面时视为「未登记的新叫法」。
_ALIAS_SUFFIXES: tuple[str, ...] = (
    "大人", "前辈", "老人", "老", "爷", "阁下", "先生", "公子", "少爷", "少主",
    "宗主", "长老", "师兄", "师姐", "师弟", "师妹", "师父", "师尊", "老师",
    "哥哥", "姐姐", "弟弟", "妹妹", "兄长", "兄", "姐", "妹", "弟",
    "将军", "道友", "阁下大人",
)

#: 「角色名 + 敬称」的候选正则；按长度优先匹配长后缀（「大人」先于「大」）。
_ALIAS_SUFFIX_RE = "(" + "|".join(
    re.escape(s) for s in sorted(_ALIAS_SUFFIXES, key=len, reverse=True)
) + ")"


#: 结构化条目：既接受契约里的纯字符串，也接受带硬事实的 dict（超集，不收紧契约）。
_ENTRY_SCHEMA: dict[str, Any] = {
    "oneOf": [
        {"type": "string", "minLength": 1},
        {
            "type": "object",
            "required": ["name"],
            "properties": {
                "name": {"type": "string", "minLength": 1},
                "content": {"type": "string"},
                "summary": {"type": "string"},
                "chapter_id": {"type": "string"},
                "states": {"type": "object"},
                "state": {"type": "object"},
                "alive": {"type": ["boolean", "string"]},
                "realm": {"type": ["string", "number"]},
                "location": {"type": "string"},
                "holder": {"type": "string"},
                "status": {"type": "string"},
            },
            "additionalProperties": True,
        },
    ]
}


def _writeback_schema() -> dict[str, Any]:
    """§3.5 ``writeback_report`` 的**宽松超集**：条目允许字符串或结构化 dict。

    冻结的 ``common.schema("writeback_report")`` 把 characters/items/timeline 限定为
    纯字符串，无法承载「谁死了 / 什么境界 / 东西在谁手上」这类硬事实。本模块不改
    冻结层，而是放宽**自己接受**的输入面（字符串仍然合法），让回写能真正驱动
    ``set_state``。冻结 Schema 的其余约束（必填字段、伏笔 status 枚举）保持不变。
    """
    sch = copy.deepcopy(schema("writeback_report"))
    updated = sch["properties"]["updated"]["properties"]
    for key in ("characters", "items", "timeline", "world", "style"):
        if key in updated:
            updated[key] = {"type": "array", "items": _ENTRY_SCHEMA}
    return sch


#: 模块级缓存，避免每次回写都深拷贝 Schema
_REPORT_SCHEMA: dict[str, Any] | None = None


def _report_schema() -> dict[str, Any]:
    global _REPORT_SCHEMA
    if _REPORT_SCHEMA is None:
        _REPORT_SCHEMA = _writeback_schema()
    return _REPORT_SCHEMA


_REALM_SUFFIX_BONUS: dict[str, int] = {
    "初期": 0, "小成": 0, "初段": 0, "一段": 0, "二段": 0,
    "中期": 1, "大成": 1, "中段": 1,
    "后期": 2, "圆满": 2, "高段": 2,
    "巅峰": 3, "大圆满": 3,
}

#: 前缀量词/星阶，如「九星斗宗」「一星斗皇」「三星斗者」
_REALM_PREFIX = re.compile(r"^([一二三四五六七八九十百千\d]+)(星|级|阶|重|层|段)")


def _realm_rank(text: Any) -> tuple[int, int]:
    """境界文本 -> ``(基础档次, 修饰权重)``，可比较；无法识别返回 ``(-1, -1)``。

    「斗宗」「九星斗宗」「斗宗巅峰」都落在同一基础档次，用第二元区分强弱。
    无法识别的境界不参与比较（返回 -1），避免误报。
    """
    s = str(text or "").strip()
    if not s:
        return (-1, -1)
    base, _, _ = s.partition("·")

    bonus = 0
    while True:  # 逐层剥掉「巅峰」「大圆满」等修饰（可能叠加，如「斗宗后期巅峰」）
        m = _REALM_SUFFIX.search(base)
        if not m:
            break
        stripped = base[: m.start()]
        if not stripped:
            break
        bonus += _REALM_SUFFIX_BONUS.get(m.group(1), 1)
        base = stripped

    m = _REALM_PREFIX.match(base)
    if m:
        digits = m.group(1)
        n = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
             "十": 10, "百": 100, "千": 1000}.get(digits)
        if n is None:
            try:
                n = int(digits)
            except ValueError:
                n = 0
        bonus += n
        base = base[m.end():]

    if base in REALM_LADDER:
        return (REALM_LADDER.index(base), bonus)

    # 兜底：包含某档次名，取最长者（「斗之气」优先于「斗」）
    best: tuple[int, int] = (-1, -1)
    best_len = -1
    for i, name in enumerate(REALM_LADDER):
        if name in base and len(name) > best_len:
            best, best_len = (i, bonus), len(name)
    return best


# --------------------------------------------------------------------------
# 回写代理
# --------------------------------------------------------------------------
class WriteBackAgent:
    """把 ``writeback_report`` 落到末影箱，并做矛盾检测。

    用法::

        agent = WriteBackAgent(chest)
        result = agent.apply(report, chapter_id="ch_042")
        warnings = agent.detect_contradictions(report)

    ``apply`` 先在**任何写入之前**完成 Schema 校验与矛盾检测，因此非法报告
    不会留下半截写入（``SchemaError`` 在落盘前抛出）。
    """

    def __init__(self, chest: Any):
        self.chest = chest

    # -- 公开 API ------------------------------------------------------
    def apply(self, report: dict, *, chapter_id: str | None = None) -> dict[str, Any]:
        """校验并回写一份报告。

        :param report: §3.5 ``writeback_report`` 形态的 dict。
        :param chapter_id: 覆盖报告里的 ``chapter_id``（缺省用报告内的）。
        :returns: ``{"chapter_id","applied","warnings"}``
        :raises SchemaError: 报告不符合 Schema（**不做任何写入**）。
        """
        validate(report, _report_schema())
        cid = chapter_id or report.get("chapter_id") or ""
        if not cid:
            raise SchemaError("#/chapter_id: 报告未给出 chapter_id，且调用未传入 chapter_id")

        updated: Mapping[str, Any] = report.get("updated") or {}

        applied: list[str] = []
        warnings: list[str] = [str(w) for w in report.get("warnings") or []]

        # 1) 正文/摘要（先写，保证时间轴上「本章存在」）
        if report.get("text") is not None or report.get("summary") is not None:
            self.chest.put_chapter(
                cid,
                str(report.get("text") or ""),
                title=str(report.get("title") or ""),
                summary=str(report.get("summary") or ""),
            )
            applied.append(f"chapter:{cid}")

        # 2) 人物 / 物品 / 时间 / 世界 / 风格 —— 记录 + 状态索引
        for layer, key in (
            ("character", "characters"),
            ("item", "items"),
            ("timeline", "timeline"),
            ("world", "world"),
            ("style", "style"),
        ):
            for entry in updated.get(key) or []:
                applied.extend(self._apply_entry(layer, entry, cid))

        # 3) 伏笔状态机
        for entry in updated.get("foreshadowing") or []:
            applied.extend(self._apply_foreshadow(entry, cid))

        self._commit()
        return {"chapter_id": cid, "applied": applied, "warnings": warnings}

    def detect_contradictions(
        self, report: dict, *, chapter_id: str | None = None
    ) -> list[str]:
        """四类矛盾检测，返回人类可读中文 warning（不阻断回写）。

        报告先过 Schema 校验（非法报告直接抛 ``SchemaError``，避免拿脏数据检测）。
        顺序固定：死而复生 → 时间倒错 → 能力/规则违规 → 称呼漂移。
        """
        validate(report, _report_schema())
        cid = chapter_id or report.get("chapter_id") or ""
        updated: Mapping[str, Any] = report.get("updated") or {}
        found: list[str] = []

        names = self._report_entities(report, updated)
        found.extend(self._check_resurrection(report, updated, names, cid))
        found.extend(self._check_time_inversion(updated, cid))
        found.extend(self._check_rule_violation(updated, cid))
        found.extend(self._check_alias_drift(report, names, cid))
        return dedupe(found)

    # -- 内部：写入 ----------------------------------------------------
    def _commit(self) -> None:
        commit = getattr(self.chest, "commit", None)
        if callable(commit):
            commit()

    def _apply_entry(self, layer: str, entry: Any, cid: str) -> list[str]:
        """写一条 ``characters`` / ``items`` / ``timeline`` … 条目。

        条目可以是 ``"萧炎"`` 这样的字符串，也可以是带 ``name``/``content``/
        ``states`` 的 dict。状态字段统一进 ``set_state`` 状态索引。
        """
        if isinstance(entry, str):
            name, content, items = entry.strip(), "", {}
        elif isinstance(entry, Mapping):
            name = str(entry.get("name") or entry.get("entity") or entry.get("id") or "").strip()
            content = str(
                entry.get("content") or entry.get("summary") or entry.get("description") or ""
            )
            items = entry
        else:
            log.warning("忽略无法识别的 %s 条目：%r", layer, entry)
            return []

        if not name:
            log.warning("忽略缺少名称的 %s 条目：%r", layer, entry)
            return []

        entity = name if layer == "timeline" else _norm_entity(self.chest, name)
        entity = entity or name

        prev = self.chest.get(layer, entity)
        data = dict(getattr(prev, "data", None) or {})
        text = content or (prev.content if prev else "")
        if not text:
            # 只给了名字：用已有内容兜底，再不行就给一句可读占位，保证记录可检索
            text = content or f"{LAYER_CN.get(layer, layer)}：{name}"
        if layer == "timeline" and not re.search(r"ch_\d+", entity):
            text = f"{cid}：{text}" if text else cid

        extra = {
            k: v for k, v in items.items() if k not in ("states", "state", "name", "entity", "content")
        }
        if extra:
            data.update(extra)
        self.chest.upsert(layer, entity, text, data=data or None, chapter_id=cid)

        applied = [f"{layer}:{entity}"]

        states = items.get("states") or items.get("state") or {}
        if isinstance(states, Mapping):
            for k, v in states.items():
                self.chest.set_state(entity, _canon_state_key(k), v, chapter_id=cid)
                applied.append(f"state:{entity}.{_canon_state_key(k)}")
        # 顶层硬事实字段（alive / realm / location / holder / status）
        for field in _STATE_FIELDS:
            if field in items:
                key = _canon_state_key(field)
                self.chest.set_state(entity, key, items[field], chapter_id=cid)
                applied.append(f"state:{entity}.{key}")
        return applied

    def _ensure_foreshadow(self, fs_id: str, entry: Mapping[str, Any], cid: str) -> Foreshadow:
        """伏笔不存在时按报告内容先落地（planted），返回最新记录。"""
        cur = self.chest.get("foreshadowing", fs_id)
        if cur is not None:
            return self._foreshadow_of(fs_id)
        content = str(entry.get("content") or "").strip()
        if not content:
            raise KeyError(f"伏笔 {fs_id} 不存在，且报告未给出 content，无法新建")
        planted = entry.get("planted_chapter") or cid
        planned = entry.get("planned_resolve") or None
        log.info("报告引用了未登记的伏笔 %s，按 planted 新建", fs_id)
        self.chest.set_foreshadow(
            fs_id,
            content,
            status="planted",
            planted_chapter=planted,
            planned_resolve=planned,
            chapter_id=planted,
            extra={
                "status": "planted",
                "planted_chapter": planted,
                "planned_resolve": planned,
                "reinforced_chapters": [],
                "resolved_chapter": None,
            },
        )
        return self._foreshadow_of(fs_id)

    def _foreshadow_of(self, fs_id: str) -> Foreshadow:
        """读回落盘后的伏笔（保证与库内一致，不依赖写接口的返回值）。"""
        for fs in self.chest.all_foreshadowings():
            if fs.id == fs_id:
                return fs
        rec = self.chest.get("foreshadowing", fs_id)
        if rec is None:
            raise KeyError(f"伏笔不存在：{fs_id}")
        d = dict(rec.data or {})
        return Foreshadow(
            id=fs_id,
            content=rec.content,
            status=d.get("status", "planted"),
            planted_chapter=d.get("planted_chapter"),
            planned_resolve=d.get("planned_resolve"),
            reinforced_chapters=list(d.get("reinforced_chapters", [])),
            resolved_chapter=d.get("resolved_chapter"),
            data=d,
        )

    def _apply_foreshadow(self, entry: Mapping[str, Any], cid: str) -> list[str]:
        """按 ``status`` 驱动伏笔状态机。

        每条最终都用 ``set_foreshadow(extra=<权威值>)`` 落盘，规避冻结层
        ``data.update(extra)`` 覆盖 ``reinforced_chapters`` / ``resolved_chapter``
        的问题（见模块文档）。
        """
        fs_id = str(entry.get("id") or "").strip()
        status = str(entry.get("status") or "planted")
        if not fs_id:
            log.warning("忽略缺少 id 的伏笔条目：%r", entry)
            return []

        cur = self._ensure_foreshadow(fs_id, entry, cid)
        note = str(entry.get("content") or entry.get("note") or "").strip()
        content = str(entry.get("content") or "").strip() or cur.content

        planted = entry.get("planted_chapter") or cur.planted_chapter or cid
        planned = entry.get("planned_resolve") or cur.planned_resolve
        reinforced = list(cur.reinforced_chapters)
        resolved = cur.resolved_chapter
        more: dict[str, Any] = {}

        if status == "planted":
            # 已登记的伏笔再报 planted：视为一次提及（强化），避免状态机倒退
            if cur.status in ("reinforced", "resolved", "abandoned"):
                status = "reinforced"
                if cid not in reinforced:
                    reinforced.append(cid)
        elif status == "reinforced":
            if cid not in reinforced:
                reinforced.append(cid)
            if note:
                more["last_note"] = note
        elif status == "resolved":
            resolved = cid
            more["resolve_note"] = note or cur.data.get("resolve_note", "")
            if not more["resolve_note"]:
                more.pop("resolve_note")
        elif status == "abandoned":
            more["abandon_reason"] = note or cur.data.get("abandon_reason", "回写报告判定废弃")
        else:  # pragma: no cover - Schema 已挡住
            raise ValueError(f"非法伏笔状态：{status!r}")

        self.chest.set_foreshadow(
            fs_id,
            content,
            status=status,
            planted_chapter=planted,
            planned_resolve=planned,
            reinforced_chapters=reinforced,
            resolved_chapter=resolved,
            chapter_id=cid,
            extra=_foreshadow_extra(
                cur,
                status=status,
                planted_chapter=planted,
                planned_resolve=planned,
                reinforced_chapters=reinforced,
                resolved_chapter=resolved,
                **more,
            ),
        )
        return [f"foreshadowing:{fs_id}#{status}"]

    # -- 内部：矛盾检测 --------------------------------------------------
    @staticmethod
    def _report_entities(report: Mapping[str, Any], updated: Mapping[str, Any]) -> list[str]:
        """本章涉及的人物名（``updated.characters`` + 可选 ``active_characters``）。"""
        names: list[str] = []
        for entry in updated.get("characters") or []:
            if isinstance(entry, str):
                names.append(entry.strip())
            elif isinstance(entry, Mapping):
                names.append(str(entry.get("name") or entry.get("entity") or "").strip())
        for entry in report.get("active_characters") or []:
            if isinstance(entry, str):
                names.append(entry.strip())
            elif isinstance(entry, Mapping):
                names.append(str(entry.get("name") or entry.get("entity") or "").strip())
        return [n for n in dedupe(names) if n]

    def _check_resurrection(
        self,
        report: Mapping[str, Any],
        updated: Mapping[str, Any],
        names: Sequence[str],
        cid: str,
    ) -> list[str]:
        """a) 死而复生：库里 alive 为 False，本章却作为活跃人物出场。"""
        out: list[str] = []
        for name in names:
            canonical = self.chest.resolve_entity(name) or name
            alive = self.chest.get_state(canonical, "alive")
            if _as_bool(alive) is not False:
                continue
            if self._resurrection_explained(updated, canonical, cid):
                continue
            last = self.chest.state_chapter(canonical, "alive") or "未知章节"
            out.append(
                f"死而复生：{name} 在 {last} 已被记为死亡（alive=false），"
                f"却在本章 {cid} 以活跃人物出现；若为复活/假死，请在报告中显式说明"
            )
        return out

    def _resurrection_explained(
        self, updated: Mapping[str, Any], canonical: str, cid: str
    ) -> bool:
        """报告显式声明「复活/假死/复活归来」时，不视为矛盾。"""
        markers = ("复活", "假死", "复生", "归来", "诈死", "重生", "死而复生", "借尸还魂")
        for entry in updated.get("characters") or []:
            if isinstance(entry, str):
                target, text = entry, ""
            elif isinstance(entry, Mapping):
                target = str(entry.get("name") or entry.get("entity") or "")
                text = str(entry.get("content") or entry.get("summary") or "")
                if entry.get("alive") is True or entry.get("revived") is True:
                    return True
            else:
                continue
            if self.chest.resolve_entity(target) != canonical:
                continue
            if any(m in text for m in markers):
                return True
        return False

    def _check_time_inversion(
        self, updated: Mapping[str, Any], cid: str
    ) -> list[str]:
        """b) 时间倒错：新事件的 chapter_index 早于实际落点，或早于该实体最后更新章。"""
        out: list[str] = []
        chapter_no = chapter_index(cid)

        for entry in updated.get("timeline") or []:
            if isinstance(entry, str):
                ev_id, ev_chapter, text = entry, None, entry
            elif isinstance(entry, Mapping):
                ev_id = str(entry.get("name") or entry.get("id") or entry.get("event") or "")
                ev_chapter = entry.get("chapter_id") or entry.get("chapter") or entry.get("when")
                text = str(entry.get("content") or ev_chapter or ev_id)
            else:
                continue
            ev_no = chapter_index(str(ev_chapter)) if ev_chapter else -1
            if ev_no >= 0 and chapter_no >= 0 and ev_no < chapter_no:
                out.append(
                    f"时间倒错：时间线事件「{text}」标注发生在第 {ev_no} 章，"
                    f"却由第 {chapter_no} 章（{cid}）回写；若属倒叙请显式标注倒叙"
                )
            # 现有事件被改到更早的章节
            if ev_id:
                rec = self.chest.get("timeline", ev_id)
                if rec is not None and rec.chapter_no >= 0 and chapter_no >= 0 and rec.chapter_no > chapter_no:
                    out.append(
                        f"时间倒错：时间线事件「{ev_id}」已登记在第 {rec.chapter_no} 章，"
                        f"本章 {cid}（第 {chapter_no} 章）的回写早于其最后更新章"
                    )

        for entry in updated.get("characters") or []:
            if not isinstance(entry, Mapping):
                continue
            name = str(entry.get("name") or entry.get("entity") or "").strip()
            if not name:
                continue
            canonical = self.chest.resolve_entity(name)
            # 取该角色**所有**状态键里最晚的一次更新章（任一键都比本章晚即为倒错）
            last_ch = ""
            last_no = -1
            for key in self.chest.states_of(canonical):
                ch = self.chest.state_chapter(canonical, key)
                no = chapter_index(ch)
                if no > last_no:
                    last_ch, last_no = ch, no
            if last_no < 0 or chapter_no < 0 or last_no <= chapter_no:
                continue
            states = entry.get("states") or entry.get("state") or {}
            claims_state = bool(states) or any(f in entry for f in _STATE_FIELDS)
            if claims_state:
                out.append(
                    f"时间倒错：{name} 的状态最后更新于第 {last_no} 章（{last_ch}），"
                    f"本章 {cid}（第 {chapter_no} 章）试图改写其状态"
                )
        return out

    def _check_rule_violation(self, updated: Mapping[str, Any], cid: str) -> list[str]:
        """c) 能力/规则违规：报告声称的状态与库内硬事实冲突（如境界回退）。"""
        out: list[str] = []
        for entry in updated.get("characters") or []:
            if not isinstance(entry, Mapping):
                continue
            name = str(entry.get("name") or entry.get("entity") or "").strip()
            if not name:
                continue
            canonical = self.chest.resolve_entity(name)
            claims: dict[str, Any] = dict(entry.get("states") or entry.get("state") or {})
            for field in _STATE_FIELDS:
                if field in entry:
                    claims[_canon_state_key(field)] = entry[field]

            for raw_key, value in claims.items():
                key = _canon_state_key(raw_key)
                known = self.chest.get_state(canonical, key)
                if known is None:
                    continue
                if key == "realm":
                    old_rank, new_rank = _realm_rank(known), _realm_rank(value)
                    if old_rank[0] < 0 or new_rank[0] < 0:
                        continue
                    if new_rank < old_rank:
                        out.append(
                            f"能力违规：{name} 的境界由已记录的「{_as_text(known)}」"
                            f"回退为「{_as_text(value)}」，与既成剧情冲突"
                        )
                elif key == "alive":
                    old_b, new_b = _as_bool(known), _as_bool(value)
                    if old_b is not None and new_b is not None and old_b != new_b and old_b:
                        out.append(
                            f"规则违规：{name} 已记录为存活（alive=true），"
                            f"本章 {cid} 却声称其 alive={_as_text(value)}"
                        )
        return out

    def _check_alias_drift(
        self, report: Mapping[str, Any], names: Sequence[str], cid: str
    ) -> list[str]:
        """d) 称呼漂移：正文里出现「角色名 + 敬称/亲属称谓」但未在称呼格登记。

        用 ``chest.synonyms`` 拿到该角色的**已登记**称呼集合，再用正则扫正文：
        ``已知称呼 + 敬称后缀``（「萧炎哥哥」「炎帝大人」）命中即算漂移——
        这类新叫法 ``resolve_entity`` 必然查不到，不能等它进了库里才检测。
        已登记的合法叫法本身会被排除，因此不会误报。
        """
        text = str(report.get("text") or "")
        if not text:
            return []

        known: dict[str, str] = {}
        for name in names:
            canonical = self.chest.resolve_entity(name) or normalize_name(name)
            known.setdefault(canonical, canonical)
            for syn in self.chest.synonyms(canonical):
                known.setdefault(syn, canonical)
        if not known:
            return []

        out: list[str] = []
        hay = unicodedata.normalize("NFKC", text)
        for alias in sorted(known, key=len, reverse=True):  # 长词优先，避免子串重复报
            pattern = re.compile(re.escape(alias) + _ALIAS_SUFFIX_RE)
            for m in pattern.finditer(hay):
                token = m.group(0)
                canonical = known[alias]
                out.append(
                    f"称呼漂移：第 {cid} 章正文出现「{token}」，疑似「{alias}」的未登记别称"
                    f"（标准名 {canonical}）；建议 link_alias('{token}', '{canonical}') 或统一写法"
                )
        return out


# --------------------------------------------------------------------------
# 伏笔巡检
# --------------------------------------------------------------------------
_SEVERITY_ORDER: dict[str, int] = {
    "overdue": SEVERITY_OVERDUE,
    "stale": SEVERITY_STALE,
    "unresolved": SEVERITY_UNRESOLVED,
    "unplanned": SEVERITY_UNPLANNED,
}


def patrol(
    chest: Any,
    current_chapter: str | int,
    *,
    grace: int = 3,
    stale_threshold: int = 30,
) -> list[dict[str, Any]]:
    """伏笔巡检，返回按严重度（overdue → stale → unresolved）排序的条目。

    * **overdue**：``current_chapter > planned_resolve + grace``。附带 ``suggestion``
      ——可直接注入下一章细纲的回收建议。
    * **stale**：``status == "planted"`` 且 ``current - planted_chapter > stale_threshold``
      且**从未强化**；此时把 ``Foreshadow.data["abandon_candidate"]`` 置 ``True``
      （**不改 status**，是否废弃由人/上层决定）。
    * **unresolved**：status 已是 resolved/abandoned 却仍挂在 ``foreshadowing_to_resolve``
      里（仍被当作待处理伏笔）。

    边界一律「严格大于」：``current == planned + grace`` 不报，``+1`` 才报；
    陈旧同理（``current - planted == stale_threshold`` 不报）。

    :returns: ``[{"type","id","content","severity",...}]``，全部 JSON 可序列化。
    """
    cur_no = chapter_index(current_chapter)
    if cur_no < 0:
        raise ValueError(f"无法解析章节序号：{current_chapter!r}")

    items: list[dict[str, Any]] = []
    open_ids: set[str] = set()

    for fs in sorted(chest.all_foreshadowings(), key=lambda f: f.id):
        if fs.is_open:
            open_ids.add(fs.id)

        data = dict(fs.data or {})
        planned_no = fs.planned_no
        planted_no = fs.planted_no
        # 强化历史：优先用 data 里的口径；冻结层的 reinforce_foreshadow 直调用会把它
        # 读回成空列表，因此再回退到只追加的 history 表（version>1 即被改过）。
        reinforced = list(fs.reinforced_chapters or [])
        had_later_version = bool(chest.history("foreshadowing", fs.id)[1:])

        # 1) 超期未回收
        if fs.is_open and planned_no >= 0:
            if cur_no > planned_no + int(grace):
                items.append(
                    {
                        "type": "overdue",
                        "id": fs.id,
                        "content": fs.content,
                        "severity": SEVERITY_OVERDUE,
                        "status": fs.status,
                        "planned_resolve": fs.planned_resolve,
                        "overdue_by": cur_no - planned_no,
                        "suggestion": _overdue_suggestion(fs, cur_no, planned_no),
                    }
                )
        elif fs.status == "resolved":
            # 2) 已解决，却仍被当作待处理伏笔挂账
            if (data.get("pending_resolve") or data.get("to_resolve")
                    or data.get("in_foreshadowing_to_resolve")):
                items.append(
                    {
                        "type": "unresolved",
                        "id": fs.id,
                        "content": fs.content,
                        "severity": SEVERITY_UNRESOLVED,
                        "status": fs.status,
                        "resolved_chapter": fs.resolved_chapter,
                        "suggestion": (
                            f"伏笔 {fs.id} 已在 {fs.resolved_chapter or '某章'} 回收，"
                            f"请从待处理伏笔列表 foreshadowing_to_resolve 中移除，"
                            f"避免重复回收或占用 P0 预算"
                        ),
                    }
                )

        # 3) 陈旧：planted 且长期未强化
        if (
            fs.status == "planted"
            and planted_no >= 0
            and cur_no - planted_no > int(stale_threshold)
            and not reinforced
            and not had_later_version
        ):
            items.append(
                {
                    "type": "stale",
                    "id": fs.id,
                    "content": fs.content,
                    "severity": SEVERITY_STALE,
                    "status": fs.status,
                    "planted_chapter": fs.planted_chapter,
                    "stale_chapters": cur_no - planted_no,
                    "suggestion": (
                        f"伏笔 {fs.id} 自 {fs.planted_chapter} 埋下后已 {cur_no - planted_no} 章"
                        f"未被强化，建议近期强化一次或标记废弃（abandon_candidate=true）"
                    ),
                }
            )
            _mark_abandon_candidate(chest, fs)

        # 4) 未定回收计划：开着的伏笔没有 planned_resolve，巡检永远算不出超期。
        #    这不是写作出错，而是**伏笔无法被看管**——把它显式报出来，
        #    否则巡检会一直返回空，让人误以为"没有超期伏笔"。
        if fs.is_open and planned_no < 0:
            items.append(
                {
                    "type": "unplanned",
                    "id": fs.id,
                    "content": fs.content,
                    "severity": SEVERITY_UNPLANNED,
                    "status": fs.status,
                    "planted_chapter": fs.planted_chapter,
                    "planned_resolve": None,
                    "suggestion": (
                        f"伏笔 {fs.id} 没有设定回收章节，巡检无法判断是否超期。"
                        f"补法：用 enderwriter_remember 写入时带上 "
                        f"data.planned_resolve=\"ch_0XX\"，"
                        f"或调用 enderwriter_foreshadow 补计划。"
                    ),
                }
            )

    items.sort(key=lambda d: (_SEVERITY_ORDER.get(str(d["type"]), 9), str(d["id"])))
    return items


def _mark_abandon_candidate(chest: Any, fs: Foreshadow) -> None:
    """把陈旧伏笔标为废弃候选：只写 ``data.abandon_candidate``，**不动 status**。"""
    data = dict(fs.data or {})
    if data.get("abandon_candidate") is True:
        return
    data["abandon_candidate"] = True
    chest.set_foreshadow(
        fs.id,
        fs.content,
        status=fs.status,
        planted_chapter=fs.planted_chapter,
        planned_resolve=fs.planned_resolve,
        reinforced_chapters=list(fs.reinforced_chapters),
        resolved_chapter=fs.resolved_chapter,
        chapter_id=fs.planted_chapter,
        extra=_foreshadow_extra(
            fs,
            status=fs.status,
            planted_chapter=fs.planted_chapter,
            planned_resolve=fs.planned_resolve,
            reinforced_chapters=list(fs.reinforced_chapters),
            resolved_chapter=fs.resolved_chapter,
            abandon_candidate=True,
        ),
    )


def _overdue_suggestion(fs: Foreshadow, cur_no: int, planned_no: int) -> str:
    """生成可直接注入下一章细纲的回收建议。"""
    late = cur_no - planned_no
    next_ch = cur_no + 1
    hint = fs.content.strip() or "（无内容）"
    if len(hint) > 60:
        hint = hint[:60] + "…"
    times = len(fs.reinforced_chapters or [])
    extra = f"，已强化 {times} 次" if times else "，尚未强化"
    return (
        f"下一章（第 {next_ch} 章）请安排回收伏笔 {fs.id}：{hint}"
        f"（原计划 {fs.planned_resolve} 回收，现已超期 {late} 章{extra}）。"
        f"建议在细纲的 foreshadowing_to_resolve 中加入 {fs.id}，并确保本章正文给出明确兑现。"
    )



