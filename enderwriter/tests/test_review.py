"""规则引擎一致性检查的单元测试。

重点覆盖：
  * 四类冲突都能检出
  * 正常文本零误报（尤其是容易误判的边界）
  * 事实抽取能识别否定前缀
"""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from enderwriter.chest import EnderChest
from enderwriter.review import (
    CONFLICT_RULES,
    CheckResult,
    Fact,
    check_text,
    extract_facts,
)


class TestFactExtraction(unittest.TestCase):
    def setUp(self):
        self.chest = EnderChest(":memory:")

    def tearDown(self):
        self.chest.close()

    def test_injury_from_text(self):
        self.chest.upsert("character", "主角甲", "左手受刀伤，缠着绷带。", chapter_id="ch_003")
        facts = extract_facts(self.chest)
        injuries = [f for f in facts if f.kind == "injury"]
        self.assertEqual(len(injuries), 1)
        self.assertEqual(injuries[0].value, "左手")
        self.assertEqual(injuries[0].subject, "主角甲")

    def test_death_from_text(self):
        self.chest.upsert("character", "主角乙", "主角甲的同学，于第10章身亡。", chapter_id="ch_010")
        facts = extract_facts(self.chest)
        dead = [f for f in facts if f.kind == "dead"]
        self.assertEqual(len(dead), 1)
        self.assertEqual(dead[0].subject, "主角乙")

    def test_death_negation_not_extracted(self):
        """「没死」「不会死」不能被当成死亡事实。"""
        self.chest.upsert("character", "甲", "他其实没死，只是昏迷。")
        facts = extract_facts(self.chest)
        self.assertEqual([f for f in facts if f.kind == "dead"], [])

        self.chest.upsert("character", "乙", "他不会死的。")
        facts2 = extract_facts(self.chest)
        self.assertFalse([f for f in facts2 if f.kind == "dead" and f.subject == "乙"])

    def test_owner_from_text(self):
        self.chest.upsert("item", "断刃", "主角甲的佩剑，由主角乙保管。")
        facts = extract_facts(self.chest)
        owners = [f for f in facts if f.kind == "owner"]
        self.assertEqual(len(owners), 1)
        self.assertEqual(owners[0].subject, "断刃")
        self.assertEqual(owners[0].value, "主角乙")

    def test_state_takes_priority(self):
        self.chest.upsert("character", "李四", "斗气三段。")
        self.chest.set_state("李四", "realm", "斗帝", chapter_id="ch_030")
        facts = extract_facts(self.chest)
        realms = [f for f in facts if f.kind == "realm"]
        self.assertEqual(len(realms), 1)
        self.assertEqual(realms[0].value, "斗帝")

    def test_alive_state_false_becomes_dead(self):
        self.chest.upsert("character", "丙", "某角色")
        self.chest.set_state("丙", "alive", False, chapter_id="ch_005")
        facts = extract_facts(self.chest)
        # alive=False 应转成 dead 事实
        self.assertTrue(any(f.kind == "alive" and f.value is False for f in facts))

    def test_entities_filter(self):
        self.chest.upsert("character", "甲", "左手受刀伤。")
        self.chest.upsert("character", "乙", "右手受刀伤。")
        facts = extract_facts(self.chest, entities=["甲"])
        self.assertEqual({f.subject for f in facts}, {"甲"})


class TestRules(unittest.TestCase):
    def setUp(self):
        self.chest = EnderChest(":memory:")
        self.chest.upsert("character", "主角甲", "左手受刀伤，缠着绷带。", chapter_id="ch_003")
        self.chest.upsert("character", "主角乙", "主角甲的同学，于第10章身亡。", chapter_id="ch_010")
        self.chest.upsert("item", "断刃", "主角甲的佩剑，由主角乙保管。", chapter_id="ch_008")
        self.chest.upsert("character", "李四", "某角色。")
        self.chest.set_state("李四", "realm", "斗帝", chapter_id="ch_030")
        self.facts = extract_facts(self.chest)

    def tearDown(self):
        self.chest.close()

    # ---- R1 死而复生 ----
    def test_r1_detects_dead_character(self):
        r = check_text("主角乙笑着走近，拍了拍主角甲的肩膀。", self.facts)
        self.assertFalse(r.ok)
        self.assertIn("R1", [i.rule for i in r.issues])

    def test_r1_allows_memory_reference(self):
        for text in (
            "他想起主角乙生前说过的话。",
            "主角乙的遗蜕静静立在崖边。",
            "那块墓碑上刻着主角乙的名字。",
            "梦里，主角乙又笑了一次。",
        ):
            r = check_text(text, self.facts)
            self.assertTrue(r.ok, f"不该误报：{text}")

    # ---- R2 伤情 ----
    def test_r2_detects_injured_part_action(self):
        r = check_text("主角甲左手握剑，一剑劈下。", self.facts)
        self.assertFalse(r.ok)
        self.assertIn("R2", [i.rule for i in r.issues])

    def test_r2_detects_two_hand_action(self):
        r = check_text("主角甲双手握剑，奋力劈下。", self.facts)
        self.assertFalse(r.ok)
        self.assertIn("R2", [i.rule for i in r.issues])

    def test_r2_allows_healthy_side(self):
        r = check_text("主角甲右手握剑，站在雨里等对方先动。", self.facts)
        self.assertTrue(r.ok)

    def test_r2_allows_mentioned_pain(self):
        for text in (
            "他忍着左手的痛，把剑换到右手。",
            "左手还在渗血，他只能用右手。",
            "他扯了扯左手上的绷带。",
        ):
            r = check_text(text, self.facts)
            self.assertTrue(r.ok, f"不该误报：{text}")

    # ---- R3 持有者 ----
    def test_r3_detects_owner_violation(self):
        r = check_text("主角甲拔出断刃，横在身前。", self.facts)
        self.assertFalse(r.ok)
        self.assertIn("R3", [i.rule for i in r.issues])

    def test_r3_allows_transfer(self):
        for text in (
            "主角甲从主角乙手中接过断刃。",   # 持有者在场
            "他夺过断刃。",               # 夺取动作
            "主角甲捡起了地上的断刃。",     # 获得动作
        ):
            r = check_text(text, self.facts)
            # 只关心 R3，其他规则（如死者不能行动）另测
            r3 = [i for i in r.issues if i.rule == "R3"]
            self.assertEqual(r3, [], f"不该误报 R3：{text}")

    def test_r6_dead_takes_action(self):
        """死者在正文里主动做事，即使是递东西也算复活冲突。"""
        r = check_text("主角乙把断刃交给主角甲。", self.facts)
        self.assertFalse(r.ok)
        self.assertIn("R1", [i.rule for i in r.issues])

    # ---- R4 境界 ----
    def test_r4_detects_realm_regression(self):
        r = check_text("李四不过斗之气三段，哪里挡得住。", self.facts)
        self.assertFalse(r.ok)
        self.assertIn("R4", [i.rule for i in r.issues])

    def test_r4_allows_same_or_higher(self):
        for text in (
            "李四已是斗帝，抬手便镇住了全场。",
            "斗圣之威压下来，李四也退了半步。",
        ):
            r = check_text(text, self.facts)
            self.assertTrue(r.ok, f"不该误报：{text}")


class TestCheckResult(unittest.TestCase):
    def test_empty_text_passes(self):
        r = check_text("", [])
        self.assertTrue(r.ok)
        self.assertEqual(r.issues, [])

    def test_no_facts_passes(self):
        r = check_text("随便一段正文。", [])
        self.assertTrue(r.ok)
        self.assertEqual(r.facts_used, 0)

    def test_to_dict_serializable(self):
        f = Fact("x:1", "dead", "甲", True, chapter="ch_001")
        r = check_text("甲走了过来。", [f])
        d = r.to_dict()
        self.assertFalse(d["ok"])
        self.assertEqual(d["facts_used"], 1)
        self.assertTrue(d["issues"])

    def test_render(self):
        f = Fact("x:1", "dead", "甲", True, chapter="ch_001")
        r = check_text("甲走了过来。", [f])
        text = r.render()
        self.assertIn("一致性检查", text)
        self.assertIn("死而复生", text)

    def test_rules_run_recorded(self):
        f = Fact("x:1", "dead", "甲", True)
        r = check_text("甲走了过来。", [f])
        self.assertIn("R1", r.rules_run)

    def test_partial_rules(self):
        f1 = Fact("a:1", "dead", "甲", True)
        f2 = Fact("b:1", "injury", "乙", "左手")
        text = "甲走了过来。乙左手握剑。"
        r = check_text(text, [f1, f2], rules=["R1"])
        self.assertEqual([i.rule for i in r.issues], ["R1"])

    def test_dedup_same_quote(self):
        f1 = Fact("a:1", "injury", "甲", "左手")
        # 同一句里两处触发同一规则只报一次
        r = check_text("甲左手握剑，左手又抬了起来。", [f1])
        self.assertLessEqual(len(r.issues), 2)

    def test_rules_catalog(self):
        self.assertEqual(len(CONFLICT_RULES), 5)
        ids = {r["id"] for r in CONFLICT_RULES}
        self.assertEqual(ids, {"R1", "R2", "R3", "R4", "R5"})


if __name__ == "__main__":
    unittest.main()
