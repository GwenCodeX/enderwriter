"""冻结层与末影箱的冒烟测试（stdlib unittest，离线可跑）。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from enderwriter import common as C
from enderwriter import embed as E
from enderwriter import lexicon as L
from enderwriter.chest import EnderChest


class TestCommon(unittest.TestCase):
    def test_token_estimation(self):
        self.assertEqual(C.est_tokens(""), 0)
        self.assertEqual(C.est_tokens("萧炎"), 2)
        self.assertLessEqual(C.est_tokens(C.clip_tokens("测" * 200, 10)), 10)

    def test_ids(self):
        self.assertEqual(C.make_id("fs", 3), "fs_003")
        self.assertEqual(C.chapter_index("ch_042"), 42)
        self.assertEqual(C.chapter_index("nope"), -1)
        self.assertLess(C.chapter_sort_key("ch_002"), C.chapter_sort_key("ch_010"))

    def test_json_helpers(self):
        self.assertEqual(C.loads(C.dumps({"b": 1, "a": 2})), {"a": 2, "b": 1})
        self.assertEqual(C.normalize_name("  萧 炎 "), "萧炎")
        self.assertEqual(C.dedupe(["a", "a", "b"]), ["a", "b"])


class TestSchemas(unittest.TestCase):
    def test_outline_ok(self):
        C.validate(
            {
                "chapter_id": "ch_042",
                "chapter_goal": "初入宗门",
                "active_characters": [{"name": "萧炎", "importance": "core"}],
                "locations": ["乌坦城"],
                "items": [{"name": "黑戒", "importance": "core"}],
                "foreshadowing_to_resolve": [{"id": "fs_001"}],
                "foreshadowing_to_plant": [{"content": "黑戒", "planned_resolve": "ch_055"}],
                "mood": "紧张",
                "style_keywords": ["冷峻", "短句"],
            },
            C.schema("chapter_outline"),
        )

    def test_outline_missing_field_rejected(self):
        with self.assertRaises(C.SchemaError):
            C.validate({"chapter_id": "ch_042", "chapter_goal": "x"}, C.schema("chapter_outline"))

    def test_outline_bad_id_rejected(self):
        with self.assertRaises(C.SchemaError):
            C.validate(
                {
                    "chapter_id": "42",
                    "chapter_goal": "x",
                    "active_characters": [],
                    "locations": [],
                    "items": [],
                    "foreshadowing_to_resolve": [],
                    "foreshadowing_to_plant": [],
                    "mood": "紧张",
                    "style_keywords": [],
                },
                C.schema("chapter_outline"),
            )

    def test_writeback_report(self):
        C.validate(
            {
                "chapter_id": "ch_042",
                "updated": {
                    "characters": ["萧炎"],
                    "items": [],
                    "timeline": ["ch_042"],
                    "foreshadowing": [{"id": "fs_001", "status": "resolved"}],
                },
                "warnings": [],
            },
            C.schema("writeback_report"),
        )
        with self.assertRaises(C.SchemaError):
            C.validate(
                {
                    "chapter_id": "ch_042",
                    "updated": {"characters": [], "items": [], "timeline": [],
                                "foreshadowing": [{"id": "fs_001", "status": "bogus"}]},
                    "warnings": [],
                },
                C.schema("writeback_report"),
            )

    def test_tool_ok_and_rejects(self):
        C.validate(
            {"entities": ["萧炎"], "query_context": "宗门", "layers": ["character"], "budget": 800},
            C.schema("search_memory_tool"),
        )
        for bad in ({"entities": []}, {"entities": ["a"], "budget": 0}):
            with self.assertRaises(C.SchemaError):
                C.validate(bad, C.schema("search_memory_tool"))

    def test_memory_result_ref(self):
        C.validate(
            {
                "high": [
                    {
                        "layer": "character",
                        "entity": "萧炎",
                        "content": "主角",
                        "version": 1,
                        "last_updated": "2024-01-01T00:00:00",
                        "confidence": 0.9,
                    }
                ],
                "medium": [],
                "low": [],
                "total_tokens": 12,
            },
            C.schema("memory_result"),
        )
        with self.assertRaises(C.SchemaError):
            C.validate({"high": [{"layer": "character"}], "medium": [], "low": [], "total_tokens": 0},
                       C.schema("memory_result"))

    def test_four_schemas(self):
        self.assertEqual(
            sorted(C.SCHEMAS),
            ["chapter_outline", "memory_result", "search_memory_tool", "writeback_report"],
        )


class TestLexicon(unittest.TestCase):
    def test_cjk_bigrams(self):
        terms = L.analyze("萧炎走进了宗门")
        self.assertIn("萧炎", terms)
        self.assertIn("宗门", terms)

    def test_latin(self):
        terms = L.analyze("Use Qwen3B model")
        self.assertIn("qwen3b", terms)
        self.assertIn("use", terms)
        self.assertNotIn("qwen", terms)  # 拉丁词整体成词，不再切分

    def test_weights(self):
        w = L.term_weights("萧炎之戒", entity="萧炎", title="黑戒")
        self.assertGreaterEqual(w.get("萧炎", 0.0), 3.0)

    def test_extract_longest_first(self):
        self.assertEqual(
            L.extract_entities("萧炎与药老同行", {"萧炎", "药老"}), ["萧炎", "药老"]
        )


class TestEmbed(unittest.TestCase):
    def test_vector(self):
        v = E.embed("萧炎")
        self.assertEqual(v.shape, (E.DIM,))
        self.assertAlmostEqual(float((v ** 2).sum()), 1.0, places=5)
        self.assertTrue((E.embed("萧炎") == v).all())

    def test_cosine(self):
        v = E.embed("萧炎")
        self.assertAlmostEqual(E.cosine(v, v), 1.0, places=5)
        self.assertLess(E.cosine(v, E.embed("量子力学")), 0.5)

    def test_blob_and_matrix(self):
        v = E.embed("萧炎")
        self.assertTrue((E.from_blob(E.to_blob(v)) == v).all())
        m = E.embed_many(["萧炎", "药老"])
        self.assertEqual(m.shape, (2, E.DIM))
        self.assertEqual(E.embed_many([]).shape, (0, E.DIM))


class TestChest(unittest.TestCase):
    def setUp(self):
        self.chest = EnderChest(":memory:")
        self.chest.link_alias("炎帝", "萧炎")
        self.chest.link_alias("岩枭", "萧炎")

    def tearDown(self):
        self.chest.close()

    def test_alias_and_versions(self):
        r1 = self.chest.upsert("character", "萧炎", "主角，斗气三段。", chapter_id="ch_001")
        self.assertEqual(r1.version, 1)
        r2 = self.chest.upsert("character", "炎帝", "晋升斗帝。", chapter_id="ch_030")
        self.assertEqual(r2.entity, "萧炎")
        self.assertEqual(r2.version, 2)
        hist = self.chest.history("character", "萧炎")
        self.assertEqual(len(hist), 2)
        self.assertTrue(hist[0].content.endswith("斗气三段。"))

    def test_state_latest_wins(self):
        self.chest.set_state("萧炎", "alive", True, chapter_id="ch_001")
        self.chest.set_state("萧炎", "realm", "斗气三段", chapter_id="ch_001")
        self.chest.set_state("萧炎", "realm", "斗帝", chapter_id="ch_030")
        self.assertEqual(self.chest.get_state("萧炎", "realm"), "斗帝")
        states = self.chest.states_of("萧炎")
        self.assertIs(states["alive"], True)
        self.assertEqual(self.chest.state_chapter("萧炎", "realm"), "ch_030")

    def test_foreshadow_lifecycle(self):
        fs = self.chest.set_foreshadow(
            "fs_001", "黑戒藏着药老残魂。", planted_chapter="ch_002", planned_resolve="ch_055"
        )
        self.assertTrue(fs.is_open)
        self.assertEqual(fs.planted_no, 2)
        fs = self.chest.reinforce_foreshadow("fs_001", "ch_010")
        self.assertEqual(fs.status, "reinforced")
        self.assertIn("ch_010", fs.reinforced_chapters)
        fs = self.chest.resolve_foreshadow("fs_001", "ch_055")
        self.assertEqual(fs.status, "resolved")
        self.assertEqual(fs.resolved_chapter, "ch_055")
        self.assertFalse(fs.is_open)
        self.assertEqual(self.chest.all_foreshadowings(only_open=True), [])
        self.assertEqual(self.chest.next_foreshadow_id(), "fs_002")

    def test_reinforce_missing_raises(self):
        with self.assertRaises(KeyError):
            self.chest.reinforce_foreshadow("fs_999", "ch_010")

    def test_invalid_layer(self):
        with self.assertRaises(ValueError):
            self.chest.upsert("nope", "x", "y")

    def test_chapters(self):
        for i in (1, 2, 3):
            self.chest.put_chapter(f"ch_{i:03d}", f"正文{i}", title=f"第{i}章", summary=f"s{i}")
        self.assertEqual([c["chapter_id"] for c in self.chest.recent_chapters(2)],
                         ["ch_002", "ch_003"])
        self.assertEqual([c["chapter_id"] for c in self.chest.recent_chapters(2, before="ch_003")],
                         ["ch_001", "ch_002"])
        self.assertEqual(self.chest.get_chapter("ch_001")["title"], "第1章")


class TestRetrievalIndexes(unittest.TestCase):
    def setUp(self):
        self.chest = EnderChest(":memory:")
        self.chest.link_alias("炎帝", "萧炎")
        self.chest.upsert("character", "萧炎", "主角，斗气三段，性格坚韧不拔。", chapter_id="ch_001")
        self.chest.upsert("character", "药老", "寄居黑戒中的炼药师残魂。", chapter_id="ch_002")
        self.chest.upsert("world", "斗气", "斗气大陆以斗气为力量根基。", chapter_id="ch_001")
        self.chest.add_edge("萧炎", "师徒", "药老", chapter_id="ch_002")

    def tearDown(self):
        self.chest.close()

    def test_search_terms(self):
        hits = self.chest.search_terms(L.analyze("萧炎"))
        self.assertTrue(hits)
        self.assertEqual(hits[0][0].entity, "萧炎")
        self.assertGreater(hits[0][1], 0.0)
        self.assertEqual(self.chest.search_terms([]), [])

    def test_search_vector(self):
        hits = self.chest.search_vector("斗气大陆的力量根基")
        self.assertTrue(hits)
        self.assertEqual(hits[0][0].layer, "world")
        self.assertLessEqual(hits[0][1], 1.0)
        self.assertEqual(self.chest.search_vector(""), [])

    def test_search_graph(self):
        hits = self.chest.search_graph(["萧炎"], hops=1)
        ents = {r.entity for r, _ in hits}
        self.assertIn("药老", ents)
        self.assertEqual(self.chest.search_graph([]), [])

    def test_search_layers_filter(self):
        hits = self.chest.search_terms(L.analyze("斗气"), layers=["character"])
        self.assertTrue(all(r.layer == "character" for r, _ in hits))

    def test_combined_search(self):
        hits = self.chest.search("萧炎与药老的师徒关系", entities=["萧炎"])
        self.assertTrue(hits)
        self.assertEqual(hits[0][0].entity, "萧炎")

    def test_detect_entities(self):
        found = self.chest.detect_entities("萧炎拿着黑戒，药老在戒中沉睡")
        self.assertIn("萧炎", found)
        self.assertIn("药老", found)

    def test_entity_vocab_has_alias(self):
        self.assertIn("炎帝", self.chest.entity_vocab())

    def test_multi_hop_reaches_deep_nodes(self):
        """回归：hops>=2 必须真的取到远端节点（曾有人误报 2 跳取不到第三层）。"""
        with EnderChest(":memory:") as ch:
            for n in ("甲", "乙", "丙", "丁"):
                ch.upsert("character", n, n + "的卡片")
            ch.add_edge("甲", "认识", "乙")
            ch.add_edge("乙", "认识", "丙")
            ch.add_edge("丙", "认识", "丁")

            one = {r.entity for r, _ in ch.search_graph(["甲"], hops=1)}
            two = {r.entity for r, _ in ch.search_graph(["甲"], hops=2)}
            three = {r.entity for r, _ in ch.search_graph(["甲"], hops=3)}

            self.assertEqual(one, {"甲", "乙"})
            self.assertEqual(two, {"甲", "乙", "丙"})
            self.assertEqual(three, {"甲", "乙", "丙", "丁"})
            sc = {r.entity: s for r, s in ch.search_graph(["甲"], hops=3)}
            self.assertGreater(sc["甲"], sc["乙"])
            self.assertGreater(sc["乙"], sc["丙"])
            self.assertGreater(sc["丙"], sc["丁"])

    def test_next_chapter_id(self):
        with EnderChest(":memory:") as ch:
            self.assertEqual(ch.next_chapter_id(), "ch_001")
            ch.put_chapter("ch_001", "正文一")
            ch.put_chapter("ch_007", "正文七")
            self.assertEqual(ch.next_chapter_id(), "ch_008")
            ch.upsert("character", "萧炎", "设定", chapter_id="ch_012")
            self.assertEqual(ch.next_chapter_id(), "ch_013")

    def test_foreshadow_reinforce_persists(self):
        """回归：set_foreshadow 的 extra 不得覆盖显式写入的强化/回收字段。"""
        with EnderChest(":memory:") as ch:
            ch.set_foreshadow("fs_007", "残灯照见回溯。", planted_chapter="ch_001", planned_resolve="ch_010")
            ch.reinforce_foreshadow("fs_007", "ch_004")
            fs = ch.all_foreshadowings()[0]
            self.assertEqual(fs.reinforced_chapters, ["ch_004"])
            ch.resolve_foreshadow("fs_007", "ch_009")
            fs = ch.all_foreshadowings()[0]
            self.assertEqual(fs.status, "resolved")
            self.assertEqual(fs.resolved_chapter, "ch_009")
            self.assertEqual(fs.reinforced_chapters, ["ch_004"])


class TestChestStats(unittest.TestCase):
    def test_stats_and_history_rows(self):
        with EnderChest(":memory:") as chest:
            chest.upsert("character", "萧炎", "一", chapter_id="ch_001")
            chest.upsert("character", "萧炎", "二", chapter_id="ch_002")
            chest.put_chapter("ch_001", "正文", summary="s")
            st = chest.stats()
            self.assertEqual(st["records"]["character"], 1)
            self.assertEqual(st["total_records"], 1)
            self.assertEqual(st["history"], 2)
            self.assertEqual(st["chapters"], 1)
            self.assertEqual(st["open_foreshadowings"], 0)
            self.assertGreater(st["terms"], 0)

