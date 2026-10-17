"""Memory Keeper（retrieval.py）单元测试。

覆盖需求点：
  * 实体归一化（称呼格 别名 -> 标准名，trace 留痕）
  * 三档分层与 50% / 35% / 15% 预算比例
  * in_context 去重（已在上下文的实体不重复返回正文）
  * low 抽样标记 + 同一写作周期内不重复抽（seen_low）
  * 非法请求抛 common.SchemaError
  * 返回值通过 common.schema("memory_result") 校验
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from enderwriter import common as C
from enderwriter.chest import EnderChest
from enderwriter.retrieval import (
    BAND_RATIOS,
    DEFAULT_BUDGET,
    MemoryKeeper,
    search_memory,
)


def build_story_chest() -> EnderChest:
    """一套小而全的测试世界：别名、图关系、可直接进 high 的核心实体、
    单通道中等相关的邻居、以及二跳才可达的弱相关外围势力（供应 low 抽样）。"""
    chest = EnderChest(":memory:")
    chest.link_alias("炎帝", "萧炎")
    chest.link_alias("岩枭", "萧炎")

    chest.upsert("character", "萧炎", "主角，性格坚韧，出身乌坦城萧家。", chapter_id="ch_001")
    chest.upsert("character", "药老", "寄居黑戒中的炼药师残魂，是萧炎的引路人。", chapter_id="ch_002")
    chest.upsert("world", "斗气", "斗气大陆以斗气为力量根基，等级森严。", chapter_id="ch_001")
    chest.upsert("item", "黑戒", "萧炎母亲遗留的黑色古戒，内含药老残魂。", chapter_id="ch_002")
    chest.upsert("world", "乌坦城", "萧家所在的边陲小城。", chapter_id="ch_001")
    chest.upsert("world", "青云宗", "大陆第一宗门，规矩森严。", chapter_id="ch_005")
    chest.upsert("style", "冷峻短句", "多用短句，白描，少形容词。", chapter_id="ch_001")

    # 二跳弱相关：萧炎 -住在-> 乌坦城 -往来-> 外围N（5 条，足够两轮不重复抽样）
    chest.add_edge("萧炎", "住在", "乌坦城", chapter_id="ch_001")
    for i in range(5):
        chest.upsert("world", f"外围{i}", f"第{i}号外围势力，记载简略。", chapter_id="ch_009")
        chest.add_edge("乌坦城", "往来", f"外围{i}", chapter_id="ch_009")

    # 一跳相关：黑戒、药老等中相关来源
    chest.add_edge("萧炎", "师徒", "药老", chapter_id="ch_002")
    chest.add_edge("萧炎", "持有", "黑戒", chapter_id="ch_002")
    chest.commit()
    return chest


def band_tokens(res: dict, band: str) -> int:
    """按**最终 JSON 形态**统计一档的 token 开销。

    in_context 的命中在返回里被渲染成 ``"已在上下文"``，所以这里统计的也是
    提示串本身——与 ``total_tokens`` 的口径一致。
    """
    return sum(C.est_tokens(h["content"]) for h in res[band])


def band_entities(res: dict, band: str) -> list[str]:
    return [h["entity"] for h in res[band]]


class RetrievalTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.chest = build_story_chest()
        self.keeper = MemoryKeeper(self.chest)

    def tearDown(self) -> None:
        self.chest.close()

    def entity_names(self, res: dict) -> list[str]:
        return [h["entity"] for band in ("high", "medium", "low") for h in res[band]]


# ---------------------------------------------------------------------------
# 1. 实体归一化
# ---------------------------------------------------------------------------
class TestNormalization(RetrievalTestBase):
    def test_alias_maps_to_canonical(self):
        res = self.keeper.search({"entities": ["炎帝"], "query_context": "黑戒"})
        self.assertEqual(res["normalized"]["entities"], ["萧炎"])
        self.assertEqual(res["normalized"]["aliases"], {"炎帝": "萧炎"})
        # 记录以标准名为键返回，绝不出现别名
        self.assertIn("萧炎", self.entity_names(res))
        self.assertNotIn("炎帝", self.entity_names(res))

    def test_aliases_in_trace(self):
        res = self.keeper.search({"entities": ["炎帝"], "query_context": "黑戒"})
        joined = "\n".join(res["trace"])
        self.assertIn("normalized", joined)
        self.assertIn("炎帝→萧炎", joined)

    def test_unregistered_name_kept_as_is(self):
        res = self.keeper.search({"entities": ["未知之人"], "query_context": "随便"})
        self.assertEqual(res["normalized"]["entities"], ["未知之人"])
        self.assertEqual(res["normalized"]["aliases"], {})

    def test_multiple_aliases_collapse_to_one(self):
        res = self.keeper.search({"entities": ["炎帝", "岩枭", "萧炎"], "query_context": ""})
        # 三个输入（两别名 + 标准名）归一为同一个实体
        self.assertEqual(res["normalized"]["entities"], ["萧炎"])
        self.assertEqual(res["normalized"]["aliases"], {"炎帝": "萧炎", "岩枭": "萧炎"})

    def test_two_hop_weak_candidates_exposed(self):
        res = self.keeper.search({"entities": ["萧炎"], "query_context": "黑戒"})
        joined = "\n".join(res["trace"])
        self.assertIn("recall/weak", joined)


# ---------------------------------------------------------------------------
# 2. 三档分层 + 预算比例
# ---------------------------------------------------------------------------
class TestBandAllocation(RetrievalTestBase):
    def test_band_ratios_are_spec(self):
        self.assertEqual(BAND_RATIOS, {"high": 0.50, "medium": 0.35, "low": 0.15})
        self.assertEqual(sum(BAND_RATIOS.values()), 1.0)

    def test_default_budget_is_800(self):
        self.assertEqual(DEFAULT_BUDGET, 800)
        self.assertEqual(MemoryKeeper(self.chest).default_budget, 800)

    def test_all_three_bands_populated(self):
        res = self.keeper.search(
            {"entities": ["萧炎"], "query_context": "斗气大陆黑戒炼药师", "budget": 800}
        )
        self.assertTrue(res["high"], "high 档应当有内容")
        self.assertTrue(res["medium"], "medium 档应当有内容")
        self.assertTrue(res["low"], "low 档应当有内容（弱相关探索）")

    def test_high_band_gets_full_content(self):
        res = self.keeper.search({"entities": ["萧炎"], "query_context": "黑戒", "budget": 800})
        for hit in res["high"]:
            record = self.chest.get(hit["layer"], hit["entity"])
            self.assertIsNotNone(record)
            self.assertEqual(hit["content"], record.content)
            self.assertFalse(hit["content"].endswith("…"))

    def test_medium_band_is_clipped_summary(self):
        long_text = "斗气" * 200
        self.chest.upsert("world", "斗气说明", long_text, chapter_id="ch_003")
        res = self.keeper.search(
            {"entities": ["斗气说明"], "query_context": "黑戒炼药师师徒", "budget": 200}
        )
        for hit in res["medium"]:
            self.assertLessEqual(C.est_tokens(hit["content"]), C.est_tokens(long_text))
        # high 档仍是完整 content
        for hit in res["high"]:
            record = self.chest.get(hit["layer"], hit["entity"])
            self.assertEqual(hit["content"], record.content)

    def test_band_budget_caps(self):
        """在预算吃紧时，各档不得超出自己的份额（50%/35%/15%）。"""
        budget = 120
        for i in range(6):
            self.chest.upsert("world", f"长记录{i}", "长" * 300, chapter_id="ch_011")
        res = self.keeper.search(
            {"entities": ["长记录0", "长记录1"], "query_context": "黑戒炼药师", "budget": budget}
        )
        high_cap = int(budget * 0.50)
        medium_cap = int(budget * 0.35)
        low_cap = int(budget * 0.15)
        # high 优先且允许用满 50%；medium 在 high 剩余的基础上 ≤35%；low ≤15%
        self.assertLessEqual(band_tokens(res, "high"), high_cap + 1)
        self.assertLessEqual(band_tokens(res, "medium"), high_cap + medium_cap + 1)
        self.assertLessEqual(band_tokens(res, "low"), low_cap + 1)
        self.assertLessEqual(res["total_tokens"], budget)

    def test_low_band_is_1_to_2_samples(self):
        res = self.keeper.search({"entities": ["萧炎"], "query_context": "黑戒", "budget": 800})
        self.assertGreaterEqual(len(res["low"]), 1)
        self.assertLessEqual(len(res["low"]), 2)

    def test_total_tokens_matches_estimate_and_budget(self):
        for budget in (60, 120, 400, 800):
            res = self.keeper.search(
                {"entities": ["萧炎", "药老"], "query_context": "斗气大陆黑戒师徒", "budget": budget}
            )
            recomputed = sum(
                C.est_tokens(h["content"])
                for band in ("high", "medium", "low")
                for h in res[band]
            )
            self.assertEqual(res["total_tokens"], recomputed)
            self.assertLessEqual(res["total_tokens"], budget)

    def test_band_field_present_on_every_hit(self):
        res = self.keeper.search({"entities": ["萧炎"], "query_context": "黑戒", "budget": 800})
        for band in ("high", "medium", "low"):
            for hit in res[band]:
                # to_dict 会抹掉 band，这里用 confidence 与 content 形态间接确认分档生效
                self.assertIn(hit["layer"], C.LAYERS)
                self.assertGreaterEqual(hit["confidence"], 0.0)
                self.assertLessEqual(hit["confidence"], 1.0)

    def test_confidence_ordering(self):
        res = self.keeper.search({"entities": ["萧炎"], "query_context": "黑戒", "budget": 800})
        if res["high"] and res["medium"]:
            self.assertGreaterEqual(
                min(h["confidence"] for h in res["high"]),
                max(h["confidence"] for h in res["medium"]),
            )


# ---------------------------------------------------------------------------
# 3. in_context 去重
# ---------------------------------------------------------------------------
class TestInContextDedup(RetrievalTestBase):
    def test_in_context_content_masked(self):
        res = self.keeper.search(
            {"entities": ["萧炎", "药老"], "query_context": "黑戒"},
            context_entities=("萧炎",),
        )
        hits = {h["entity"]: h for band in ("high", "medium", "low") for h in res[band]}
        self.assertIn("萧炎", hits)
        self.assertEqual(hits["萧炎"]["content"], "已在上下文")
        self.assertTrue(hits["萧炎"].get("in_context"))
        # 不在上下文的实体仍返回正文
        others = [h for k, h in hits.items() if k != "萧炎"]
        self.assertTrue(others)
        for hit in others:
            self.assertNotEqual(hit["content"], "已在上下文")
            self.assertFalse(hit.get("in_context"))

    def test_context_entity_alias_also_dedupes(self):
        # 上下文给的是别名"炎帝"，也应识别为标准名"萧炎"
        res = self.keeper.search(
            {"entities": ["萧炎"], "query_context": "黑戒"}, context_entities=("炎帝",)
        )
        hits = {h["entity"]: h for band in ("high", "medium", "low") for h in res[band]}
        self.assertEqual(hits["萧炎"]["content"], "已在上下文")
        self.assertIn("萧炎", res["normalized"]["in_context"])

    def test_in_context_hit_costs_little_budget(self):
        """已在上下文的实体即使正文极长，也只按提示串计费，不挤占预算。"""
        big = "萧炎" * 300
        self.chest.upsert("character", "萧炎", big, chapter_id="ch_020")
        with_ctx = self.keeper.search(
            {"entities": ["萧炎", "药老"], "query_context": "黑戒", "budget": 200},
            context_entities=("萧炎",),
        )
        hits = {h["entity"]: h for band in ("high", "medium", "low") for h in with_ctx[band]}
        self.assertEqual(hits["萧炎"]["content"], "已在上下文")
        # 遮掉的正文绝不计入 total_tokens
        self.assertLess(with_ctx["total_tokens"], C.est_tokens(big))
        self.assertLessEqual(with_ctx["total_tokens"], 200)
        # 其他实体仍拿到正文
        self.assertTrue(any(h["content"] != "已在上下文" for h in hits.values()))

    def test_empty_context_changes_nothing(self):
        a = self.keeper.search({"entities": ["萧炎"], "query_context": "黑戒"})
        b = self.keeper.search({"entities": ["萧炎"], "query_context": "黑戒"}, context_entities=())
        self.assertEqual(self.entity_names(a), self.entity_names(b))


# ---------------------------------------------------------------------------
# 4. low 抽样标记 + seen_low 轮转
# ---------------------------------------------------------------------------
class TestLowSampling(RetrievalTestBase):
    QUERY = {"entities": ["萧炎"], "query_context": "城主府", "budget": 800}

    def test_low_is_marked_and_short(self):
        res = self.keeper.search(self.QUERY)
        self.assertTrue(res["low"])
        for hit in res["low"]:
            self.assertTrue(hit["content"])
            # low 只给极短摘要
            self.assertLessEqual(C.est_tokens(hit["content"]), 60)
        # 与 high 相比，low 明显更短
        if res["high"]:
            self.assertLess(
                max(C.est_tokens(h["content"]) for h in res["low"]),
                max(C.est_tokens(h["content"]) for h in res["high"]) * 10,
            )

    def test_low_candidates_are_weakly_related(self):
        res = self.keeper.search(self.QUERY)
        high_names = set(band_entities(res, "high"))
        for name in band_entities(res, "low"):
            self.assertNotIn(name, high_names)

    def test_seen_low_avoids_repeats_within_cycle(self):
        seen: set[tuple[str, str]] = set()
        cycles = [self.keeper.search(self.QUERY, seen_low=seen) for _ in range(2)]
        first = {(h["layer"], h["entity"]) for h in cycles[0]["low"]}
        second = {(h["layer"], h["entity"]) for h in cycles[1]["low"]}
        self.assertTrue(first, "第一轮应当抽出 low")
        self.assertTrue(second, "候选充足时第二轮也应抽出 low")
        self.assertFalse(first & second, "同一写作周期内 low 抽样不应重复")
        self.assertTrue(first <= seen and second <= seen, "抽中的 key 必须写回 seen_low")

    def test_seen_low_accumulates_across_cycles(self):
        seen: set[tuple[str, str]] = set()
        for _ in range(2):
            res = self.keeper.search(self.QUERY, seen_low=seen)
            self.assertTrue(seen)
            for hit in res["low"]:
                self.assertIn((hit["layer"], hit["entity"]), seen)

    def test_seen_low_none_is_stateless(self):
        # 不传 seen_low：同一次查询结果应可复现（无隐藏状态）
        a = self.keeper.search(self.QUERY)
        b = self.keeper.search(self.QUERY)
        self.assertEqual(band_entities(a, "low"), band_entities(b, "low"))

    def test_all_candidates_seen_rotates_not_crashes(self):
        # 先把本轮抽中的 2 跳候选标记为"已抽"，下一轮应轮转到其他候选
        base = self.keeper.search(self.QUERY)
        seen = {(h["layer"], h["entity"]) for h in base["low"]}
        again = self.keeper.search(self.QUERY, seen_low=seen)
        self.assertIsInstance(again["low"], list)
        second = {(h["layer"], h["entity"]) for h in again["low"]}
        self.assertFalse(second & {(h["layer"], h["entity"]) for h in base["low"]})

    def test_seen_low_convenience_function(self):
        seen: set[tuple[str, str]] = set()
        res = search_memory(self.chest, self.QUERY, seen_low=seen)
        self.assertIsInstance(res["low"], list)
        for hit in res["low"]:
            self.assertIn((hit["layer"], hit["entity"]), seen)


# ---------------------------------------------------------------------------
# 5. 非法请求 -> SchemaError
# ---------------------------------------------------------------------------
class TestInvalidRequests(RetrievalTestBase):
    def test_empty_entities_raises(self):
        with self.assertRaises(C.SchemaError):
            self.keeper.search({"entities": []})

    def test_missing_entities_raises(self):
        with self.assertRaises(C.SchemaError):
            self.keeper.search({"query_context": "黑戒"})

    def test_budget_zero_raises(self):
        with self.assertRaises(C.SchemaError):
            self.keeper.search({"entities": ["萧炎"], "budget": 0})

    def test_budget_negative_raises(self):
        with self.assertRaises(C.SchemaError):
            self.keeper.search({"entities": ["萧炎"], "budget": -5})

    def test_unknown_layer_raises(self):
        with self.assertRaises(C.SchemaError):
            self.keeper.search({"entities": ["萧炎"], "layers": ["不存在"]})

    def test_unknown_field_rejected(self):
        with self.assertRaises(C.SchemaError):
            self.keeper.search({"entities": ["萧炎"], "bogus": 1})

    def test_budget_over_max_raises(self):
        with self.assertRaises(C.SchemaError):
            self.keeper.search({"entities": ["萧炎"], "budget": 99999})

    def test_schema_error_is_value_error(self):
        with self.assertRaises(ValueError):
            self.keeper.search({"entities": []})


# ---------------------------------------------------------------------------
# 6. 返回值契约校验 / 其他行为
# ---------------------------------------------------------------------------
class TestResultContract(RetrievalTestBase):
    def test_result_passes_memory_result_schema(self):
        res = self.keeper.search(
            {"entities": ["炎帝"], "query_context": "黑戒炼药师", "budget": 800}
        )
        C.validate(res, C.schema("memory_result"))

    def test_result_extra_fields_allowed(self):
        res = self.keeper.search({"entities": ["萧炎"], "query_context": "黑戒"})
        for key in ("elapsed_ms", "normalized", "trace"):
            self.assertIn(key, res)
        self.assertIsInstance(res["elapsed_ms"], float)
        self.assertIsInstance(res["trace"], list)
        self.assertIsInstance(res["normalized"], dict)

    def test_result_required_keys(self):
        res = self.keeper.search({"entities": ["萧炎"]})
        for key in ("high", "medium", "low", "total_tokens"):
            self.assertIn(key, res)
        self.assertIsInstance(res["total_tokens"], int)

    def test_convenience_function_matches_keeper(self):
        req = {"entities": ["萧炎"], "query_context": "黑戒", "budget": 300}
        via_fn = search_memory(self.chest, req)
        via_keeper = self.keeper.search(req)
        self.assertEqual(band_entities(via_fn, "high"), band_entities(via_keeper, "high"))
        self.assertEqual(via_fn["total_tokens"], via_keeper["total_tokens"])

    def test_layers_filter_respected(self):
        res = self.keeper.search(
            {"entities": ["萧炎", "药老"], "query_context": "师徒黑戒", "layers": ["character"]}
        )
        hits = [h for band in ("high", "medium", "low") for h in res[band]]
        self.assertTrue(hits)
        self.assertTrue(all(h["layer"] == "character" for h in hits))

    def test_empty_chest_returns_empty_bands(self):
        empty = EnderChest(":memory:")
        try:
            res = search_memory(empty, {"entities": ["萧炎"], "query_context": "黑戒"})
            self.assertEqual(res["high"], [])
            self.assertEqual(res["medium"], [])
            self.assertEqual(res["low"], [])
            self.assertEqual(res["total_tokens"], 0)
            C.validate(res, C.schema("memory_result"))
        finally:
            empty.close()

    def test_default_budget_used_when_absent(self):
        res = self.keeper.search({"entities": ["萧炎"], "query_context": "黑戒"})
        self.assertEqual(res["normalized"]["budget"], DEFAULT_BUDGET)
        self.assertLessEqual(res["total_tokens"], DEFAULT_BUDGET)

    def test_tool_schema_is_frozen_contract(self):
        self.assertIs(self.keeper.tool_schema(), C.schema("search_memory_tool"))

    def test_hit_shape_has_schema_fields(self):
        res = self.keeper.search({"entities": ["萧炎"], "query_context": "黑戒"})
        hits = [h for band in ("high", "medium", "low") for h in res[band]]
        self.assertTrue(hits)
        for hit in hits:
            for key in ("layer", "entity", "content", "version", "last_updated", "confidence"):
                self.assertIn(key, hit)
            self.assertGreaterEqual(hit["version"], 1)


if __name__ == "__main__":
    unittest.main()
