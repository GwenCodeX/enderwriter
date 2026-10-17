"""Memory Keeper：记忆守护者的分层检索（规范 §3.3 / §4）。

设计目标：把末影箱（``chest.store.EnderChest``）的三重索引（关键词 / 向量 / 图）
收敛成一个 **token 预算内、可解释、可复现** 的 ``search_memory`` 工具。

一次检索的流水线（严格照规范第四节）：

  1. **入参校验**：用 ``common.schema("search_memory_tool")`` 校验 ``req``，
     非法请求（entities 为空 / budget<=0 / 未知格子）抛 ``common.SchemaError``；
  2. **实体归一化**：``chest.resolve_entity`` 把别名映射为标准名，
     别名映射与上下文重叠都写进 ``trace``；
  3. **多通道召回**：``search_terms(lexicon.analyze(query_context + entities))``、
     ``search_vector(query_context)``、``search_graph(entities)``，
     外加"实体精确命中"通道；同一记录记录其命中通道集合；
  4. **三档分层**：high = 多通道命中（>=2）或实体精确命中；medium = 单通道且过阈值；
     low = 剩余弱相关（只抽样 1~2 条并打标）；
  5. **预算分档**：total budget 的 50% / 35% / 15% 分配给 high / medium / low，
     high 给完整 content，medium 给 ``clip_tokens`` 摘要，low 只给极短摘要 + ``band`` 标记；
  6. **总预算裁剪**：token 数用 ``common.est_tokens`` 估算，最终保证
     ``sum(est_tokens(content)) <= budget``，回填 ``total_tokens``；
  7. **契约校验**：结果用 ``common.schema("memory_result")`` 再校验一遍（额外允许
     ``elapsed_ms`` / ``normalized`` / ``trace`` / ``dropped``）。

已在上下文中的实体（``context_entities``）不重复返回正文：该实体的记录仍可出现在
high 档（提示"已加载"），但 ``content`` 由 ``Hit.to_dict`` 渲染为 ``"已在上下文"``，
且其 token 开销按提示串计费，不会挤占读者的上下文预算。

同一写作周期内 low 抽样不重复：传入 ``seen_low: set[tuple[str, str]] | None``，
被抽中的 ``(layer, entity)`` 会写回该集合，下一轮检索优先抽没见过的弱相关记录。
"""

from __future__ import annotations

import time
from typing import Any, Sequence

from . import common as C
from . import lexicon
from .chest.store import Hit, Record

__all__ = ["MemoryKeeper", "search_memory", "BAND_RATIOS", "DEFAULT_BUDGET"]

#: 三档预算比例（规范 §4：high 50% / medium 35% / low 15%）
BAND_RATIOS: dict[str, float] = {"high": 0.50, "medium": 0.35, "low": 0.15}

#: ``search_memory`` 单次默认预算（规范 §4：单次 ≤ 800 token）
DEFAULT_BUDGET = 800

#: 各通道默认召回条数（合并后再分层，比单通道 limit 略宽）
_CHANNEL_LIMIT = 40
#: 图通道单独放宽：候选池里最"弱"的一批往往只经图可达，截断太早会抽不到 low
_GRAPH_LIMIT = 200

#: 分层阈值：high 需多通道命中；medium 需单通道分数达标
_EXACT_SCORE = 1.0
_MEDIUM_FLOOR = 0.25
_WEAK_FLOOR = 0.28
#: low 抽样单条摘要上限 / 无历史时的兜底采样额度（token）
_LOW_CLIP_MAX = 60
_LOW_SAMPLE_TOKENS = 40


def _est(text: str) -> int:
    return C.est_tokens(text)


def rendered_cost(hit: Hit) -> int:
    """一条命中在**最终返回 JSON 里**的 token 开销。

    ``Hit.to_dict`` 会把 in_context 命中的 content 渲染成 ``"已在上下文"``，
    因此实际占用的是提示串的 token，而不是被遮掉的原文——这样"已在上下文"
    的命中不会再把读者的预算吃掉一遍。
    """
    if getattr(hit, "in_context", False):
        return _est("已在上下文")
    return _est(hit.content)


class MemoryKeeper:
    """记忆守护者：在固定 token 预算内做三档分层检索。

    典型用法::

        mk = MemoryKeeper(chest)
        res = mk.search({"entities": ["炎帝"], "query_context": "黑戒"}, 
                        context_entities=("萧炎",))
        tools = mk.tool_schema()          # 可直接喂给 function-calling
    """

    __slots__ = ("chest", "default_budget", "log")

    def __init__(self, chest: Any, *, default_budget: int = DEFAULT_BUDGET) -> None:
        if chest is None:
            raise ValueError("MemoryKeeper 需要末影箱实例")
        self.chest = chest
        self.default_budget = int(default_budget)
        self.log = C.get_logger("memory_keeper")

    # ------------------------------------------------------------------
    # 对外：检索
    # ------------------------------------------------------------------
    def search(
        self,
        req: dict[str, Any],
        *,
        context_entities: Sequence[str] = (),
        seen_low: set[tuple[str, str]] | None = None,
    ) -> dict[str, Any]:
        """执行一次 ``search_memory``。

        :param req: ``search_memory_tool`` Schema 的入参 dict；
        :param context_entities: 已经在上下文里的实体（不重复返回正文）；
        :param seen_low: 同一写作周期内已抽过的 low 抽样集合，会被原地更新；
        :raises common.SchemaError: 非法请求 / 返回值违反 ``memory_result``。
        """
        t0 = time.perf_counter()
        trace: list[str] = []

        # --- 1) 入参校验（非法请求在这里抛 SchemaError） -----------------
        C.validate(req, C.schema("search_memory_tool"))
        req = dict(req)
        entities_in: list[str] = [str(x) for x in req["entities"]]
        query_context: str = str(req.get("query_context") or "")
        layers: list[str] | None = list(req["layers"]) if req.get("layers") else None
        budget: int = int(req.get("budget") or self.default_budget)
        if budget <= 0:  # 双保险：Schema 已挡 <=0，这里兼容以 default_budget 被调坏
            raise C.SchemaError(f"budget 必须为正整数，收到 {budget}")
        trace.append(f"budget={budget}; layers={'*' if layers is None else ','.join(layers)}")

        # --- 2) 实体归一化（称呼格：别名 -> 标准名） --------------------
        entities, alias_map, canonical_aliases = self._normalize(entities_in, trace)
        if not entities:
            raise C.SchemaError("entities 归一化后为空")
        ctx_set = self._context_set(context_entities, trace)

        # --- 3) 多通道召回 ---------------------------------------------
        channels = self._recall(entities, query_context, layers, alias_map, trace)

        # --- 4) 三档分层 -----------------------------------------------
        bands = self._classify(channels, ctx_set, trace)

        # --- 5) 预算分档 + 6) 总预算裁剪 --------------------------------
        buckets = self._allocate(bands, budget, ctx_set, canonical_aliases, trace)

        # --- 7) low 抽样去重（同一写作周期内不重复抽） -------------------
        self._apply_seen_low(buckets, bands, seen_low, ctx_set, canonical_aliases, trace)

        high, medium, low = buckets["high"], buckets["medium"], buckets["low"]
        used = sum(rendered_cost(h) for h in (*high, *medium, *low))
        elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 3)

        result: dict[str, Any] = {
            "high": [h.to_dict() for h in high],
            "medium": [h.to_dict() for h in medium],
            "low": [h.to_dict() for h in low],
            "total_tokens": int(used),
            "elapsed_ms": elapsed_ms,
            "normalized": {
                "input": entities_in,
                "entities": entities,
                "aliases": alias_map,
                "canonical": canonical_aliases,
                "in_context": sorted(ctx_set),
                "budget": budget,
                "bands": {b: len(v) for b, v in buckets.items()},
            },
            "trace": trace,
        }
        C.validate(result, C.schema("memory_result"))
        self.log.debug("search_memory entities=%s tokens=%d", entities, used)
        return result

    def tool_schema(self) -> dict[str, Any]:
        """``search_memory`` 的 function-calling 入参 Schema（直接复用冻结契约）。"""
        return C.schema("search_memory_tool")

    # ------------------------------------------------------------------
    # 步骤 2：实体归一化
    # ------------------------------------------------------------------
    def _normalize(
        self, entities_in: Sequence[str], trace: list[str]
    ) -> tuple[list[str], dict[str, str], dict[str, list[str]]]:
        """别名 -> 标准名；返回 (标准名列表, 别名->标准名, 标准名->全部别名)。"""
        entities: list[str] = []
        alias_map: dict[str, str] = {}
        canonical_aliases: dict[str, list[str]] = {}
        for raw in entities_in:
            canon = self.chest.resolve_entity(raw)
            if not canon:
                continue
            if canon not in entities:
                entities.append(canon)
            if canon not in canonical_aliases:
                canonical_aliases[canon] = []
            for alias in (raw, C.normalize_name(raw)):
                if alias and alias != canon:
                    alias_map[alias] = canon
                    if alias not in canonical_aliases[canon]:
                        canonical_aliases[canon].append(alias)
            for alias in self._safe_synonyms(canon):
                if alias and alias != canon and alias not in canonical_aliases[canon]:
                    canonical_aliases[canon].append(alias)
        if alias_map:
            pairs = ", ".join(f"{a}→{c}" for a, c in sorted(alias_map.items()))
            trace.append(f"normalized {len(alias_map)} 个别名：{pairs}")
        else:
            trace.append(f"normalized 0 个别名；entities={entities}")
        return entities, alias_map, canonical_aliases

    def _safe_synonyms(self, canonical: str) -> list[str]:
        try:
            return list(self.chest.synonyms(canonical))
        except Exception:  # pragma: no cover - store 冻结，防御性兜底
            return []

    def _context_set(self, context_entities: Sequence[str], trace: list[str]) -> set[str]:
        """上下文实体也做归一化，保证 别名/标准名 都能去重。"""
        ctx: set[str] = set()
        for name in context_entities or ():
            canon = self.chest.resolve_entity(name)
            if canon:
                ctx.add(canon)
        if ctx:
            trace.append(f"context_entities→{sorted(ctx)}（正文不重复返回）")
        return ctx

    # ------------------------------------------------------------------
    # 步骤 3：多通道召回
    # ------------------------------------------------------------------
    def _recall(
        self,
        entities: Sequence[str],
        query_context: str,
        layers: list[str] | None,
        alias_map: dict[str, str],
        trace: list[str],
    ) -> dict[tuple[str, str], dict[str, Any]]:
        """返回 ``{(layer, entity): {record, scores, channels, exact}}``。"""
        terms_text = query_context
        for ent in entities:
            terms_text = f"{terms_text} {ent}"
        terms = lexicon.analyze(terms_text)
        # 别名也参与关键词召回：读者可能用"炎帝"称呼标准名"萧炎"。
        # 同时记下 别名词项 -> 别名 的映射，命中后可解释"为什么召回"。
        alias_terms: dict[str, str] = {}
        for alias in alias_map:
            for term in lexicon.analyze(alias):
                alias_terms.setdefault(term, alias)
                terms.append(term)
        terms = C.dedupe([t for t in terms if t])

        merged: dict[tuple[str, str], dict[str, Any]] = {}

        def slot_of(rec: Record) -> dict[str, Any]:
            key = (rec.layer, rec.entity)
            slot = merged.get(key)
            if slot is None:
                slot = {"record": rec, "scores": {}, "channels": [], "exact": False}
                merged[key] = slot
            return slot

        def add_pairs(records: list[tuple[Record, float]], channel: str) -> int:
            n = 0
            for rec, score in records:
                slot = slot_of(rec)
                slot["scores"][channel] = max(slot["scores"].get(channel, 0.0), float(score))
                if channel not in slot["channels"]:
                    slot["channels"].append(channel)
                n += 1
            return n

        # 通道 a：关键词倒排
        n_terms = 0
        if terms:
            n_terms = add_pairs(
                self.chest.search_terms(terms, layers=layers, limit=_CHANNEL_LIMIT), "terms"
            )
        trace.append(f"recall/terms: {len(terms)} 词项 → {n_terms} 命中")

        # 通道 b：向量
        n_vec = 0
        if query_context.strip():
            n_vec = add_pairs(
                self.chest.search_vector(query_context, layers=layers, limit=_CHANNEL_LIMIT),
                "vector",
            )
        trace.append(f"recall/vector: → {n_vec} 命中")

        # 通道 c：图（从请求实体出发，1 跳）
        n_graph = 0
        if entities:
            n_graph = add_pairs(
                self.chest.search_graph(entities, hops=1, layers=layers, limit=_GRAPH_LIMIT),
                "graph",
            )
        trace.append(f"recall/graph: seeds={list(entities)} → {n_graph} 命中")

        # 通道 d：实体精确命中（请求实体本身的记录一律视为强信号）
        n_exact = 0
        for ent in entities:
            for rec in self.chest.get_any(ent, layers):
                slot = slot_of(rec)
                slot["scores"]["exact"] = _EXACT_SCORE
                if "exact" not in slot["channels"]:
                    slot["channels"].append("exact")
                slot["exact"] = True
                n_exact += 1
        trace.append(f"recall/exact: → {n_exact} 命中")

        # 别名归因：请求里的别名本身的记录，若被别名词项召回，标记原因
        if alias_terms:
            alias_seeds = sorted(set(alias_terms.values()))
            for rec, _score in self.chest.search_terms(
                list(alias_terms), layers=layers, limit=_CHANNEL_LIMIT
            ):
                alias = alias_terms.get(rec.entity) or alias_terms.get(rec.entity[:2])
                if not alias and rec.entity not in alias_seeds:
                    continue
                slot = slot_of(rec)
                marker = f"alias:{alias or rec.entity}"
                if marker not in slot["channels"]:
                    slot["channels"].append(marker)
        trace.append(
            f"recall/alias: {len(alias_terms)} 个别名词项参与召回"
            if alias_terms
            else "recall/alias: 无别名"
        )

        # 通道 d：图的二跳弱关联（"探索"通道）
        #
        # 注意：冻结的 ``chest.search_graph`` 实现里，BFS 的 ``seen`` 在深度 1 就把
        # 所有已扫描节点标脏了，因此 hops>=2 实际不会越过第二层；那是底层行为，
        # 不在本组件权限内修改。这里用冻结契约里的 ``chest.neighbors(entity, hops=1)``
        # 手工做一层 2 跳扩展，专供"弱相关探索"，分数压到 1/3 以下，保证只可能进 low。
        n_weak = 0
        weak_targets = self._weak_targets(entities, layers, trace)
        for rec in weak_targets:
            slot = slot_of(rec)
            slot["scores"]["weak"] = max(slot["scores"].get("weak", 0.0), 0.3)
            if "weak" not in slot["channels"]:
                slot["channels"].append("weak")
            n_weak += 1
        trace.append(f"recall/weak(2-hop): → {n_weak} 命中")

        trace.append(f"recall/merged: {len(merged)} 条候选")
        return merged

    def _weak_targets(
        self, entities: Sequence[str], layers: list[str] | None, trace: list[str]
    ) -> list[Record]:
        """2 跳邻域里的记录（排除 0/1 跳与请求实体本身），作为 low 档候选来源。"""
        near: set[str] = set()
        for ent in entities:
            near.add(self.chest.resolve_entity(ent))
            for src, _rel, dst in self.chest.neighbors(ent, hops=1):
                near.add(src)
                near.add(dst)
        far: set[str] = set()
        for node in sorted(near):
            for src, _rel, dst in self.chest.neighbors(node, hops=1):
                far.update((src, dst))
        far -= near
        if not far:
            return []
        out: list[Record] = []
        for name in sorted(far):
            for rec in self.chest.get_any(name, layers):
                out.append(rec)
        trace.append(f"recall/weak-scope: 2 跳实体 {len(far)} 个 {sorted(far)[:6]}")
        return out


    # ------------------------------------------------------------------
    # 步骤 4：三档分层
    # ------------------------------------------------------------------
    @staticmethod
    def _weak_floor(channels: Sequence[str]) -> float:
        """low 档（弱相关）的分数下限：只凭图通道（仅共享邻居、正文无重叠）才算弱相关。"""
        strong = [c for c in channels if c != "exact"]
        return 0.0 if (len(strong) == 1 and strong[0] == "graph") else _WEAK_FLOOR

    def _classify(
        self,
        channels: dict[tuple[str, str], dict[str, Any]],
        ctx_set: set[str],
        trace: list[str],
    ) -> dict[str, list[dict[str, Any]]]:
        """把候选分成 high / medium / low 三档，每档内按综合分降序。"""
        bands: dict[str, list[dict[str, Any]]] = {"high": [], "medium": [], "low": []}
        for key, slot in channels.items():
            rec: Record = slot["record"]
            # exact 通道单独加权；weak 是二跳弱关联；其余（terms/vector/graph/alias:*）为常规证据
            strong = [c for c in slot["channels"] if c not in ("exact", "weak")]
            # 别名归因不算独立证据（它与 terms 出自同一次倒排召回）
            evidence = [c for c in strong if not c.startswith("alias:")]
            confidence = self._confidence(slot)
            item = {
                "key": key,
                "record": rec,
                "channels": tuple(slot["channels"]),
                "confidence": confidence,
                "exact": bool(slot["exact"]),
                "score": self._score(slot),
                "ctx_hit": rec.entity in ctx_set,
            }
            # 二跳弱关联（weak）是纯探索信号：即便它也经图可达，仍压回 low，
            # 保证 low 档真的存在且不与 medium（实际语义关联）抢预算
            if slot["exact"] or len(evidence) >= 2:
                bands["high"].append(item)
            elif "weak" in slot["channels"]:
                bands["low"].append(item)
            elif evidence and confidence >= _MEDIUM_FLOOR:
                bands["medium"].append(item)
            elif confidence >= self._weak_floor(slot["channels"]):
                bands["low"].append(item)
            else:
                # 只有向量噪声等可忽略证据：直接丢弃，不占预算
                continue
        for name, items in bands.items():
            items.sort(key=lambda it: (-it["score"], it["key"][0], it["key"][1]))
            trace.append(f"classify/{name}: {len(items)} 条")
        return bands

    @staticmethod
    def _score(slot: dict[str, Any]) -> float:
        """综合分：各通道分数之和，exact 通道额外加成，用于档内排序。"""
        total = 0.0
        for channel, value in slot["scores"].items():
            total += value * (2.0 if channel == "exact" else 1.0)
        return round(total, 6)

    @staticmethod
    def _confidence(slot: dict[str, Any]) -> float:
        """置信度 ∈ [0,1]。

        分档只用 high/medium/low 三档语义，因此置信度刻意压成三个"台阶"：
        多通道或实体精确 -> 最高档；单通道 -> 中档；弱相关 -> 低档。
        台阶内再按命中强度细调，便于档内排序而不是跨档越级。
        """
        strong = [c for c in slot["channels"] if c not in ("exact", "weak")]
        best = max(slot["scores"].values()) if slot["scores"] else 0.0
        # 别名归因与主通道同类（同为倒排命中），只是不计入"独立证据"数
        if slot["exact"] or len([c for c in strong if not c.startswith("alias:")]) >= 2:
            lo = 0.80
        elif strong:
            lo = 0.35
        elif "weak" in slot["channels"]:
            lo = 0.12
        else:
            lo = 0.10
        conf = lo + min(0.15, 0.15 * max(0.0, best))
        return round(min(1.0, max(0.0, conf)), 4)

    # ------------------------------------------------------------------
    # 步骤 5：预算分档（50% / 35% / 15%）
    # ------------------------------------------------------------------
    def _allocate(
        self,
        bands: dict[str, list[dict[str, Any]]],
        budget: int,
        ctx_set: set[str],
        canonical_aliases: dict[str, list[str]],
        trace: list[str],
    ) -> dict[str, list[Hit]]:
        """按档分配预算并填充 Hit；未用完的额度顺延给下一档，保证总 token <= budget。"""
        caps = {
            "high": max(1, int(budget * BAND_RATIOS["high"])),
            "medium": max(1, int(budget * BAND_RATIOS["medium"])),
            "low": max(1, int(budget * BAND_RATIOS["low"])),
        }
        out: dict[str, list[Hit]] = {"high": [], "medium": [], "low": []}
        total_left = budget

        # ---- high：完整 content ----
        cap = min(caps["high"], total_left)
        for item in bands["high"]:
            rec: Record = item["record"]
            in_ctx = rec.entity in ctx_set
            content = self._render(rec, item, canonical_aliases, in_ctx)
            hit = rec.to_hit(band="high", confidence=item["confidence"])
            hit.in_context = in_ctx
            self._finish_hit(hit, content, item)
            cost = rendered_cost(hit)
            if cost > total_left:
                break
            if cost > cap and out["high"]:
                break
            out["high"].append(hit)
            total_left -= cost
            cap -= cost
        used_high = sum(rendered_cost(h) for h in out["high"])
        trace.append(f"budget/high: {used_high}/{caps['high']} tokens（完整正文，{len(out['high'])} 条）")

        # ---- medium：clip_tokens 摘要（分池，<= 35% 总预算） ----
        cap = min(caps["medium"] + (caps["high"] - used_high), total_left)
        medium_size = max(1, int(caps["medium"] / max(1, len(bands["medium"]))))
        for item in bands["medium"]:
            rec: Record = item["record"]
            in_ctx = rec.entity in ctx_set
            full = self._render(rec, item, canonical_aliases, in_ctx)
            share = min(medium_size, cap)
            if share <= 0 or total_left <= 0:
                break
            content = C.clip_tokens(full, share)
            hit = rec.to_hit(band="medium", confidence=item["confidence"])
            hit.in_context = in_ctx
            self._finish_hit(hit, content, item)
            cost = rendered_cost(hit)
            out["medium"].append(hit)
            total_left -= cost
            cap -= cost
        used_medium = sum(rendered_cost(h) for h in out["medium"])
        trace.append(f"budget/medium: {used_medium}/{caps['medium']} tokens（摘要，{len(out['medium'])} 条）")

        # ---- low：只抽样 1~2 条，极短摘要 + band 标记（独立分池 <= 15% 总预算） ----
        cap = min(caps["low"] + (caps["medium"] - used_medium), total_left)
        limit = 2 if cap >= 40 else 1
        candidate_cap = cap
        per = max(20, min(60, max(12, candidate_cap // max(1, limit))))
        for item in bands["low"]:
            if len(out["low"]) >= limit or total_left <= 0:
                break
            rec: Record = item["record"]
            in_ctx = rec.entity in ctx_set
            full = self._render(rec, item, canonical_aliases, in_ctx)
            content = C.clip_tokens(full, max(12, min(per, candidate_cap)))
            hit = rec.to_hit(band="low", confidence=item["confidence"])
            hit.in_context = in_ctx
            self._finish_hit(hit, content, item)
            cost = rendered_cost(hit)
            if cost > candidate_cap or cost > total_left:
                break
            out["low"].append(hit)
            total_left -= cost
            candidate_cap -= cost
        used_low = sum(rendered_cost(h) for h in out["low"])
        trace.append(f"budget/low: {used_low}/{caps['low']} tokens（抽样 {len(out['low'])} 条）")
        traced = sum(rendered_cost(h) for h in (*out["high"], *out["medium"], *out["low"]))
        trace.append(f"budget/total: {traced}/{budget} tokens")
        return out

    def _render(
        self,
        rec: Record,
        item: dict[str, Any],
        canonical_aliases: dict[str, list[str]],
        in_ctx: bool,
    ) -> str:
        """渲染一条命中的 content。

        * 已在上下文 -> 交回 ``Hit.to_dict`` 的 ``"已在上下文"``（正文不重复返回）；
        * 纯别名命中 -> 前缀别名，如 ``（炎帝）``，让读者知道为什么被召回；
        * 其他 -> 原样返回记录 content。
        """
        if in_ctx:
            return rec.content  # to_dict 会渲染成 "已在上下文"
        alias_hits = [c.split(":", 1)[1] for c in item["channels"] if c.startswith("alias:")]
        if alias_hits:
            return f"（{'/'.join(alias_hits)}）{rec.content}"
        return rec.content

    @staticmethod
    def _finish_hit(hit: Hit, content: str, item: dict[str, Any]) -> None:
        """把分层过程产出的通道/正文写回 Hit（Hit 是可见 dataclass）。"""
        hit.content = content
        hit.channels = tuple(item["channels"])

    # ------------------------------------------------------------------
    # 步骤 7：low 抽样去重（同一写作周期内不重复抽）
    # ------------------------------------------------------------------
    def _apply_seen_low(
        self,
        buckets: dict[str, list[Hit]],
        bands: dict[str, list[dict[str, Any]]],
        seen_low: set[tuple[str, str]] | None,
        ctx_set: set[str],
        canonical_aliases: dict[str, list[str]],
        trace: list[str],
    ) -> None:
        """从 low 候选池里做"轮转式"抽样，同一周期内不重复抽。

        * ``seen_low is None`` -> 无状态调用，保留 ``_allocate`` 已抽的 1~2 条；
        * 传入集合 -> 剔除本轮已抽中但本周期已出现过的条目，再从候选池里按顺序
          （"最少出现次数"优先，同次数按分数降序）补位，并把最终抽中的 key 写回集合。

        这样：候选数充足时每轮都是新条目；候选耗尽时按出现次数轮转，
        既不空手而归，也保证"不重复抽"的语义在可满足范围内成立。
        """
        if seen_low is None:
            return
        pool = bands.get("low", [])
        if not pool:
            trace.append("seen_low: 无 low 候选")
            return
        token_budget = sum(rendered_cost(h) for h in buckets["low"]) or _LOW_SAMPLE_TOKENS
        limit = max(len(buckets["low"]), 1)
        # 轮转队列：没见过的最先，其次按候选排序位次（同分数时与上一轮错开）
        order = sorted(range(len(pool)), key=lambda i: (pool[i]["key"] in seen_low, i))
        chosen: list[Hit] = []
        left = token_budget
        for idx in order:
            if len(chosen) >= limit or left <= 0:
                break
            item = pool[idx]
            rec: Record = item["record"]
            in_ctx = rec.entity in ctx_set
            full = self._render(rec, item, canonical_aliases, in_ctx)
            content = C.clip_tokens(full, max(12, min(_LOW_CLIP_MAX, left)))
            hit = rec.to_hit(band="low", confidence=item["confidence"])
            hit.in_context = in_ctx
            hit.content = content
            hit.channels = tuple(item["channels"])
            cost = rendered_cost(hit)
            if cost > left:
                break
            chosen.append(hit)
            left -= cost
        buckets["low"] = chosen
        reused = sum(1 for h in chosen if (h.layer, h.entity) in seen_low)
        seen_low.update((h.layer, h.entity) for h in chosen)
        trace.append(
            f"seen_low: 抽中 {len(chosen)}/{len(pool)} 条 low（全新 {len(chosen) - reused} 条）"
        )


def search_memory(
    chest: Any,
    req: dict[str, Any],
    *,
    context_entities: Sequence[str] = (),
    seen_low: set[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """模块级便捷函数：内部建一个 ``MemoryKeeper`` 并执行检索。

    与 ``MemoryKeeper.search`` 完全等价，供 function-calling 直接挂载。
    """
    keeper = MemoryKeeper(chest)
    return keeper.search(req, context_entities=context_entities, seen_low=seen_low)
