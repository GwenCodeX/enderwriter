"""回写代理与伏笔巡检的单元测试（stdlib unittest，离线可跑）。

覆盖：
  * 合法报告的 人物/物品/时间/伏笔 四类更新真正落到末影箱（记录 + 状态索引）；
  * 非法报告抛 SchemaError，且**不产生任何写入**；
  * 四类矛盾检测各至少一例命中（死而复生 / 时间倒错 / 能力违规 / 称呼漂移）；
  * patrol 的 overdue 与 stale 判定边界（grace 内不报、超了报）；
  * 所有返回值可 json 序列化。
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from enderwriter import common as C
from enderwriter.chest import EnderChest
from enderwriter.writeback import WriteBackAgent, patrol


# --------------------------------------------------------------------------
# 测试夹具
# --------------------------------------------------------------------------
def make_chest() -> EnderChest:
    chest = EnderChest(":memory:")
    chest.link_alias("炎帝", "萧炎")
    chest.upsert("character", "萧炎", "主角，乌坦城出身。", chapter_id="ch_001")
    chest.upsert("character", "药老", "寄居黑戒的炼药师残魂。", chapter_id="ch_002")
    chest.upsert("item", "黑戒", "漆黑的戒指，内藏残魂。", chapter_id="ch_002")
    chest.set_state("萧炎", "alive", True, chapter_id="ch_001")
    chest.set_state("萧炎", "realm", "斗之气", chapter_id="ch_001")
    chest.set_state("药老", "alive", True, chapter_id="ch_002")
    return chest


def report(**over) -> dict:
    """一份合法的 writeback_report 基线。"""
    base = {
        "chapter_id": "ch_003",
        "updated": {
            "characters": ["萧炎"],
            "items": [],
            "timeline": [],
            "foreshadowing": [],
        },
        "warnings": [],
    }
    base.update(over)
    return base


class ChestCase(unittest.TestCase):
    def setUp(self):
        self.chest = make_chest()
        self.agent = WriteBackAgent(self.chest)

    def tearDown(self):
        self.chest.close()

    # -- 断言辅助 ------------------------------------------------------
    def assertJsonable(self, obj):
        dumped = json.dumps(obj, ensure_ascii=False)
        self.assertEqual(json.loads(dumped), obj)

    def foreshadow(self, fs_id: str):
        for fs in self.chest.all_foreshadowings():
            if fs.id == fs_id:
                return fs
        return None


# --------------------------------------------------------------------------
# 1) 合法报告：四类更新都落地
# --------------------------------------------------------------------------
class TestApplyValidReport(ChestCase):
    def test_return_shape(self):
        result = self.agent.apply(report())
        self.assertEqual(set(result), {"chapter_id", "applied", "warnings"})
        self.assertEqual(result["chapter_id"], "ch_003")
        self.assertIsInstance(result["applied"], list)
        self.assertIsInstance(result["warnings"], list)
        self.assertJsonable(result)

    def test_characters_land(self):
        result = self.agent.apply(
            report(updated={
                "characters": ["萧炎", "药老"], "items": [], "timeline": [], "foreshadowing": [],
            })
        )
        self.assertIn("character:萧炎", result["applied"])
        self.assertIn("character:药老", result["applied"])
        rec = self.chest.get("character", "萧炎")
        self.assertEqual(rec.version, 2)  # 已有记录 -> 版本递增
        self.assertEqual(rec.chapter_id, "ch_003")

    def test_character_dict_writes_state(self):
        self.agent.apply(report(updated={
            "characters": [{"name": "萧炎", "content": "晋升斗者。", "realm": "斗者"}],
            "items": [], "timeline": [], "foreshadowing": [],
        }))
        self.assertEqual(self.chest.get_state("萧炎", "realm"), "斗者")
        self.assertEqual(self.chest.state_chapter("萧炎", "realm"), "ch_003")

    def test_character_state_cn_keys_normalized(self):
        self.agent.apply(report(updated={
            "characters": [{"name": "萧炎", "states": {"境界": "斗者", "存活": True}}],
            "items": [], "timeline": [], "foreshadowing": [],
        }))
        self.assertEqual(self.chest.get_state("萧炎", "realm"), "斗者")
        self.assertIs(self.chest.get_state("萧炎", "alive"), True)

    def test_items_land(self):
        result = self.agent.apply(report(updated={
            "characters": [], "timeline": [], "foreshadowing": [],
            "items": [{"name": "黑戒", "content": "戒面裂纹加深。", "holder": "萧炎"}],
        }))
        self.assertIn("item:黑戒", result["applied"])
        self.assertEqual(self.chest.get("item", "黑戒").chapter_id, "ch_003")
        self.assertEqual(self.chest.get_state("黑戒", "holder"), "萧炎")

    def test_timeline_land(self):
        result = self.agent.apply(report(updated={
            "characters": [], "items": [], "foreshadowing": [],
            "timeline": [{"name": "ev_001", "content": "萧炎初遇药老。", "chapter_id": "ch_003"}],
        }))
        self.assertIn("timeline:ev_001", result["applied"])
        rec = self.chest.get("timeline", "ev_001")
        self.assertIn("初遇药老", rec.content)
        self.assertEqual(rec.chapter_id, "ch_003")

    def test_chapter_text_and_summary(self):
        result = self.agent.apply(report(text="正文内容。", summary="摘要", title="第三章"))
        self.assertIn("chapter:ch_003", result["applied"])
        ch = self.chest.get_chapter("ch_003")
        self.assertEqual(ch["text"], "正文内容。")
        self.assertEqual(ch["summary"], "摘要")
        self.assertEqual(ch["title"], "第三章")

    def test_report_warnings_preserved(self):
        result = self.agent.apply(report(warnings=["作者备注：节奏偏慢"]))
        self.assertIn("作者备注：节奏偏慢", result["warnings"])

    def test_chapter_id_override(self):
        result = self.agent.apply(report(), chapter_id="ch_007")
        self.assertEqual(result["chapter_id"], "ch_007")
        self.assertEqual(self.chest.get("character", "萧炎").chapter_id, "ch_007")


# --------------------------------------------------------------------------
# 2) 伏笔状态机
# --------------------------------------------------------------------------
class TestApplyForeshadow(ChestCase):
    def setUp(self):
        super().setUp()
        self.chest.set_foreshadow(
            "fs_001", "黑戒藏着药老残魂。", planted_chapter="ch_002", planned_resolve="ch_055"
        )

    def _apply_fs(self, status, cid="ch_003", **extra):
        entry = {"id": "fs_001", "status": status}
        entry.update(extra)
        return self.agent.apply(report(
            chapter_id=cid,
            updated={"characters": [], "items": [], "timeline": [], "foreshadowing": [entry]},
        ))

    def test_planted_new_foreshadow(self):
        result = self.agent.apply(report(updated={
            "characters": [], "items": [], "timeline": [],
            "foreshadowing": [{"id": "fs_009", "status": "planted", "content": "老者身份存疑。"}],
        }))
        self.assertIn("foreshadowing:fs_009#planted", result["applied"])
        fs = self.foreshadow("fs_009")
        self.assertIsNotNone(fs)
        self.assertEqual(fs.status, "planted")
        self.assertEqual(fs.planted_chapter, "ch_003")
        self.assertEqual(fs.content, "老者身份存疑。")

    def test_reinforced_persists_chapters(self):
        """回归测试：冻结层 set_foreshadow 会用旧值覆盖，必须仍能读回强化章。"""
        result = self._apply_fs("reinforced", cid="ch_010")
        self.assertIn("foreshadowing:fs_001#reinforced", result["applied"])
        fs = self.foreshadow("fs_001")
        self.assertEqual(fs.status, "reinforced")
        self.assertIn("ch_010", fs.reinforced_chapters)
        self.assertIsNone(fs.resolved_chapter)

    def test_resolved_sets_chapter(self):
        self._apply_fs("reinforced", cid="ch_010")
        self._apply_fs("resolved", cid="ch_055")
        fs = self.foreshadow("fs_001")
        self.assertEqual(fs.status, "resolved")
        self.assertEqual(fs.resolved_chapter, "ch_055")
        self.assertIn("ch_010", fs.reinforced_chapters)  # 强化历史不丢
        self.assertFalse(fs.is_open)

    def test_abandoned(self):
        self._apply_fs("abandoned", cid="ch_040", content="太久没提，废弃")
        fs = self.foreshadow("fs_001")
        self.assertEqual(fs.status, "abandoned")
        self.assertEqual(fs.data.get("abandon_reason"), "太久没提，废弃")

    def test_unknown_foreshadow_with_content_is_planted(self):
        result = self.agent.apply(report(updated={
            "characters": [], "items": [], "timeline": [],
            "foreshadowing": [{"id": "fs_777", "status": "planted", "content": "新线索。"}],
        }))
        self.assertIn("foreshadowing:fs_777#planted", result["applied"])
        self.assertEqual(self.foreshadow("fs_777").content, "新线索。")

    def test_unknown_foreshadow_without_content_raises(self):
        # fs_999 从未登记，报告也没给 content -> 无法新建，抛 KeyError
        with self.assertRaises(KeyError):
            self.agent.apply(report(updated={
                "characters": [], "items": [], "timeline": [],
                "foreshadowing": [{"id": "fs_999", "status": "reinforced"}],
            }))

    def test_resolved_clears_abandon_candidate(self):
        self.chest.set_foreshadow(
            "fs_001", "黑戒藏着药老残魂。", status="planted",
            planted_chapter="ch_002", planned_resolve="ch_055",
            extra={"abandon_candidate": True},
        )
        self._apply_fs("resolved", cid="ch_055")
        self.assertNotIn("abandon_candidate", self.foreshadow("fs_001").data)


# --------------------------------------------------------------------------
# 3) 非法报告：SchemaError 且不写入
# --------------------------------------------------------------------------
class TestApplyInvalidReport(ChestCase):
    def _snapshot(self):
        return (
            self.chest.stats(),
            [f.status for f in self.chest.all_foreshadowings()],
            self.chest.get("character", "萧炎").version,
        )

    def test_missing_required_raises(self):
        with self.assertRaises(C.SchemaError):
            self.agent.apply({"chapter_id": "ch_003"})

    def test_bad_foreshadow_status_raises(self):
        with self.assertRaises(C.SchemaError):
            self.agent.apply(report(updated={
                "characters": [], "items": [], "timeline": [],
                "foreshadowing": [{"id": "fs_001", "status": "bogus"}],
            }))

    def test_bad_chapter_id_raises(self):
        with self.assertRaises(C.SchemaError):
            self.agent.apply(report(chapter_id="42"))

    def test_invalid_entry_type_raises(self):
        with self.assertRaises(C.SchemaError):
            self.agent.apply(report(updated={
                "characters": [{"content": "缺 name"}],
                "items": [], "timeline": [], "foreshadowing": [],
            }))

    def test_schema_error_leaves_chest_untouched(self):
        before = self._snapshot()
        with self.assertRaises(C.SchemaError):
            self.agent.apply(report(updated={
                "characters": ["萧炎"], "items": [], "timeline": [],
                "foreshadowing": [{"id": "fs_001", "status": "bogus"}],
            }))
        self.assertEqual(self._snapshot(), before)

    def test_detect_contradictions_validates(self):
        with self.assertRaises(C.SchemaError):
            self.agent.detect_contradictions({"chapter_id": "ch_003"})


# --------------------------------------------------------------------------
# 4) 四类矛盾检测
# --------------------------------------------------------------------------
class TestContradictionDeadAlive(ChestCase):
    def test_resurrection_detected(self):
        self.chest.set_state("药老", "alive", False, chapter_id="ch_010")
        found = self.agent.detect_contradictions(report(updated={
            "characters": ["药老"], "items": [], "timeline": [], "foreshadowing": [],
        }))
        self.assertEqual(len(found), 1)
        self.assertIn("死而复生", found[0])
        self.assertIn("药老", found[0])
        self.assertJsonable(found)

    def test_alive_character_not_flagged(self):
        found = self.agent.detect_contradictions(report())
        self.assertEqual(found, [])

    def test_explicit_revival_not_flagged(self):
        self.chest.set_state("药老", "alive", False, chapter_id="ch_010")
        found = self.agent.detect_contradictions(report(updated={
            "characters": [{"name": "药老", "content": "假死归来。"}],
            "items": [], "timeline": [], "foreshadowing": [],
        }))
        self.assertEqual(found, [])

    def test_active_characters_also_checked(self):
        self.chest.set_state("药老", "alive", False, chapter_id="ch_010")
        r = report()
        r["active_characters"] = [{"name": "药老", "importance": "core"}]
        found = self.agent.detect_contradictions(r)
        self.assertTrue(any("死而复生" in w for w in found))


class TestContradictionTimeInversion(ChestCase):
    def test_character_state_older_chapter(self):
        self.chest.set_state("萧炎", "realm", "斗者", chapter_id="ch_030")
        found = self.agent.detect_contradictions(report(chapter_id="ch_020", updated={
            "characters": [{"name": "萧炎", "realm": "斗师"}],
            "items": [], "timeline": [], "foreshadowing": [],
        }))
        self.assertTrue(any("时间倒错" in w for w in found), found)

    def test_timeline_event_in_past(self):
        # 事件标注发生在第 9 章，却由第 12 章回写 -> 倒错
        found = self.agent.detect_contradictions(report(chapter_id="ch_012", updated={
            "characters": [], "items": [], "foreshadowing": [],
            "timeline": [{"name": "ev_009", "content": "此前的战役", "chapter_id": "ch_009"}],
        }))
        self.assertTrue(any("时间倒错" in w for w in found), found)

    def test_timeline_event_in_future_not_flagged(self):
        # 事件在第 10 章，本章回写第 3 章：那是「预告」，不是倒错
        found = self.agent.detect_contradictions(report(chapter_id="ch_003", updated={
            "characters": [], "items": [], "foreshadowing": [],
            "timeline": [{"name": "ev_010", "content": "后的战役", "chapter_id": "ch_010"}],
        }))
        self.assertEqual(found, [])

    def test_chronological_order_not_flagged(self):
        self.chest.set_state("萧炎", "realm", "斗者", chapter_id="ch_002")
        found = self.agent.detect_contradictions(report(chapter_id="ch_005", updated={
            "characters": [{"name": "萧炎", "realm": "斗师"}],
            "items": [], "timeline": [], "foreshadowing": [],
        }))
        self.assertEqual(found, [])


class TestContradictionRuleViolation(ChestCase):
    def test_realm_regression_detected(self):
        self.chest.set_state("萧炎", "realm", "斗宗", chapter_id="ch_030")
        found = self.agent.detect_contradictions(report(chapter_id="ch_040", updated={
            "characters": [{"name": "萧炎", "realm": "斗灵"}],
            "items": [], "timeline": [], "foreshadowing": [],
        }))
        self.assertTrue(any("能力违规" in w for w in found), found)
        self.assertJsonable(found)

    def test_realm_advance_not_flagged(self):
        self.chest.set_state("萧炎", "realm", "斗灵", chapter_id="ch_030")
        found = self.agent.detect_contradictions(report(chapter_id="ch_040", updated={
            "characters": [{"name": "萧炎", "realm": "斗宗"}],
            "items": [], "timeline": [], "foreshadowing": [],
        }))
        self.assertEqual(found, [])

    def test_same_realm_tier_with_suffix_not_flagged(self):
        self.chest.set_state("萧炎", "realm", "斗宗", chapter_id="ch_030")
        found = self.agent.detect_contradictions(report(chapter_id="ch_040", updated={
            "characters": [{"name": "萧炎", "realm": "斗宗巅峰"}],
            "items": [], "timeline": [], "foreshadowing": [],
        }))
        self.assertEqual(found, [])

    def test_alive_flip_detected(self):
        self.chest.set_state("药老", "alive", True, chapter_id="ch_020")
        found = self.agent.detect_contradictions(report(chapter_id="ch_040", updated={
            "characters": [{"name": "药老", "alive": False}],
            "items": [], "timeline": [], "foreshadowing": [],
        }))
        self.assertTrue(any("规则违规" in w for w in found), found)

    def test_unknown_realm_ignored(self):
        self.chest.set_state("萧炎", "realm", "不可知名", chapter_id="ch_030")
        found = self.agent.detect_contradictions(report(chapter_id="ch_040", updated={
            "characters": [{"name": "萧炎", "realm": "斗灵"}],
            "items": [], "timeline": [], "foreshadowing": [],
        }))
        self.assertEqual(found, [])


class TestContradictionAliasDrift(ChestCase):
    def test_unregistered_alias_warns(self):
        found = self.agent.detect_contradictions(report(text="萧炎哥哥缓步而来。"))
        self.assertTrue(any("称呼漂移" in w for w in found), found)
        self.assertIn("萧炎哥哥", found[0])
        self.assertJsonable(found)

    def test_registered_alias_clean(self):
        found = self.agent.detect_contradictions(report(text="炎帝缓步而来，萧炎抬头。"))
        self.assertEqual(found, [])

    def test_unregistered_variant_of_registered_alias(self):
        found = self.agent.detect_contradictions(report(text="炎帝大人开口了。"))
        self.assertTrue(any("称呼漂移" in w for w in found), found)

    def test_sentence_fragment_not_flagged(self):
        # 分词跨词边界的片段（「萧炎抬头」）不是别称，不能误报
        found = self.agent.detect_contradictions(report(text="萧炎抬头看天，风起。"))
        self.assertEqual(found, [])

    def test_no_text_no_warning(self):
        found = self.agent.detect_contradictions(report())
        self.assertEqual(found, [])

    def test_drift_does_not_block_apply(self):
        result = self.agent.apply(report(text="萧炎哥哥缓步而来。"))
        self.assertEqual(result["warnings"], [])  # detect 与 apply 解耦
        self.assertIn("character:萧炎", result["applied"])


class TestContradictionMatrix(ChestCase):
    def test_all_four_categories_in_one_report(self):
        self.chest.set_state("药老", "alive", False, chapter_id="ch_010")
        self.chest.set_state("萧炎", "realm", "斗宗", chapter_id="ch_030")
        found = self.agent.detect_contradictions(report(
            chapter_id="ch_020",
            updated={
                "characters": [
                    {"name": "药老"},
                    {"name": "萧炎", "realm": "斗灵"},
                ],
                "items": [], "foreshadowing": [],
                "timeline": [{"name": "ev_009", "chapter_id": "ch_009"}],
            },
            text="萧炎哥哥转身离去。",
        ))
        joined = "\n".join(found)
        for marker in ("死而复生", "时间倒错", "能力违规", "称呼漂移"):
            self.assertIn(marker, joined)
        self.assertJsonable(found)
        self.assertEqual(len(found), len(set(found)))  # 去重


# --------------------------------------------------------------------------
# 5) patrol：超期 / 陈旧 / 已解决仍挂账
# --------------------------------------------------------------------------
class TestPatrolOverdue(unittest.TestCase):
    def setUp(self):
        self.chest = EnderChest(":memory:")
        self.chest.set_foreshadow(
            "fs_001", "黑戒藏着药老残魂。", planted_chapter="ch_002", planned_resolve="ch_010"
        )

    def tearDown(self):
        self.chest.close()

    def test_within_grace_not_reported(self):
        # planned 10 + grace 3 = 13；12、13 都还在宽限内
        for cur in (10, 11, 12, 13):
            self.assertEqual(patrol(self.chest, cur), [], f"current={cur} 不应报超期")

    def test_over_grace_reported(self):
        items = patrol(self.chest, 14)
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["type"], "overdue")
        self.assertEqual(item["id"], "fs_001")
        self.assertEqual(item["planned_resolve"], "ch_010")
        self.assertEqual(item["overdue_by"], 4)
        self.assertIn("suggestion", item)
        self.assertTrue(item["suggestion"])  # 可注入下一章细纲的建议
        self.assertIn("fs_001", item["suggestion"])

    def test_custom_grace(self):
        over = patrol(self.chest, 11, grace=0)
        self.assertEqual(len(over), 1)
        self.assertEqual(over[0]["type"], "overdue")
        self.assertEqual(over[0]["id"], "fs_001")
        self.assertEqual(patrol(self.chest, 10, grace=0), [])

    def test_chapter_id_accepted(self):
        self.assertTrue(patrol(self.chest, "ch_014"))
        self.assertEqual(patrol(self.chest, "ch_013"), [])

    def test_bad_chapter_raises(self):
        with self.assertRaises(ValueError):
            patrol(self.chest, "nope")

    def test_resolved_foreshadow_not_overdue(self):
        self.chest.resolve_foreshadow("fs_001", "ch_012")
        self.assertEqual([i for i in patrol(self.chest, 99) if i["type"] == "overdue"], [])


class TestPatrolStale(unittest.TestCase):
    def setUp(self):
        self.chest = EnderChest(":memory:")
        self.chest.set_foreshadow("fs_002", "老者身份存疑。", planted_chapter="ch_010")

    def tearDown(self):
        self.chest.close()

    def test_boundary_exactly_at_threshold_not_reported(self):
        # 30 - 10 == stale_threshold(30)：不报（严格大于才报）
        self.assertEqual([i for i in patrol(self.chest, 40) if i["type"] == "stale"], [])
        # 31 - 10 == 31 > 30：报
        items = [i for i in patrol(self.chest, 41) if i["type"] == "stale"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], "fs_002")
        self.assertEqual(items[0]["stale_chapters"], 31)

    def test_custom_threshold(self):
        # 15 - 10 == 5：不报；16 - 10 == 6 > 5：报
        self.assertEqual([i for i in patrol(self.chest, 15, stale_threshold=5) if i["type"] == "stale"], [])
        self.assertTrue([i for i in patrol(self.chest, 16, stale_threshold=5) if i["type"] == "stale"])

    def test_marks_abandon_candidate_without_changing_status(self):
        patrol(self.chest, 41)
        rec = self.chest.get("foreshadowing", "fs_002")
        self.assertIs(rec.data.get("abandon_candidate"), True)
        self.assertEqual(rec.data.get("status"), "planted")  # status 不变

    def test_reinforced_not_stale(self):
        self.chest.reinforce_foreshadow("fs_002", "ch_020")
        self.assertEqual([i for i in patrol(self.chest, 99) if i["type"] == "stale"], [])

    def test_strengthened_once_history_blocks_stale(self):
        # 强化过（哪怕很久以前）就不算「从未强化」。
        # 注意：冻结层的 reinforce_foreshadow 落盘会丢掉 reinforced_chapters，
        # 所以这里断言的是「状态已变为 reinforced + 有后续版本」而不是章节列表。
        self.chest.reinforce_foreshadow("fs_002", "ch_011")
        rec = self.chest.get("foreshadowing", "fs_002")
        self.assertEqual(rec.data["status"], "reinforced")
        self.assertGreater(len(self.chest.history("foreshadowing", "fs_002")), 1)
        self.assertEqual([i for i in patrol(self.chest, 99) if i["type"] == "stale"], [])


class TestPatrolUnresolved(unittest.TestCase):
    def setUp(self):
        self.chest = EnderChest(":memory:")
        self.chest.set_foreshadow("fs_003", "旧地图。", planted_chapter="ch_001")
        self.chest.resolve_foreshadow("fs_003", "ch_005")

    def tearDown(self):
        self.chest.close()

    def test_resolved_still_pending_reported(self):
        # 标记为仍在 foreshadowing_to_resolve 中
        self.chest.set_foreshadow(
            "fs_003", "旧地图。", status="resolved", planted_chapter="ch_001",
            resolved_chapter="ch_005", extra={"pending_resolve": True},
        )
        items = [i for i in patrol(self.chest, 10) if i["type"] == "unresolved"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], "fs_003")
        self.assertIn("suggestion", items[0])

    def test_resolved_and_cleared_not_reported(self):
        self.assertEqual(patrol(self.chest, 10), [])


class TestPatrolOrderingAndJson(unittest.TestCase):
    def setUp(self):
        self.chest = EnderChest(":memory:")
        # 陈旧：早期埋下、从未强化
        self.chest.set_foreshadow("fs_001", "旧伏笔。", planted_chapter="ch_001")
        # 超期：计划回收但拖了很久
        self.chest.set_foreshadow(
            "fs_002", "超期伏笔。", planted_chapter="ch_002", planned_resolve="ch_010"
        )
        # 已解决但仍挂账
        self.chest.set_foreshadow("fs_003", "已回收。", planted_chapter="ch_003")
        self.chest.resolve_foreshadow("fs_003", "ch_005")
        self.chest.set_foreshadow(
            "fs_003", "已回收。", status="resolved", planted_chapter="ch_003",
            resolved_chapter="ch_005", extra={"pending_resolve": True},
        )

    def tearDown(self):
        self.chest.close()

    def test_overdue_first(self):
        items = patrol(self.chest, 60)
        types = [i["type"] for i in items]
        self.assertEqual(types[0], "overdue")
        # unplanned 是后加的类别：开着但没定回收计划的伏笔会被报出来，
        # 排最后（不紧急），但必须出现——否则巡检无法解释为什么算不出超期。
        order = {"overdue": 0, "stale": 1, "unresolved": 2, "unplanned": 3}
        self.assertEqual(types, sorted(types, key=lambda t: order[t]))

    def test_unplanned_reported(self):
        """没有 planned_resolve 的伏笔必须被报为 unplanned。"""
        items = patrol(self.chest, 60)
        unplanned = [i for i in items if i["type"] == "unplanned"]
        # fs_001 与 fs_003 都没设 planned_resolve；fs_003 已 resolved 不算 open
        self.assertIn("fs_001", [i["id"] for i in unplanned])
        self.assertNotIn("fs_003", [i["id"] for i in unplanned])

    def test_json_serializable(self):
        items = patrol(self.chest, 60)
        self.assertTrue(items)
        dumped = json.dumps(items, ensure_ascii=False)
        self.assertEqual(json.loads(dumped), items)

    def test_ids_sorted_within_type(self):
        items = patrol(self.chest, 60)
        stale_ids = [i["id"] for i in items if i["type"] == "stale"]
        self.assertEqual(stale_ids, sorted(stale_ids))

    def test_each_item_has_type_and_id(self):
        for item in patrol(self.chest, 60):
            self.assertIn("type", item)
            self.assertIn("id", item)


# --------------------------------------------------------------------------
# 6) 端到端：patrol 的建议能回流到下一章细纲
# --------------------------------------------------------------------------
class TestPatrolFeedsNextOutline(unittest.TestCase):
    def test_overdue_suggestion_is_injectable(self):
        with EnderChest(":memory:") as chest:
            chest.set_foreshadow(
                "fs_001", "黑戒藏着药老残魂。", planted_chapter="ch_002", planned_resolve="ch_010"
            )
            items = patrol(chest, 20)
            overdue = [i for i in items if i["type"] == "overdue"]
            self.assertTrue(overdue)
            suggestion = overdue[0]["suggestion"]
            self.assertIn("fs_001", suggestion)
            self.assertIn("第 21 章", suggestion)  # 指向下一章

    def test_patrol_after_apply_roundtrip(self):
        with EnderChest(":memory:") as chest:
            chest.set_foreshadow(
                "fs_001", "黑戒藏着药老残魂。", planted_chapter="ch_002", planned_resolve="ch_010"
            )
            agent = WriteBackAgent(chest)
            self.assertTrue([i for i in patrol(chest, 20) if i["type"] == "overdue"])
            # 下一章按建议回收 -> 巡检不再报
            agent.apply({
                "chapter_id": "ch_021",
                "updated": {
                    "characters": [], "items": [], "timeline": [],
                    "foreshadowing": [{"id": "fs_001", "status": "resolved"}],
                },
                "warnings": [],
            })
            self.assertEqual([i for i in patrol(chest, 21) if i["type"] == "overdue"], [])
            self.assertNotIn("abandon_candidate", chest.get("foreshadowing", "fs_001").data)


if __name__ == "__main__":
    unittest.main()
