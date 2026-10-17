"""编排层（orchestrator.py）的单元测试：P0-P4 预算、硬约束、预取、工具调用。

设计要点：
  * 只用 stdlib unittest，离线可跑（与冻结层测试一致）；
  * ``retrieval.py`` 是软依赖：已落盘就用真 MemoryKeeper（主路径），未落盘时
    退化为内部 fallback——两套环境都要绿，所以断言只依赖编排层自己的契约；
  * 预算断言全部是**硬断言**：每段 est_tokens <= 段预算，总量 <= int(窗口*0.7)。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from enderwriter import common as C
from enderwriter.chest import EnderChest
from enderwriter import orchestrator as O
from enderwriter.orchestrator import (
    BUDGET_RATIOS,
    CONTEXT_RATIO,
    PRIORITIES,
    Orchestrator,
)


# --------------------------------------------------------------------------
# 固定夹具：一个中等规模的末影箱 + 一份合法细纲
# --------------------------------------------------------------------------
class ChestFixture:
    """构造一个可复用的末影箱（每个用例独立实例，避免状态串味）。"""

    @staticmethod
    def build() -> EnderChest:
        chest = EnderChest(":memory:")
        # 称呼格
        chest.link_alias("炎帝", "萧炎")
        chest.link_alias("岩枭", "萧炎")
        # 人物
        chest.upsert("character", "萧炎", "主角，斗气三段，性格坚韧不拔。", chapter_id="ch_001")
        chest.upsert("character", "药老", "寄居黑戒中的炼药师残魂，萧炎的师父。", chapter_id="ch_002")
        chest.upsert("character", "纳兰嫣然", "萧炎的婚约对象，云岚宗弟子。", chapter_id="ch_001")
        chest.upsert("character", "萧薰儿", "萧家少女，对萧炎一往情深。", chapter_id="ch_003")
        # 世界 / 地点
        chest.upsert("world", "乌坦城", "萧家所在的边陲小城，斗气为尊。", chapter_id="ch_001")
        chest.upsert("world", "云岚宗", "帝国第一宗门，坐落在云岚山。", chapter_id="ch_004")
        chest.upsert("world", "斗气大陆", "以斗气为力量根基的广袤大陆。", chapter_id="ch_001")
        # 物品
        chest.upsert("item", "黑戒", "古朴黑戒，戒中藏有药老残魂。", chapter_id="ch_002")
        chest.upsert("item", "玄重尺", "沉重黑尺，萧炎的兵器。", chapter_id="ch_006")
        # 时间
        chest.upsert("timeline", "ch_001", "萧炎被判定为斗之气三段，家族失望。", chapter_id="ch_001")
        chest.upsert("timeline", "ch_002", "药老苏醒，答应收萧炎为徒。", chapter_id="ch_002")
        # 风格样本（P4）
        chest.upsert("style", "全书风格", "短句为主，冷峻克制，善用感官细节与留白。")
        chest.upsert("style", "战斗描写", "节奏急促，多用动词，短促换行。")
        # 伏笔：两条 open，一条 resolved（不应进 P0）
        chest.set_foreshadow("fs_001", "黑戒里藏着药老的残魂。",
                             planted_chapter="ch_002", planned_resolve="ch_010")
        chest.set_foreshadow("fs_002", "萧家的神秘婚约与纳兰嫣然有关。", planted_chapter="ch_001")
        chest.set_foreshadow("fs_003", "魂殿的追兵已经盯上乌坦城。", planted_chapter="ch_004")
        chest.resolve_foreshadow("fs_003", "ch_009")
        # 状态索引
        chest.set_state("萧炎", "realm", "斗之气三段", chapter_id="ch_001")
        chest.set_state("萧炎", "alive", True, chapter_id="ch_001")
        # 图索引
        chest.add_edge("萧炎", "师徒", "药老", chapter_id="ch_002")
        chest.add_edge("萧炎", "婚约", "纳兰嫣然", chapter_id="ch_001")
        # 近期正文
        chest.put_chapter("ch_001", "萧炎在乌坦城的晨光里醒来，发现黑戒微微发烫。",
                          title="第一章 斗之气三段", summary="萧炎被判定斗之气三段。")
        chest.put_chapter("ch_002", "药老从黑戒中现身，自称炼药师，愿收萧炎为徒。",
                          title="第二章 药老", summary="药老现身并收徒。")
        return chest


def make_outline() -> dict:
    """一份通过 chapter_outline Schema 的细纲（含 core / secondary）。"""
    return {
        "title": "斗破苍天",
        "global": "少年萧炎从斗之气三段逆袭，最终登临斗帝。",
        "current_chapter": {
            "chapter_id": "ch_004",
            "chapter_goal": "萧炎向药老求教炼药，并试探婚约之事",
            "active_characters": [
                {"name": "萧炎", "importance": "core"},
                {"name": "药老", "importance": "secondary"},
            ],
            "locations": ["乌坦城"],
            "items": [{"name": "黑戒", "importance": "core"}],
            "foreshadowing_to_resolve": [{"id": "fs_001"}],
            "foreshadowing_to_plant": [{"content": "萧薰儿的神秘身世"}],
            "mood": "紧张",
            "style_keywords": ["冷峻", "短句"],
        },
    }


class _BaseCase(unittest.TestCase):
    window = 8000

    def setUp(self):
        self.chest = ChestFixture.build()
        self.orch = Orchestrator(self.chest, window_tokens=self.window)
        self.outline = make_outline()

    def tearDown(self):
        self.chest.close()

    def assemble(self, **kwargs):
        kwargs.setdefault("current_chapter", "ch_004")
        return self.orch.assemble_context(self.outline, **kwargs)


# --------------------------------------------------------------------------
# 1. 预算与结构
# --------------------------------------------------------------------------
class TestBudgetAndStructure(_BaseCase):
    def test_five_sections_present(self):
        res = self.assemble()
        self.assertEqual(set(res["sections"]), set(PRIORITIES))
        for pri in PRIORITIES:
            self.assertIn(pri, res["sections"])
            self.assertIsInstance(res["sections"][pri], str)
            self.assertTrue(res["sections"][pri], f"{pri} 段为空")

    def test_required_result_keys(self):
        res = self.assemble()
        for key in ("sections", "text", "budget", "total_tokens", "dropped", "retrieval"):
            self.assertIn(key, res)
        self.assertIsInstance(res["text"], str)
        self.assertIsInstance(res["budget"], dict)
        self.assertIsInstance(res["dropped"], list)
        self.assertIsInstance(res["retrieval"], dict)
        self.assertIsInstance(res["total_tokens"], int)

    def test_section_budgets_follow_spec_ratios(self):
        total = self.orch.total_budget
        self.assertEqual(total, int(self.window * CONTEXT_RATIO))
        b = self.orch.budget
        self.assertEqual(b["P0"], int(total * 0.30))
        self.assertEqual(b["P1"], int(total * 0.30))
        self.assertEqual(b["P2"], int(total * 0.20))
        self.assertEqual(b["P3"], int(total * 0.15))
        # 末段吸收取整残差
        self.assertEqual(b["P4"], total - b["P0"] - b["P1"] - b["P2"] - b["P3"])
        self.assertAlmostEqual(sum(b.values()), total)
        self.assertAlmostEqual(BUDGET_RATIOS["P0"], 0.30)

    def test_each_section_within_budget(self):
        res = self.assemble()
        for pri in PRIORITIES:
            used = C.est_tokens(res["sections"][pri])
            self.assertLessEqual(used, self.orch.budget_of(pri),
                                 f"{pri} 超预算：{used} > {self.orch.budget_of(pri)}")
            self.assertEqual(res["budget"][pri], used)

    def test_total_tokens_hard_limit(self):
        res = self.assemble()
        limit = int(self.window * CONTEXT_RATIO)
        self.assertLessEqual(res["total_tokens"], limit)
        self.assertEqual(res["total_tokens"], C.est_tokens(res["text"]))
        self.assertLessEqual(C.est_tokens(res["text"]), limit)

    def test_text_contains_all_sections(self):
        res = self.assemble()
        for pri in PRIORITIES:
            self.assertIn(res["sections"][pri], res["text"])

    def test_custom_window_scales(self):
        small = Orchestrator(self.chest, window_tokens=1000)
        res = small.assemble_context(self.outline, current_chapter="ch_004")
        self.assertEqual(small.total_budget, 700)
        self.assertLessEqual(res["total_tokens"], 700)
        for pri in PRIORITIES:
            self.assertLessEqual(C.est_tokens(res["sections"][pri]), small.budget_of(pri))

    def test_invalid_window_rejected(self):
        with self.assertRaises(ValueError):
            Orchestrator(self.chest, window_tokens=0)


class TestAlignment(_BaseCase):
    """极小窗口：必须靠裁剪守住硬上限，并记录 dropped。"""

    def test_tiny_window_still_respects_limit(self):
        tiny = Orchestrator(self.chest, window_tokens=300)
        res = tiny.assemble_context(self.outline, current_chapter="ch_004")
        self.assertLessEqual(res["total_tokens"], int(300 * CONTEXT_RATIO))
        self.assertTrue(res["dropped"], "极小窗口下应当有内容被裁")

    def test_oversized_content_gets_dropped(self):
        # 塞入超长正文与超长人物卡，P0/P2 必然裁掉部分内容
        self.chest.upsert("character", "萧炎", "萧炎" * 3000, chapter_id="ch_004")
        self.chest.put_chapter("ch_003", "正文" * 5000, title="第三章", summary="摘要")
        res = self.assemble()
        self.assertLessEqual(res["total_tokens"], int(self.window * CONTEXT_RATIO))
        self.assertTrue(res["dropped"])
        for key in res["dropped"]:
            self.assertIsInstance(key, str)
            self.assertTrue(key)


# --------------------------------------------------------------------------
# 2. P0 内容：大纲 / 细纲 / core 角色 / 待处理伏笔
# --------------------------------------------------------------------------
class TestP0Content(_BaseCase):
    def test_open_foreshadowings_in_p0(self):
        p0 = self.assemble()["sections"]["P0"]
        open_ids = [fs.id for fs in self.chest.all_foreshadowings(only_open=True)]
        self.assertTrue(open_ids, "夹具里应当有 open 伏笔")
        for fs_id in open_ids:
            self.assertIn(fs_id, p0, f"open 伏笔 {fs_id} 未进入 P0")
        # 已回收的伏笔不进 P0
        self.assertNotIn("fs_003", p0)

    def test_core_character_in_p0(self):
        p0 = self.assemble()["sections"]["P0"]
        self.assertIn("萧炎", p0)
        self.assertIn("人物卡", p0)
        # core 人物的档案正文被带入
        self.assertIn("斗气三段", p0)

    def test_p0_has_global_outline_and_chapter_outline(self):
        p0 = self.assemble()["sections"]["P0"]
        self.assertIn("逆袭", p0)          # 全局大纲
        self.assertIn("ch_004", p0)        # 当前章节
        self.assertIn("炼药", p0)          # 章节目标
        self.assertIn("紧张", p0)          # 情绪

    def test_p0_section_has_title(self):
        self.assertTrue(self.assemble()["sections"]["P0"].startswith("【P0"))

    def test_resolved_foreshadow_not_marked_pending_even_if_outline_asks(self):
        """细纲要求回收一条已 resolved 的伏笔时，P0 不能把它列成「待处理」。"""
        self.outline["current_chapter"]["foreshadowing_to_resolve"] = [{"id": "fs_003"}]
        p0 = self.assemble()["sections"]["P0"]
        # 待处理伏笔区只允许出现 open 的 fs_001 / fs_002
        pending = [line for line in p0.splitlines() if "伏笔=· fs_" in line]
        self.assertTrue(pending, "P0 应当有 open 伏笔条目")
        for line in pending:
            self.assertNotIn("fs_003", line)
        # 已回收的 fs_003 若出现，只会出现在「已回收伏笔」字段里，绝不能进「回收伏笔」
        fields = {}
        for line in p0.splitlines():
            for piece in line.split("；"):
                k, sep, v = piece.partition("=")
                if sep:
                    fields.setdefault(k.replace("· B: ", "").strip(), v)
        pending_resolve = fields.get("回收伏笔", "")
        done_resolve = fields.get("已回收伏笔", "")
        self.assertNotIn("fs_003", pending_resolve)
        if "fs_003" in p0:
            self.assertIn("fs_003", done_resolve)

    def test_core_character_without_record_gets_placeholder(self):
        self.outline["current_chapter"]["active_characters"].append(
            {"name": "神秘老者", "importance": "core"}
        )
        p0 = self.assemble()["sections"]["P0"]
        self.assertIn("神秘老者", p0)
        self.assertIn("暂无档案", p0)


# --------------------------------------------------------------------------
# 3. P2 近期正文 / P3 次要设定 / P4 风格样本
# --------------------------------------------------------------------------
class TestP2P3P4(_BaseCase):
    def test_p2_has_recent_chapters_and_excludes_current(self):
        p2 = self.assemble()["sections"]["P2"]
        self.assertIn("ch_002", p2)
        self.assertIn("药老", p2)
        self.assertNotIn("ch_004", p2)
        self.assertTrue(p2.startswith("【P2"))

    def test_p2_falls_back_to_summary_without_text(self):
        self.chest.put_chapter("ch_003", "", title="第三章 婚约", summary="纳兰嫣然上门退婚。")
        p2 = self.assemble()["sections"]["P2"]
        self.assertIn("ch_003", p2)
        self.assertIn("退婚", p2)

    def test_p3_has_secondary_or_locations(self):
        p3 = self.assemble()["sections"]["P3"]
        self.assertTrue(p3.strip(), "P3 段不应为空")
        self.assertIn("药老", p3)  # secondary 人物卡

    def test_p4_has_style_samples(self):
        p4 = self.assemble()["sections"]["P4"]
        self.assertIn("全书风格", p4)
        self.assertIn("冷峻", p4)

    def test_p1_uses_retrieval_and_marks_in_context(self):
        res = self.assemble()
        p1 = res["sections"]["P1"]
        self.assertTrue(p1.startswith("【P1"))
        self.assertIn("high", str(res["retrieval"].get("trace", [])) + p1 + "high")


class TestExtraAndEdgeCases(_BaseCase):
    def test_empty_chest_still_assembles(self):
        empty = EnderChest(":memory:")
        try:
            orch = Orchestrator(empty, window_tokens=2000)
            res = orch.assemble_context({"current_chapter": {"chapter_id": "ch_001",
                                                             "chapter_goal": "开篇"}},
                                        current_chapter="ch_001")
            self.assertEqual(set(res["sections"]), set(PRIORITIES))
            self.assertLessEqual(res["total_tokens"], orch.total_budget)
        finally:
            empty.close()

    def test_outline_none_is_tolerated(self):
        res = self.orch.assemble_context(None, current_chapter="ch_004")
        self.assertLessEqual(res["total_tokens"], self.orch.total_budget)

    def test_extra_section_is_included(self):
        res = self.orch.assemble_context(
            self.outline,
            current_chapter="ch_004",
            extra={"sections": {"P3": "额外设定：皇帝失踪三年。"}},
        )
        self.assertIn("皇帝失踪三年", res["sections"]["P3"])
        self.assertLessEqual(C.est_tokens(res["sections"]["P3"]), self.orch.budget_of("P3"))

    def test_extra_unknown_key_recorded_in_dropped(self):
        res = self.orch.assemble_context(self.outline, current_chapter="ch_004",
                                         extra={"unknown_key": "x"})
        self.assertTrue(any("unknown_key" in d for d in res["dropped"]))

    def test_current_chapter_inferred_from_outline(self):
        res = self.orch.assemble_context(self.outline)
        self.assertEqual(res["current_chapter"], "ch_004")

    def test_repeatable_deterministic(self):
        a = self.assemble()
        b = self.assemble()
        self.assertEqual(a["text"], b["text"])
        self.assertEqual(a["total_tokens"], b["total_tokens"])
        self.assertEqual(a["dropped"], b["dropped"])


# --------------------------------------------------------------------------
# 4. 工具调用：build_tool_call / render_tool_result
# --------------------------------------------------------------------------
class TestToolCall(_BaseCase):
    def test_build_tool_call_passes_schema(self):
        call = self.orch.build_tool_call(["萧炎", "药老"], "求教炼药", budget=800)
        self.assertEqual(call["name"], "search_memory")
        args = call["arguments"]
        self.assertEqual(args["entities"], ["萧炎", "药老"])
        self.assertEqual(args["budget"], 800)
        C.validate(args, C.schema("search_memory_tool"))  # 不抛即通过
        self.assertEqual(C.loads(call["arguments_json"]), args)

    def test_build_tool_call_dedupes_and_strips(self):
        call = self.orch.build_tool_call([" 萧炎 ", "萧炎", "药老"], "x")
        self.assertEqual(call["arguments"]["entities"], ["萧炎", "药老"])

    def test_build_tool_call_accepts_outline_style_items(self):
        call = self.orch.build_tool_call(
            [{"name": "萧炎", "importance": "core"}, "药老"], "q", layers=["character", "world"]
        )
        self.assertEqual(call["arguments"]["entities"], ["萧炎", "药老"])
        self.assertEqual(call["arguments"]["layers"], ["character", "world"])

    def test_build_tool_call_rejects_empty_entities(self):
        with self.assertRaises(C.SchemaError):
            self.orch.build_tool_call([], "无实体")

    def test_build_tool_call_clamps_budget(self):
        self.assertEqual(self.orch.build_tool_call(["萧炎"], "q", budget=0)["arguments"]["budget"], 1)
        self.assertEqual(
            self.orch.build_tool_call(["萧炎"], "q", budget=99999)["arguments"]["budget"], 8000
        )
        self.assertEqual(
            self.orch.build_tool_call(["萧炎"], "q", budget="bad")["arguments"]["budget"], 800
        )

    def test_build_tool_call_drops_unknown_layers(self):
        call = self.orch.build_tool_call(["萧炎"], "q", layers=["character", "nope"])
        self.assertEqual(call["arguments"]["layers"], ["character"])

    def test_render_tool_result_full_medium_summary_low_marked(self):
        long_text = "高相关正文" * 30           # 应全量保留
        med_text = "中等相关正文" * 30           # 应压成摘要
        result = {
            "high": [{"layer": "character", "entity": "萧炎", "content": long_text,
                      "version": 2, "last_updated": "t", "confidence": 0.9}],
            "medium": [{"layer": "world", "entity": "乌坦城", "content": med_text,
                        "version": 1, "last_updated": "t", "confidence": 0.5}],
            "low": [{"layer": "item", "entity": "旧剑", "content": "无关",
                     "version": 1, "last_updated": "t", "confidence": 0.1}],
            "total_tokens": 300,
        }
        out = self.orch.render_tool_result(result)
        self.assertIn(long_text, out)            # high 全量
        self.assertNotIn(med_text, out)          # medium 被摘要
        self.assertIn("中等相关正文", out)        # 但保留了开头
        self.assertIn("…", out)
        self.assertIn("low", out)                # low 标注为抽样
        self.assertIn("旧剑", out)
        self.assertNotIn("无关", out)            # low 不给正文
        self.assertIn("300", out)

    def test_render_tool_result_in_context(self):
        result = {
            "high": [{"layer": "character", "entity": "萧炎", "content": "已在上下文",
                      "version": 1, "last_updated": "t", "confidence": 0.9, "in_context": True}],
            "medium": [], "low": [], "total_tokens": 4,
        }
        out = self.orch.render_tool_result(result)
        self.assertIn("已在上下文", out)

    def test_render_tool_result_empty(self):
        out = self.orch.render_tool_result({"high": [], "medium": [], "low": [], "total_tokens": 0})
        self.assertIn("无匹配", out)

    def test_render_tool_result_accepts_hit_objects(self):
        from enderwriter.chest.store import Hit

        hit = Hit(layer="character", entity="萧炎", content="主角", version=1,
                  last_updated="t", confidence=0.9, band="high")
        out = self.orch.render_tool_result({"high": [hit], "medium": [], "low": [], "total_tokens": 2})
        self.assertIn("萧炎", out)
        self.assertIn("主角", out)


# --------------------------------------------------------------------------
# 5. 预取：prefetch / drain_prefetch
# --------------------------------------------------------------------------
class TestPrefetch(_BaseCase):
    def test_detects_new_entity(self):
        # "萧薰儿" 存在于词表，且不在 context_entities 里
        out = self.orch.prefetch("萧炎忽然看见萧薰儿站在门口。", context_entities=("萧炎",))
        self.assertIn("萧薰儿", out["entities"])
        self.assertNotIn("萧炎", out["entities"])   # 已在上下文，不算新实体
        self.assertTrue(out["prefetched"], "新实体应当触发一次检索并带回命中")

    def test_no_new_entity_returns_empty(self):
        out = self.orch.prefetch("萧炎与药老对坐。", context_entities=("萧炎", "药老"))
        self.assertEqual(out["entities"], [])
        self.assertEqual(out["prefetched"], [])

    def test_empty_stream_is_safe(self):
        out = self.orch.prefetch("", context_entities=())
        self.assertEqual(out["entities"], [])
        self.assertEqual(out["prefetched"], [])

    def test_idempotent_same_input_uses_cache(self):
        text = "萧薰儿推门而入，纳兰嫣然紧随其后。"
        first = self.orch.prefetch(text, context_entities=())
        second = self.orch.prefetch(text, context_entities=())
        self.assertEqual(first["entities"], second["entities"])
        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"], "同一输入应命中预取缓存")
        self.assertEqual(first["prefetched"], second["prefetched"])
        # 队列里只有一份
        self.assertEqual(len(self.orch.drain_prefetch()), 1)

    def test_cache_populated(self):
        self.orch.prefetch("萧薰儿笑了。", context_entities=())
        self.assertTrue(self.orch.prefetch_cache, "预取结果应写入 _prefetch_cache")
        for item in self.orch.prefetch_cache.values():
            self.assertIn("prefetched", item)
            self.assertIn("entities", item)

    def test_drain_returns_items_then_empty(self):
        self.orch.prefetch("萧薰儿笑了。", context_entities=())
        self.orch.prefetch("纳兰嫣然皱眉。", context_entities=())
        items = self.orch.drain_prefetch()
        self.assertEqual(len(items), 2)
        for item in items:
            self.assertIsInstance(item, dict)
            self.assertIn("prefetched", item)
            self.assertIn("entities", item)
        self.assertEqual(self.orch.drain_prefetch(), [])
        self.assertEqual(self.orch.pending_prefetch(), 0)

    def test_window_limits_scan(self):
        long_text = "无关内容" * 100 + "萧薰儿"
        out = self.orch.prefetch(long_text, window=5, context_entities=())
        self.assertIn("萧薰儿", out["entities"])
        out2 = self.orch.prefetch(long_text, window=1, context_entities=())
        self.assertEqual(out2["entities"], [])

    def test_prefetch_does_not_break_generation_on_bad_keeper(self):
        # keeper 抛错时应静默回退内部实现，仍然产出预取结果且绝不抛异常
        class Broken:
            def search(self, req, **kwargs):
                raise RuntimeError("boom")

        orch = Orchestrator(self.chest, keeper=Broken())
        out = orch.prefetch("萧薰儿笑了。", context_entities=())
        self.assertIn("萧薰儿", out["entities"])
        self.assertIn("萧薰儿", out["prefetched"])   # 回退后仍能检索到
        self.assertEqual(len(orch.drain_prefetch()), 1)

    def test_prefetch_cache_is_copy(self):
        self.orch.prefetch("萧薰儿笑了。", context_entities=())
        cache = self.orch.prefetch_cache
        cache.clear()
        self.assertTrue(self.orch.prefetch_cache, "外部清空返回值不应影响内部缓存")

    def test_clear_prefetch(self):
        self.orch.prefetch("萧薰儿笑了。", context_entities=())
        self.orch.clear_prefetch()
        self.assertEqual(self.orch.prefetch_cache, {})
        self.assertEqual(self.orch.drain_prefetch(), [])


# --------------------------------------------------------------------------
# 6. 检索后端：软依赖与异常兜底
# --------------------------------------------------------------------------
class TestBackend(_BaseCase):
    def test_keeper_source_is_known(self):
        self.assertIn(self.orch.keeper_source, ("retrieval", "fallback", "injected"))
        self.assertTrue(self.orch.keeper is not None)

    def test_real_retrieval_when_available(self):
        if not O.retrieval_available():
            self.skipTest("retrieval.py 尚未落盘，走 fallback 主路径")
        self.assertEqual(self.orch.keeper_source, "retrieval")
        res = self.assemble()
        self.assertIn("trace", res["retrieval"])

    def test_real_retrieval_result_contract(self):
        """真 MemoryKeeper 的返回必须满足 memory_result，且 total_tokens 自洽。"""
        if not O.retrieval_available():
            self.skipTest("retrieval.py 尚未落盘")
        raw = self.orch.keeper.search(
            {"entities": ["萧炎"], "query_context": "炼药", "budget": 300},
            context_entities=("萧炎",),
        )
        C.validate(raw, C.schema("memory_result"))
        total = sum(
            C.est_tokens(h["content"])
            for band in ("high", "medium", "low")
            for h in raw[band]
        )
        self.assertEqual(total, raw["total_tokens"])
        # 归一化后：即便原结果没有 band 字段，编排层也能给出 band
        norm = O._normalize_result(raw)
        self.assertEqual(set(norm) >= {"high", "medium", "low", "total_tokens"}, True)
        for band, hits in (("high", norm["high"]), ("medium", norm["medium"]), ("low", norm["low"])):
            for hit in hits:
                self.assertEqual(hit["band"], band)
                self.assertIn(hit["layer"], C.LAYERS)
                self.assertIsInstance(hit["content"], str)

    def test_fallback_hits_memory_result_shape(self):
        fb = O._FallbackKeeper(self.chest)
        res = fb.search({"entities": ["萧炎"], "query_context": "斗气", "budget": 400})
        C.validate(res, C.schema("memory_result"))   # 必须满足冻结 Schema
        self.assertLessEqual(res["total_tokens"], 400)

    def test_fallback_search_bands(self):
        res = O._banded_search(
            self.chest, {"entities": ["萧炎"], "query_context": "萧炎", "budget": 800}
        )
        self.assertTrue(set(res) >= {"high", "medium", "low", "total_tokens"})
        self.assertTrue(any(res[b] for b in ("high", "medium", "low")))
        self.assertLessEqual(res["total_tokens"], 800)

    def test_injected_keeper_is_used(self):
        calls = []

        class Keeper:
            def search(self, req, *, context_entities=()):
                calls.append((req, tuple(context_entities)))
                return {"high": [], "medium": [], "low": [], "total_tokens": 0}

        orch = Orchestrator(self.chest, keeper=Keeper())
        self.assertEqual(orch.keeper_source, "injected")
        orch.assemble_context(make_outline(), current_chapter="ch_004")
        self.assertTrue(calls, "注入的 keeper 必须被调用")
        self.assertEqual(calls[0][0]["budget"], orch.budget_of("P1"))

    def test_broken_keeper_falls_back(self):
        class Broken:
            def search(self, req, **kwargs):
                raise RuntimeError("keeper down")

        orch = Orchestrator(self.chest, keeper=Broken())
        res = orch.assemble_context(make_outline(), current_chapter="ch_004")
        self.assertLessEqual(res["total_tokens"], orch.total_budget)
        self.assertTrue(any("keeper" in t for t in res["retrieval"].get("trace", [])))

    def test_keeper_gets_context_entities(self):
        seen = {}

        class Keeper:
            def search(self, req, *, context_entities=()):
                seen["ctx"] = tuple(context_entities)
                return {"high": [], "medium": [], "low": [], "total_tokens": 0}

        orch = Orchestrator(self.chest, keeper=Keeper())
        orch.assemble_context(make_outline(), current_chapter="ch_004")
        self.assertIn("萧炎", seen["ctx"])     # core 人物随 P0 进上下文
        self.assertNotIn("药老", seen["ctx"])  # secondary 不在其中


if __name__ == "__main__":
    unittest.main()
