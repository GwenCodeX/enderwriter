"""编排层（Orchestrator）：P0-P4 上下文组装 + 动态异步预取 + 工具调用。

对应规范第四节：
  * 章节上下文总量 = 模型窗口的 70%；
  * 段内预算：P0 30% / P1 30% / P2 20% / P3 15% / P4 5%（占上述总量的比例）；
  * 单次 ``search_memory`` ≤ 800 token，层级 high 50% / medium 35% / low 15%。

设计约束（来自冻结契约 INTERFACES.md）：
  * 只依赖 ``common`` / ``chest.store`` / ``lexicon`` 的公开签名；
  * ``retrieval.py`` 由另一组件并行开发，因此对 ``MemoryKeeper`` / ``search_memory``
    做**惰性软依赖**：能导入就用真实现，导入不到或调用抛错时退化到内部的
    :class:`_FallbackKeeper`（直接调 ``chest.search`` + 分档），保证本模块的
    组装逻辑任何时候都能跑通。

硬保证：
  * 每个优先级段用 :func:`common.clip_tokens` 严格截断到该段预算；
  * ``total_tokens = est_tokens(最终 text)``，且断言 ``total_tokens <= int(window*0.7)``；
  * 任何单段超预算都会被 ``dropped`` 记录，绝不静默丢弃。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Iterable, Sequence

from . import common as C
from . import lexicon as L
from .chest.store import EnderChest, Hit, Record
from .common import get_logger

__all__ = ["Orchestrator", "PRIORITIES", "BUDGET_RATIOS", "CONTEXT_RATIO"]

log = get_logger("orchestrator")

# --------------------------------------------------------------------------
# 预算常量（严格照规范 §4）
# --------------------------------------------------------------------------
#: 五个优先级段
PRIORITIES: tuple[str, ...] = ("P0", "P1", "P2", "P3", "P4")

#: 段内预算占「章节上下文总量」的比例
BUDGET_RATIOS: dict[str, float] = {
    "P0": 0.30,
    "P1": 0.30,
    "P2": 0.20,
    "P3": 0.15,
    "P4": 0.05,
}

#: 章节上下文总量 / 模型窗口
CONTEXT_RATIO: float = 0.70

#: 与规范同名的默认值（便于外部对齐）
WINDOW_TOKENS_DEFAULT = 8000
SEARCH_TOOL_BUDGET_DEFAULT = 800
SEARCH_BAND_RATIOS: dict[str, float] = {"high": 0.50, "medium": 0.35, "low": 0.15}

#: 段标题（拼 prompt 时的分隔符）
_SECTION_TITLES: dict[str, str] = {
    "P0": "【P0 核心设定：大纲 / 细纲 / 核心人物 / 待处理伏笔】",
    "P1": "【P1 高相关记忆】",
    "P2": "【P2 近期正文】",
    "P3": "【P3 次要设定】",
    "P4": "【P4 风格样本与探索】",
}

#: 条目编号前缀（P0 用字母序号，其余用数字）
_ITEM_LABELS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"

#: 实体卡的格子优先级（人物卡压缩时保留 order）
_ENTITY_LAYER_ORDER: tuple[str, ...] = (
    "character",
    "world",
    "item",
    "timeline",
    "foreshadowing",
    "style",
)


# --------------------------------------------------------------------------
# 软依赖：retrieval.MemoryKeeper / search_memory
# --------------------------------------------------------------------------
try:  # pragma: no cover - 取决于并行组件的落盘时机
    from .retrieval import MemoryKeeper as _RealMemoryKeeper  # type: ignore
    from .retrieval import search_memory as _real_search_memory  # type: ignore

    HAS_RETRIEVAL = True
    _RETRIEVAL_IMPORT_ERROR = ""
except Exception as exc:  # pragma: no cover - 正常情况下就是这条分支
    _RealMemoryKeeper = None  # type: ignore[assignment]
    _real_search_memory = None  # type: ignore[assignment]
    HAS_RETRIEVAL = False
    _RETRIEVAL_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"


def retrieval_available() -> bool:
    """``retrieval.py`` 是否可用（软依赖探测结果，便于测试/诊断）。"""
    return HAS_RETRIEVAL


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------
def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set, frozenset)):
        return list(value)
    return [value]


def _name_of(item: Any) -> str:
    """条目 -> 名称（字符串或章节细纲里的 {"name": ...} 形态）。"""
    if item is None:
        return ""
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        for key in ("name", "entity", "id", "title"):
            v = item.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return ""


def _first_paragraph(text: str, *, limit: int = 60) -> str:
    """取首个非空段落的前 limit 字（P2 无正文时作为关键句）。"""
    for line in str(text or "").splitlines():
        line = line.strip()
        if line:
            return line[:limit]
    return str(text or "").strip()[:limit]


def _match_entity(name: str, text: str) -> bool:
    """实体名是否出现在文本里（大小写/空白不敏感，且用 NFKC 归一）。"""
    if not name or not text:
        return False
    a = C.normalize_name(name)
    b = C.normalize_name(text)
    return bool(a) and a in b


def _entry(label: str, fields: dict[str, str]) -> str:
    """把若干字段压成一条紧凑条目。"""
    parts = [f"{k}={v}" for k, v in fields.items() if v not in (None, "")]
    return f"· {label}: " + "；".join(parts)


def _entry_parts(entry: str) -> tuple[str, list[tuple[str, str]]]:
    """``_entry`` 的逆操作：拆分出标签与有序字段。"""
    label, sep, body = entry.partition(": ")
    label = label.replace("· ", "", 1).strip()
    items: list[tuple[str, str]] = []
    if not sep:
        return label, items
    for piece in body.split("；"):
        k, s2, v = piece.partition("=")
        if s2:
            items.append((k, v))
    return label, items


def _rebuild_entry(label: str, fields: Sequence[tuple[str, str]]) -> str:
    return _entry(label, {k: v for k, v in fields if v})


def _entry_parts_with_splits(unit: str) -> list[str]:
    """把「超宽条目」拆成一串可独立保留的片段（保持可读的前缀链）。

    ``· 萧炎: 人物卡=…；状态=…`` -> ``["· 萧炎", "· 萧炎: 人物卡=…", "· 萧炎: 人物卡=…；状态=…"]``
    组合截断时取能装下的最长前缀，信息量尽量不丢。
    """
    label, fields = _entry_parts(unit)
    if not label or not fields:
        return []
    frags: list[str] = [f"· {label}"]
    grown: list[tuple[str, str]] = []
    for k, v in fields:
        grown.append((k, v))
        frags.append(_rebuild_entry(label, grown))
    return frags


def _rank_dropped(key: str) -> tuple[int, str]:
    """裁剪时从**尾部**丢弃：P0 条目字母序越靠后越先丢，其它段数字序号越大越先丢。"""
    label = key.split(":", 1)[-1]
    if label[:1] in _ITEM_LABELS and label[:1].isalpha() and len(label) == 1:
        return (ord(label), key)
    return (label.zfill(6), key)


# --------------------------------------------------------------------------
# 软依赖：内部极简 fallback 检索（仅在 retrieval.py 不可用时使用）
# --------------------------------------------------------------------------
def _record_to_hit(rec: Record, score: float, band: str, channels: Sequence[str]) -> Hit:
    return Hit(
        layer=rec.layer,
        entity=rec.entity,
        content=rec.content,
        version=int(rec.version),
        last_updated=rec.last_updated,
        confidence=max(0.0, min(1.0, float(score))),
        band=band,
        in_context=False,
        data=dict(rec.data),
        channels=tuple(channels),
    )


def _banded_search(chest: EnderChest, req: dict, *, context_entities: Sequence[str] = ()) -> dict:
    """极简检索：``chest.search`` 三路合并 + 按分数分 high/medium/low 三档。

    只在 ``retrieval.py`` 未就绪时兜底；预算切分遵循 high 50% / medium 35% / low 15%。
    """
    req = req if isinstance(req, dict) else {}
    entities = [e for e in _as_list(req.get("entities")) if isinstance(e, str) and e]
    layers = req.get("layers") or None
    budget = int(req.get("budget", SEARCH_TOOL_BUDGET_DEFAULT) or SEARCH_TOOL_BUDGET_DEFAULT)
    query = str(req.get("query_context") or "")
    if not isinstance(layers, (list, tuple)):
        layers = None

    query_text = " ".join([query, *entities]).strip() or " ".join(entities)
    pairs: list[tuple[Record, float]] = []
    if query_text:
        pairs = chest.search(query_text, entities=entities, layers=layers, limit=50)

    # 归一化到 0~1 再分档，避免不同通道分数量纲差异把 everything 压进 low
    top = max((float(s) for _r, s in pairs), default=0.0) or 1.0
    bands: dict[str, list[Hit]] = {"high": [], "medium": [], "low": []}
    ctx = {C.normalize_name(e) for e in context_entities if e}
    for rec, score in pairs:
        ratio = float(score) / top
        band = "high" if ratio >= 0.55 else ("medium" if ratio >= 0.30 else "low")
        hit = _record_to_hit(rec, ratio, band, ("terms", "vector", "graph"))
        hit.in_context = C.normalize_name(rec.entity) in ctx
        bands[band].append(hit)

    result: dict[str, Any] = {"high": [], "medium": [], "low": [], "total_tokens": 0}
    used = 0
    trace: list[str] = [f"fallback: entities={entities} pairs={len(pairs)} budget={budget}"]
    for band, share in SEARCH_BAND_RATIOS.items():
        allow = int(budget * share)
        kept: list[dict] = []
        for hit in bands[band]:
            d = hit.to_dict()
            cost = C.est_tokens(d.get("content", ""))
            if cost > allow - used:
                trace.append(f"drop {band}/{hit.entity} (cost {cost} > left {allow - used})")
                continue
            used += cost
            kept.append(d)
        result[band] = kept
    result["total_tokens"] = used
    result["elapsed_ms"] = 0.0
    result["normalized"] = {"entities": entities, "query": query, "layers": layers or []}
    result["trace"] = trace
    return result


class _FallbackKeeper:
    """``MemoryKeeper`` 的最小可用替身：只提供 ``search``（签名对齐契约）。"""

    def __init__(self, chest: EnderChest) -> None:
        self.chest = chest

    def search(self, req: dict, *, context_entities: Sequence[str] = ()) -> dict:
        local = getattr(req, "to_dict", None)
        payload = local() if callable(local) else req
        return _banded_search(self.chest, payload, context_entities=context_entities)

    # 兼容性签名：允许 (chest, req) 或 (req) 两种调用
    def __call__(self, *args: Any, **kwargs: Any) -> dict:
        if len(args) >= 2:
            args = args[1:]
        return self.search(*args, **kwargs)


# --------------------------------------------------------------------------
# 检索结果归一化 / 预取键
# --------------------------------------------------------------------------
_HIT_KEYS = ("layer", "entity", "content", "version", "last_updated", "confidence")


def _hit_to_dict(hit: Any) -> dict[str, Any]:
    """把 Hit / dict / Record / (record, score) 统一成 memory_result 的 hit dict。"""
    if isinstance(hit, (tuple, list)) and len(hit) == 2:
        rec, score = hit
        hit = _record_to_hit(rec, score, "high", ()) if isinstance(rec, Record) else rec
    d: dict[str, Any] = {}
    if isinstance(hit, Hit):
        d = hit.to_dict()
        d["band"] = hit.band
        if hit.channels:
            d["channels"] = list(hit.channels)
    elif isinstance(hit, Record):
        d = hit.to_hit().to_dict()
        d["band"] = "high"
    elif isinstance(hit, dict):
        d = dict(hit)
    else:
        d = {
            "layer": getattr(hit, "layer", ""),
            "entity": getattr(hit, "entity", ""),
            "content": getattr(hit, "content", ""),
            "version": getattr(hit, "version", 1),
            "last_updated": getattr(hit, "last_updated", ""),
            "confidence": getattr(hit, "confidence", 0.0),
        }
    for key in ("version", "confidence"):
        try:
            d.setdefault(key, 1 if key == "version" else 0.0)
            d[key] = int(d[key]) if key == "version" else float(d[key])
        except (TypeError, ValueError):
            d[key] = 1 if key == "version" else 0.0
    d.setdefault("last_updated", "")
    d.setdefault("content", "")
    d.pop("data", None)
    d.pop("channels", None)
    return d


def _normalize_result(result: Any) -> dict[str, Any]:
    """把任意检索返回值归一化成 ``memory_result`` 形状（额外的字段原样保留）。"""
    if not isinstance(result, dict):
        result = {}
    out = {k: v for k, v in result.items()}
    hits: list[dict] = []
    for band in ("high", "medium", "low"):
        arr = result.get(band) or []
        if not isinstance(arr, (list, tuple)):
            arr = [arr]
        norm = []
        for h in arr:
            d = _hit_to_dict(h)
            d["band"] = band
            norm.append(d)
            hits.append(d)
        out[band] = norm
    out["hits"] = hits
    out.setdefault("normalized", {})
    out.setdefault("trace", [])
    out.setdefault("elapsed_ms", 0.0)
    total = 0
    for d in hits:
        total += C.est_tokens(str(d.get("content", "")))
    existing = result.get("total_tokens")
    out["total_tokens"] = int(existing) if isinstance(existing, int) else total
    return out


def _hit_key(hit: dict) -> str:
    return f"{hit.get('layer', '')}|{C.normalize_name(str(hit.get('entity', '')))}"


def _prefetch_key(entities: Sequence[str], window: int) -> str:
    return f"{window}:" + ",".join(C.dedupe([C.normalize_name(e) for e in entities if e]))


def _prefetch_list(item: dict) -> list[dict]:
    hits: list[dict] = []
    for band in ("high", "medium", "low"):
        hits.extend(item.get(band, []) or [])
    return hits


def _is_core(item: dict[str, Any]) -> bool:
    """细纲条目是否 core（人物卡 / 物品卡）。"""
    if not isinstance(item, dict):
        return False
    imp = item.get("importance")
    if imp is None:
        imp = item.get("role") or item.get("type") or ""
    return str(imp).strip().lower() == "core"


# --------------------------------------------------------------------------
# 条目渲染（不可再分的最小单元）
# --------------------------------------------------------------------------
def _character_text(chest: EnderChest, name: str, *, with_states: bool) -> str:
    """从末影箱取某实体的人物卡正文；取不到则给占位（不臆造事实）。"""
    blocks: list[str] = []
    for rec in chest.get_any(name):
        if rec.layer not in ("character", "world", "item", "timeline"):
            continue
        head = C.LAYER_CN.get(rec.layer, rec.layer)
        vs = rec.data.get("version_of") if isinstance(rec.data, dict) else None
        tag = f"v{rec.version}" + (f"/{vs}" if vs else "")
        blocks.append(f"{head}·{name}({tag})：{rec.content}")
    if with_states:
        states = chest.states_of(name)
        if states:
            shown = [
                f"{k}={v}" for k, v in states.items()
                if k not in ("embedding", "last_seen")
            ]
            if shown:
                blocks.append(f"状态·{name}：" + "，".join(shown[:6]))
    if not blocks:
        blocks.append(f"{name}（末影箱暂无档案）")
    return "；".join(blocks)


def _foreshadow_text(fs: Any) -> str:
    fields: dict[str, str] = {"内容": fs.content, "状态": fs.status}
    if fs.planted_chapter:
        fields["埋设"] = fs.planted_chapter
    if fs.planned_resolve:
        fields["计划回收"] = fs.planned_resolve
    if fs.reinforced_chapters:
        fields["已强化"] = ",".join(fs.reinforced_chapters)
    return _entry(fs.id, fields)


class Orchestrator:
    """上下文编排器：把「冻结的末影箱 + 软依赖的 Memory Keeper」组装成 prompt。"""

    def __init__(
        self,
        chest: EnderChest,
        *,
        window_tokens: int = WINDOW_TOKENS_DEFAULT,
        keeper: Any = None,
    ) -> None:
        self.chest = chest
        self.window_tokens = int(window_tokens)
        if self.window_tokens <= 0:
            raise ValueError(f"window_tokens 必须为正整数，收到 {window_tokens!r}")
        self.total_budget = int(self.window_tokens * CONTEXT_RATIO)
        self.budget = self._segment_budgets(self.total_budget)

        #: 检索后端：优先显式注入，其次真 MemoryKeeper，最后内部兜底
        if keeper is not None:
            self.keeper = keeper
            self.keeper_source = "injected"
        elif HAS_RETRIEVAL:
            self.keeper = _RealMemoryKeeper(chest)
            self.keeper_source = "retrieval"
        else:
            self.keeper = _FallbackKeeper(chest)
            self.keeper_source = "fallback"

        self._prefetch_cache: dict[str, dict[str, Any]] = {}
        self._prefetch_queue: list[dict[str, Any]] = []
        self._lock = threading.RLock()
        #: 同一写作周期内已抽过的 low 抽样（传给支持 ``seen_low`` 的 keeper）
        self._seen_low: set[tuple[str, str]] = set()
        self._keeper_params = self._inspect_keeper_params()
        #: 最近一次 assemble_context 的统计（段预算 / 条目明细 / 检索原文）
        self.last_stats: dict[str, Any] = {}

    def _inspect_keeper_params(self) -> frozenset[str]:
        """探测 keeper.search 的关键字参数（软依赖：失败就当空集）。"""
        try:
            import inspect

            params = inspect.signature(self.keeper.search).parameters
            return frozenset(params)
        except Exception:  # noqa: BLE001 - C 扩展 / 无签名时静默降级
            return frozenset()

    # ==================================================================
    # 预算
    # ==================================================================
    @staticmethod
    def _segment_budgets(total: int) -> dict[str, int]:
        """按 §4 比例切分总量；末段吸收取整残差，保证加总 == total。"""
        budgets: dict[str, int] = {}
        for pri in PRIORITIES[:-1]:
            budgets[pri] = int(total * BUDGET_RATIOS[pri])
        budgets[PRIORITIES[-1]] = total - sum(budgets.values())
        return budgets

    def budget_of(self, priority: str) -> int:
        return int(self.budget[priority])

    # ==================================================================
    # 检索
    # ==================================================================
    def search(self, req: dict, *, context_entities: Sequence[str] = ()) -> dict:
        """调用检索后端（真 MemoryKeeper 优先，异常兜底），返回归一化结果。"""
        payload = dict(req or {})
        # 没有种子实体 = 无事可查：直接返回空结果，避免把非法入参喂给 keeper
        seeds = [e for e in _as_list(payload.get("entities")) if isinstance(e, str) and e.strip()]
        if not seeds:
            return _normalize_result(
                {
                    "high": [],
                    "medium": [],
                    "low": [],
                    "total_tokens": 0,
                    "trace": ["skip: 无种子实体，跳过检索"],
                }
            )
        errors: list[str] = []
        if self.keeper_source != "fallback" and getattr(self, "_keeper_broken", False) is False:
            try:
                raw = self._call_keeper(payload, context_entities)
                if isinstance(raw, dict) and raw:
                    return _normalize_result(raw)
                errors.append("keeper 返回空结果")
            except Exception as exc:  # noqa: BLE001 - 软依赖必须容错
                errors.append(f"keeper 失败：{type(exc).__name__}: {exc}")
                log.warning("检索后端失败，回退内部实现：%s", errors[-1])
                self._keeper_broken = True
        result = _normalize_result(_banded_search(self.chest, payload, context_entities=context_entities))
        if errors:
            result["trace"] = list(result.get("trace", [])) + errors
        return result

    def _call_keeper(self, payload: dict, context_entities: Sequence[str]) -> Any:
        """按契约 ``keeper.search(req, *, context_entities=())`` 调用，兼容旧形态。"""
        keeper = self.keeper
        fn = getattr(keeper, "search", None)
        if not callable(fn):
            raise TypeError(f"keeper 没有可调用的 search：{type(keeper).__name__}")
        kwargs: dict[str, Any] = {"context_entities": tuple(context_entities)}
        # retrieval.MemoryKeeper 额外支持 seen_low（同一周期内 low 抽样不重复）
        if "seen_low" in self._keeper_params:
            kwargs["seen_low"] = self._seen_low
        try:
            return fn(payload, **kwargs)
        except TypeError:
            if "seen_low" in self._keeper_params:
                kwargs.pop("seen_low", None)
                try:
                    return fn(payload, **kwargs)
                except TypeError:
                    pass
            # 兼容不接收 context_entities 关键字的实现
            return fn(payload)

    # ==================================================================
    # P0：全局大纲 / 当前章节细纲 / core 人物卡 / 待处理伏笔
    # ==================================================================
    def _build_p0(self, outline: dict, current_chapter: str | None) -> list[str]:
        chest = self.chest
        units: list[str] = []
        label_i = 0

        def label() -> str:
            nonlocal label_i
            lt = _ITEM_LABELS[label_i] if label_i < len(_ITEM_LABELS) else str(label_i)
            label_i += 1
            return lt

        # A. 全局大纲（仅在有内容时出现，避免空段）
        global_outline = outline.get("global") or outline.get("global_outline") or ""
        if isinstance(global_outline, dict):
            global_outline = global_outline.get("summary") or C.dumps(global_outline)[:400]
        title = str(outline.get("title") or outline.get("book_title") or "")
        gfields = {"标题": title, "全局大纲": str(global_outline or "")}
        if any(gfields.values()):
            units.append(_entry(label(), gfields))

        # B. 当前章节细纲
        cur = self._chapter_outline(outline)
        cur_dict: dict[str, Any] = cur if isinstance(cur, dict) else {}
        chap_id = (
            (cur_dict.get("chapter_id") if cur_dict else None)
            or outline.get("current_chapter_id")
        )
        focus_id = str(chap_id or current_chapter or "—")
        names = [_name_of(x) for x in _as_list(cur_dict.get("active_characters"))]
        locs = [_name_of(x) for x in _as_list(cur_dict.get("locations"))]
        items = [_name_of(x) for x in _as_list(cur_dict.get("items"))]
        resolve_ids = [_name_of(x) for x in _as_list(cur_dict.get("foreshadowing_to_resolve"))]
        # 已回收 / 已废弃的伏笔不再作为「待处理」呈现（末影箱是唯一事实来源）
        open_ids = {C.normalize_name(f.id) for f in chest.all_foreshadowings(only_open=True)}
        resolve_open = [r for r in resolve_ids if r and C.normalize_name(r) in open_ids]
        resolve_done = [r for r in resolve_ids if r and C.normalize_name(r) not in open_ids]
        plant = [x for x in _as_list(cur_dict.get("foreshadowing_to_plant"))]
        plant_txt = "; ".join(
            (_name_of(x) + (f"→{x['planned_resolve']}" if isinstance(x, dict) and x.get("planned_resolve") else ""))
            for x in plant
        )
        cfields = {
            "章节": focus_id,
            "目标": str(cur_dict.get("chapter_goal") or ""),
            "出场": ",".join([n for n in names if n]),
            "地点": ",".join([n for n in locs if n]),
            "物品": ",".join([n for n in items if n]),
            "回收伏笔": ",".join(resolve_open),
            "已回收伏笔": ",".join(resolve_done),
            "埋设伏笔": plant_txt,
            "情绪": str(cur_dict.get("mood") or ""),
            "风格": ",".join([s for s in _as_list(cur_dict.get("style_keywords")) if isinstance(s, str)]),
        }
        if any(v for k, v in cfields.items() if k != "章节"):
            units.append(_entry(label(), cfields))

        # C. core 角色人物卡（active_characters 中 importance == core）
        core_names = [_name_of(x) for x in _as_list(cur_dict.get("active_characters")) if _is_core(x)]
        for name in C.dedupe([n for n in core_names if n]):
            label()  # 占用一个字母序号，保持 P0 的 A/B/C/D… 连续
            units.append(_entry(f"人物卡 {name}", {"档案": _character_text(chest, name, with_states=True)}))

        # D. 待处理伏笔（open），本章要回收的排在前面
        open_fs = chest.all_foreshadowings(only_open=True)
        resolve_set = {C.normalize_name(r) for r in resolve_ids if r}
        open_fs.sort(key=lambda f: (C.normalize_name(f.id) not in resolve_set, f.planned_no if f.planned_no > 0 else 10**6, f.id))
        for fs in open_fs:
            fields = {"伏笔": _foreshadow_text(fs)}
            if C.normalize_name(fs.id) in resolve_set:
                fields["提示"] = "本章计划回收"
            elif fs.planned_no > 0:
                fields["提示"] = f"计划第{fs.planned_no}章回收"
            units.append(_entry(label(), fields))
        return units

    # ==================================================================
    # P1：高相关预检索
    # ==================================================================
    def _excluded_from_retrieval(self) -> set[str]:
        """不应由**检索**重复呈现的实体（它们在别的段已有专门位置）。

        * 风格样本：P4 专段，检索里出现属于预算浪费；
        * 伏笔：P0 专段（open 全量 + 本章回收状态），检索再吐一遍只会重复。
        """
        keys: set[str] = set()
        try:
            for f in self.chest.all_foreshadowings():
                keys.add(C.normalize_name(f.id))
        except Exception:  # noqa: BLE001
            pass
        return keys

    def _usable_hits(self, hits: Sequence[dict]) -> list[dict]:
        """过滤掉已有专段的命中（风格 / 伏笔）。"""
        out: list[dict] = []
        for hit in hits:
            layer = str(hit.get("layer") or "")
            if layer == "style":
                continue
            if layer == "foreshadowing":
                continue
            out.append(hit)
        return out

    @staticmethod
    def _chapter_outline(outline: dict) -> dict:
        """取出"本章细纲"。

        兼容两种形态：§3.1 的平铺细纲（``{"chapter_id": ...}`` 直接是细纲），
        以及外部包了一层 ``{"current_chapter": {...}}`` 的嵌套形态。
        平铺时若同时也带了 current_chapter，则用平铺字段覆盖嵌套字段（就近优先）。
        """
        cur = outline.get("current_chapter")
        base = dict(cur) if isinstance(cur, dict) else {}
        if outline.get("chapter_id") or outline.get("active_characters") or outline.get("chapter_goal"):
            for k, v in outline.items():
                if k in ("current_chapter", "global_outline", "extra"):
                    continue
                base[k] = v
        return base

    def _outline_entities(self, outline: dict) -> list[str]:
        """细纲 -> 检索种子实体（core 优先，其次 secondary/minor 与地点/物品）。"""
        cur = self._chapter_outline(outline)
        core: list[str] = []
        rest: list[str] = []
        for item in _as_list(cur.get("active_characters")):
            n = _name_of(item)
            if n:
                (core if _is_core(item) else rest).append(n)
        for key in ("locations", "items"):
            for item in _as_list(cur.get(key)):
                n = _name_of(item)
                if n:
                    rest.append(n)
        for item in _as_list(outline.get("core_characters") or outline.get("characters")):
            n = _name_of(item)
            if n and (_is_core(item) or not isinstance(item, dict)):
                core.append(n)
        return C.dedupe([n for n in core if n] + [n for n in rest if n])

    def _carried_entities(self, outline: dict, current_chapter: str | None) -> list[str]:
        """**已经随 P0 进上下文**、因此不该再让检索重复吐正文的实体。

        只包含：本章细纲里 core 的人物、全部地点、core 物品、本章要回收的伏笔 ID，
        以及全局大纲里点名的 core 角色。刻意**不含** secondary 人物——它们属于 P3。
        """
        cur = self._chapter_outline(outline)
        names: list[str] = []
        for item in _as_list(cur.get("active_characters")):
            if _is_core(item):
                names.append(_name_of(item))
        for item in _as_list(cur.get("locations")):
            names.append(_name_of(item))
        for item in _as_list(cur.get("items")):
            if _is_core(item):
                names.append(_name_of(item))
        for item in _as_list(cur.get("foreshadowing_to_resolve")):
            names.append(_name_of(item))
        for item in _as_list(outline.get("core_characters") or outline.get("characters")):
            if _is_core(item) or not isinstance(item, dict):
                names.append(_name_of(item))
        return C.dedupe([n for n in names if n])

    def _build_search_req(self, outline: dict, current_chapter: str | None) -> tuple[dict, list[str]]:
        """构造内部预检索请求（P1 用，budget = 该段预算）。"""
        entities = self._outline_entities(outline)
        cur = self._chapter_outline(outline)
        query_parts: list[str] = []
        goal = str(cur.get("chapter_goal") or "")
        if goal:
            query_parts.append(goal)
        moods = str(cur.get("mood") or "")
        if moods:
            query_parts.append(moods)
        query_parts.extend(self._outline_terms(outline))
        if current_chapter:
            prev = self.chest.recent_chapters(1, before=current_chapter)
            if prev:
                query_parts.append(str(prev[-1].get("summary") or ""))
        query = " ".join(p for p in query_parts if p).strip()
        layers = [ly for ly in C.LAYERS if ly not in ("style", "foreshadowing")]
        req: dict[str, Any] = {
            "entities": entities,
            "query_context": query,
            "budget": self.budget["P1"],
        }
        if layers:
            req["layers"] = layers
        return req, entities

    def _outline_terms(self, outline: dict) -> list[str]:
        """细纲派生关键词表（种子为空时的兜底检索词）。"""
        cur = self._chapter_outline(outline)
        terms: list[str] = []
        try:
            terms = list(C.keyword_table(cur))
        except Exception:  # noqa: BLE001 - 细纲可能不完整
            terms = []
        terms.extend(str(x) for x in _as_list(cur.get("keywords")) if isinstance(x, str))
        return C.dedupe([t for t in terms if t])

    def _build_p1(self, retrieval: dict) -> list[str]:
        """P1：高相关检索结果。已随 P0 进上下文的条目只留一行占位，不重复正文。"""
        units: list[str] = []
        for i, hit in enumerate(self._usable_hits(retrieval.get("high", []) or [])):
            entity = str(hit.get("entity") or "?")
            layer = str(hit.get("layer") or "?")
            conf = float(hit.get("confidence") or 0.0)
            if hit.get("in_context"):
                units.append(f"{i + 1}. [{layer}] {entity}（已在 P0 上下文，略）")
                continue
            content = str(hit.get("content") or "").strip()
            head = f"[{layer}] {entity} v{hit.get('version', 1)} 置信{conf:.2f}"
            body = content if len(content) <= 200 else content[:200] + "…"
            units.append(f"{i + 1}. {head}：{body}")
        return units

    def _render_hit(self, hit: dict, idx: int | str) -> str:
        entity = str(hit.get("entity") or "?")
        layer = str(hit.get("layer") or "?")
        content = str(hit.get("content") or "").strip()
        if hit.get("in_context"):
            content = "（已在上下文，省略）"
        conf = float(hit.get("confidence") or 0.0)
        head = f"[{layer}] {entity} v{hit.get('version', 1)} 置信{conf:.2f}"
        body = content if len(content) <= 200 else content[:200] + "…"
        return f"{idx}. {head}：{body}"

    # ==================================================================
    # P2：近期正文（无正文则 summary + 关键句）
    # ==================================================================
    def _recent_chapters(self, current_chapter: str | None) -> list[dict]:
        """取最近 1-2 章；有当前章时排除当前章及其之后。"""
        chest = self.chest
        chapters = chest.recent_chapters(2, before=current_chapter) if current_chapter else chest.recent_chapters(2)
        chapters = [c for c in chapters if c.get("chapter_id") != current_chapter]
        return chapters

    def _build_p2(self, current_chapter: str | None) -> list[str]:
        units: list[str] = []
        for ch in self._recent_chapters(current_chapter):
            text = str(ch.get("text") or "")
            summary = str(ch.get("summary") or "")
            title = str(ch.get("title") or "")
            body = text.strip() or summary.strip() or ""
            if not body:
                continue
            if not text.strip() and not summary.strip():
                continue
            snippet = body if len(body) <= 260 else body[:260] + "…"
            fields = {"标题": title, "摘要": "" if summary in text else summary, "正文": snippet}
            units.append(_entry(str(ch.get("chapter_id") or "?"), fields))
        return units

    # ==================================================================
    # P3：中相关结果 / secondary 角色与地点卡
    # ==================================================================
    @staticmethod
    def _entity_key(chest: EnderChest, name: str, layer: str | None = None) -> str:
        """实体的比较键：统一成 (层, 标准名)；不指定层时取该实体任意一层的首个。"""
        canon = chest.resolve_entity(name)
        if layer:
            return f"{layer}|{C.normalize_name(canon)}"
        recs = chest.get_any(canon)
        lay = recs[0].layer if recs else "character"
        return f"{lay}|{C.normalize_name(canon)}"

    def _p0_entity_keys(self, outline: dict) -> set[str]:
        """P0 已经带正文的实体（人物卡 / core 物品），P3 不应重复。"""
        keys: set[str] = set()
        cur = self._chapter_outline(outline)
        cur = cur if isinstance(cur, dict) else {}
        for item in _as_list(cur.get("active_characters")):
            if _is_core(item):
                name = _name_of(item)
                if name:
                    keys.add(self._entity_key(self.chest, name))
        for item in _as_list(cur.get("items")):
            if _is_core(item):
                name = _name_of(item)
                if name:
                    keys.add(self._entity_key(self.chest, name))
        return keys

    def _build_p3(self, retrieval: dict, outline: dict, focus_keys: set[str]) -> list[str]:
        """P3：次要角色 / 地点卡（细纲直给，优先）+ 检索 medium 结果。

        ``focus_keys`` 只用于避免「P3 内部二次重复」；**不**把 P1 的 high 命中也算作
        「已覆盖」——P1 的 high 与 P3 的 secondary 卡职责不同（前者是检索到的相关记忆，
        后者是细纲点名要出场的人物/地点档案），即便同名也应各留一份。
        唯一要排除的是 P0 已经带过正文的实体（core 人物 / core 物品）。
        """
        units: list[str] = []
        seen = set(focus_keys) | self._p0_entity_keys(outline)
        seed_keys: set[str] = set()
        cur = self._chapter_outline(outline)

        # 1) secondary 人物 / 地点卡（细纲直给，优先级最高）
        seed_names: list[str] = []
        seed_names.extend(_name_of(x) for x in _as_list(cur.get("active_characters")) if not _is_core(x))
        seed_names.extend(_name_of(x) for x in _as_list(cur.get("locations")))
        for name in C.dedupe([n for n in seed_names if n]):
            recs = self.chest.get_any(name)
            key = (
                f"{recs[0].layer}|{C.normalize_name(recs[0].entity)}"
                if recs
                else self._entity_key(self.chest, name)
            )
            if key in seen or key in seed_keys:
                continue
            seed_keys.add(key)
            units.append(_entry(name, {"卡片": _character_text(self.chest, name, with_states=False)}))

        # 2) 检索的 medium 结果（次要人物卡已经给过的实体不再重复）
        for i, hit in enumerate(self._usable_hits(retrieval.get("medium", []) or [])):
            key = _hit_key(hit)
            if key in seen or key in seed_keys:
                continue
            seen.add(key)
            units.append(self._render_hit(hit, i + 1))
        return units

    # ==================================================================
    # P4：探索性低相关样本 + 风格样本
    # ==================================================================
    def _style_units(self) -> list[str]:
        units: list[str] = []
        names = self.chest.all_entities(["style"])
        for entity in names:
            rec = self.chest.get("style", entity)
            if rec is None:
                continue
            body = str(rec.content or "").strip()
            if not body:
                continue
            units.append(_entry(f"{entity}", {"风格样本": body if len(body) <= 300 else body[:300] + "…"}))
        return units

    def _build_p4(self, retrieval: dict) -> list[str]:
        units = self._style_units()
        for i, hit in enumerate(retrieval.get("low", []) or []):
            units.append(self._render_hit(hit, f"抽样{i + 1}"))
        return units

    # ==================================================================
    # 预算裁剪：严格不超过段预算
    # ==================================================================
    @staticmethod
    def _shrink_unit(unit: str, limit: int) -> list[str] | None:
        """把单条压缩到 limit token 内：优先保留 label + 首个字段，再退化为整条截断。"""
        if limit <= 0:
            return None
        if C.est_tokens(unit) <= limit:
            return [unit]
        label, fields = _entry_parts(unit)
        for keep in (2, 1):
            head = _rebuild_entry(label, fields[:keep])
            if head and C.est_tokens(head) <= limit:
                return [head]
        if label and C.est_tokens(label) + 2 <= limit:
            return [_rebuild_entry(label, [])]
        clipped = C.clip_tokens(unit, limit)
        return [clipped] if clipped else None

    def _fit(self, priority: str, budget: int, units: Sequence[str]) -> tuple[list[str], list[str]]:
        """按优先级把条目塞进段预算，返回 (保留条目, 被裁标识)。

        两步打包（都**严格**不超预算）：
          1. 顺位装：按顺序装得下就整条放；
          2. 补白：顺位装不下的条目，若整条放不下就尝试压缩（保留 label + 首个字段 /
             截断到剩余空间），仍不行就跳过并记入 ``dropped``——后面的短条目可以
             继续填空白，避免预算被「一条太长的内容」永久浪费。
        """
        content: list[str] = []
        dropped: list[str] = []
        used = 0
        sep_cost = 1  # 条目之间的换行

        for unit in units:
            if not unit:
                continue
            label_hint = unit.split(":", 1)[0].replace("· ", "", 1).strip()
            key = f"{priority}:{label_hint}"
            cost = C.est_tokens(unit) + (sep_cost if content else 0)
            if used + cost <= budget:
                content.append(unit)
                used += cost
                continue

            budget_left = budget - used - (sep_cost if content else 0)
            placed: str | None = None
            partial = False

            # 1) 无损前缀：取装得下的最长字段前缀
            frags = _entry_parts_with_splits(unit)
            for frag in frags:
                if C.est_tokens(frag) <= budget_left:
                    placed, partial = frag, frag != frags[-1]
                else:
                    break
            # 2) 有损压缩：保留 label + 首个字段，或整条截断
            if placed is None:
                shrunk = self._shrink_unit(unit, budget_left)
                if shrunk:
                    placed, partial = shrunk[0], True

            if placed is None:
                dropped.append(key)
                continue
            extra = C.est_tokens(placed) + (sep_cost if content else 0)
            if used + extra > budget:  # 双保险，正常路径不会触发
                dropped.append(key)
                continue
            content.append(placed)
            used += extra
            if partial:
                dropped.append(f"{key}#partial")

        return content, dropped

    @staticmethod
    def _join(priority: str, units: Sequence[str]) -> str:
        body = "\n".join(units)
        if not body:
            return ""
        return f"{_SECTION_TITLES[priority]}\n{body}"

    # ==================================================================
    # 主入口：assemble_context
    # ==================================================================
    def assemble_context(
        self,
        outline: dict,
        *,
        current_chapter: str | None = None,
        extra: dict | None = None,
    ) -> dict[str, Any]:
        """按 P0-P4 预算组装章节上下文。

        返回::

            {
              "sections": {"P0": str, "P1": str, "P2": str, "P3": str, "P4": str},
              "text": str,                # 拼好的完整 prompt 文本
              "budget": {...},            # 各段 token 数（及预算明细）
              "total_tokens": int,        # est_tokens(text)，硬保证 <= int(window*0.7)
              "dropped": [str, ...],      # 被裁掉的内容标识
              "retrieval": dict,          # 检索原始结果（已归一化）
              "entities": [str, ...],     # 检索种子实体
            }
        """
        outline = outline if isinstance(outline, dict) else {}
        extra = extra if isinstance(extra, dict) else {}
        if current_chapter is None:
            cur = self._chapter_outline(outline)
            current_chapter = (
                (cur.get("chapter_id") if isinstance(cur, dict) else None)
                or outline.get("current_chapter_id")
            )
        current_chapter = str(current_chapter) if current_chapter else None

        dropped: list[str] = []
        sections: dict[str, str] = {}
        used: dict[str, int] = {}

        # ---------------- P0 ----------------
        req, seed_entities = self._build_search_req(outline, current_chapter)
        req["budget"] = max(int(self.budget["P1"]), 1)
        carried = self._carried_entities(outline, current_chapter)
        ctx_list = list(carried)
        if current_chapter:
            ctx_list.append(current_chapter)
        ctx_entities = tuple(C.dedupe(ctx_list))
        retrieval = self.search(req, context_entities=ctx_entities)

        p0_units = self._build_p0(outline, current_chapter)
        p1_hits = self._usable_hits(retrieval.get("high", []) or [])
        p1_units = self._build_p1({"high": p1_hits})
        p2_units = self._build_p2(current_chapter)
        # P3 的焦点集合为空：P1 的 high 命中不剥夺 P3 的 secondary 卡片
        p3_units = self._build_p3(retrieval, outline, set())
        p4_units = self._build_p4(retrieval)
        extra_cfg = extra.get("sections") if isinstance(extra.get("sections"), dict) else {}

        for priority, units in (
            ("P0", p0_units),
            ("P1", p1_units),
            ("P2", p2_units),
            ("P3", p3_units),
            ("P4", p4_units),
        ):
            cap = int(self.budget[priority])
            if extra_cfg.get(priority):
                units = list(units) + [str(extra_cfg[priority])]
            kept, cut = self._fit(priority, cap, units)
            sections[priority] = self._join(priority, kept)
            used[priority] = C.est_tokens(sections[priority])
            # 截断后二次兜底：clip_tokens 是硬保证
            if used[priority] > cap:
                sections[priority] = C.clip_tokens(sections[priority], cap)
                used[priority] = C.est_tokens(sections[priority])
                cut.append(f"{priority}#clip")
            dropped.extend(cut)

        text = "\n\n".join(sections[p] for p in PRIORITIES if sections[p])
        total = C.est_tokens(text)
        # 硬保证：总量 <= int(window * 0.7)
        if total > self.total_budget:
            text = C.clip_tokens(text, self.total_budget)
            total = C.est_tokens(text)
            dropped.append("#total-clip")
        if total > self.total_budget:  # pragma: no cover - clip_tokens 已保证
            raise AssertionError(f"上下文总量 {total} 超过预算 {self.total_budget}")

        for key in extra:
            if key not in ("sections",):
                dropped.append(f"extra:{key}#ignored")

        budget_report: dict[str, Any] = dict(used)
        budget_report["budget"] = dict(self.budget)
        budget_report["total"] = self.total_budget
        budget_report["window_tokens"] = self.window_tokens
        budget_report["retrieval_tokens"] = int(retrieval.get("total_tokens") or 0)

        result: dict[str, Any] = {
            "sections": sections,
            "text": text,
            "budget": budget_report,
            "total_tokens": total,
            "dropped": dropped,
            "retrieval": retrieval,
            "entities": list(seed_entities),
            "current_chapter": current_chapter,
        }
        self.last_stats = {
            "units": {
                "P0": list(p0_units),
                "P1": list(p1_units),
                "P2": list(p2_units),
                "P3": list(p3_units),
                "P4": list(p4_units),
            },
            "budget": dict(self.budget),
            "dropped": list(dropped),
            "keeper": self.keeper_source,
        }
        return result

    # ==================================================================
    # 动态异步预取
    # ==================================================================
    def detect_stream_entities(self, stream_text: str, *, window: int = 60) -> list[str]:
        """在生成流文本的**尾部窗口**里识别实体（用 chest.detect_entities）。"""
        text = str(stream_text or "")
        if not text:
            return []
        window = max(1, int(window))
        tail = text[-window:] if window < len(text) else text
        try:
            found = self.chest.detect_entities(tail)
        except Exception as exc:  # noqa: BLE001 - 软依赖容错
            log.warning("detect_entities 失败，退化到 lexicon：%s", exc)
            try:
                found = L.extract_entities(tail, self.chest.entity_vocab())
            except Exception:  # noqa: BLE001
                found = []
        return C.dedupe([f for f in found if f])

    def prefetch(
        self,
        stream_text: str,
        *,
        window: int = 60,
        context_entities: Sequence[str] = (),
    ) -> dict[str, Any]:
        """流式生成中的异步预取（非阻塞、幂等）。

        * 只关心尾部 ``window`` 个字符里**新出现**、且不在 ``context_entities`` 里的实体；
        * 同一批实体 + 同一窗口在手 → 直接返回缓存（幂等，不重复检索）；
        * 命中缓存时 ``hit_cache=False`` 并写入 ``_prefetch_cache``；
        检索本身在本进程内同步完成（无 GPU / 无网络，单次耗时 < 10ms），但**不阻塞**
        调用方：不抛异常、不改动已组装的上下文，结果进入 ``_prefetch_queue`` 供
        :meth:`drain_prefetch` 取走。
        """
        text = str(stream_text or "")
        found = self.detect_stream_entities(text, window=window)
        ctx = {C.normalize_name(e) for e in context_entities if e}
        new_entities = [e for e in found if C.normalize_name(e) not in ctx]
        out: dict[str, Any] = {
            "prefetched": [],
            "entities": new_entities,
            "window": int(window),
            "cached": False,
        }
        if not new_entities:
            return out

        key = _prefetch_key(new_entities, int(window))
        with self._lock:
            cached = self._prefetch_cache.get(key)
        if cached is not None:
            out["prefetched"] = list(cached.get("prefetched", []))
            out["cached"] = True
            out["retrieval"] = cached.get("retrieval", {})
            return out

        req = {
            "entities": list(new_entities),
            "query_context": text[-max(int(window), 1):].strip(),
            "budget": min(SEARCH_TOOL_BUDGET_DEFAULT, max(C.est_tokens(text[-120:]), 120)),
        }
        try:
            retrieval = self.search(req, context_entities=tuple(context_entities))
        except Exception as exc:  # noqa: BLE001 - 预取绝不能打断生成
            log.warning("预取检索失败：%s", exc)
            retrieval = _normalize_result(
                {"high": [], "medium": [], "low": [], "total_tokens": 0, "trace": [f"prefetch failed: {exc}"]}
            )
        prefetched = C.dedupe([str(h.get("entity")) for h in _prefetch_list(retrieval) if h.get("entity")])
        item: dict[str, Any] = {
            "entities": list(new_entities),
            "prefetched": prefetched,
            "retrieval": retrieval,
            "total_tokens": int(retrieval.get("total_tokens") or 0),
            "window": int(window),
            "context_entities": list(context_entities),
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "hit_cache": False,
        }
        with self._lock:
            existing = self._prefetch_cache.get(key)
            if existing is not None:  # 并发下的幂等：以先到者为准
                out["prefetched"] = list(existing.get("prefetched", []))
                out["cached"] = True
                out["retrieval"] = existing.get("retrieval", {})
                return out
            self._prefetch_cache[key] = item
            queued = {C.dumps(q.get("entities", [])) for q in self._prefetch_queue}
            if C.dumps(new_entities) not in queued:
                self._prefetch_queue.append(item)
        out["prefetched"] = prefetched
        out["retrieval"] = retrieval
        out["total_tokens"] = item["total_tokens"]
        return out

    def drain_prefetch(self) -> list[dict[str, Any]]:
        """取出并清空预取队列（FIFO）。"""
        with self._lock:
            items = list(self._prefetch_queue)
            self._prefetch_queue.clear()
        return items

    def pending_prefetch(self) -> int:
        with self._lock:
            return len(self._prefetch_queue)

    @property
    def prefetch_cache(self) -> dict[str, dict[str, Any]]:
        """预取缓存（浅拷贝，防止外部误改内部状态）。"""
        with self._lock:
            return dict(self._prefetch_cache)

    def clear_prefetch(self) -> None:
        """清空缓存与队列（换章 / 新书时调用）。"""
        with self._lock:
            self._prefetch_cache.clear()
            self._prefetch_queue.clear()

    # ==================================================================
    # 工具调用：search_memory
    # ==================================================================
    def build_tool_call(
        self,
        entities: Any,
        query_context: Any,
        layers: Any = None,
        budget: int = SEARCH_TOOL_BUDGET_DEFAULT,
    ) -> dict[str, Any]:
        """构造重试安全的 ``search_memory`` 工具调用（OpenAI function-calling 形态）。

        返回 ``{"name": "search_memory", "arguments": {...}, "arguments_json": str}``，
        ``arguments`` 已通过 ``common.validate(..., schema("search_memory_tool"))``。
        """
        if isinstance(entities, str):
            raw_items: list[Any] = [entities]
        else:
            raw_items = _as_list(entities)
        cleaned: list[str] = []
        for item in raw_items:
            name = _name_of(item)
            if isinstance(name, str) and name.strip():
                cleaned.append(name.strip())
        ents = C.dedupe(cleaned)
        if not ents:
            raise C.SchemaError("#/entities: search_memory 需要至少 1 个实体")

        try:
            bud = int(budget)
        except (TypeError, ValueError):
            bud = SEARCH_TOOL_BUDGET_DEFAULT
        bud = max(1, min(8000, bud))

        query = "" if query_context is None else str(query_context)

        lay: list[str] | None = None
        if layers is not None:
            raw = [str(x) for x in _as_list(layers) if str(x) in C.LAYERS]
            lay = C.dedupe(raw) or None

        args: dict[str, Any] = {"entities": ents, "query_context": query, "budget": bud}
        if lay:
            args["layers"] = lay
        C.validate(args, C.schema("search_memory_tool"))
        return {
            "name": "search_memory",
            "arguments": args,
            "arguments_json": C.dumps(args),
        }

    def render_tool_result(self, result: Any) -> str:
        """把 ``search_memory`` 返回值渲染成可注入 LLM 的紧凑文本。

        * high  —— 全量（不截断正文）；
        * medium —— 摘要（每条压到约 80 字）；
        * low   —— 只给一行抽样提示，避免污染生成；
        """
        res = _normalize_result(result if isinstance(result, dict) else {})
        high = res.get("high", []) or []
        medium = res.get("medium", []) or []
        low = res.get("low", []) or []
        if not (high or medium or low):
            return "【记忆检索】无匹配记录。"

        lines: list[str] = ["【记忆检索】"]
        if high:
            lines.append(f"◆ high（{len(high)} 条，全量）")
            for h in high:
                content = "已在上下文" if h.get("in_context") else str(h.get("content", ""))
                lines.append(f"- [{h.get('layer')}] {h.get('entity')} v{h.get('version', 1)}：{content}")
        if medium:
            lines.append(f"◇ medium（{len(medium)} 条，摘要）")
            for h in medium:
                content = "已在上下文" if h.get("in_context") else str(h.get("content", ""))
                summary = content if len(content) <= 80 else content[:80] + "…"
                lines.append(f"- [{h.get('layer')}] {h.get('entity')}：{summary}")
        if low:
            names = "、".join(f"{h.get('entity')}({h.get('layer')})" for h in low[:5])
            more = "" if len(low) <= 5 else f" 等 {len(low)} 条"
            lines.append(f"· low（{len(low)} 条，判定为弱相关内容，仅在必要时使用）：{names}{more}")
        lines.append(f"[共 {res.get('total_tokens', 0)} token]")
        return "\n".join(lines)
