"""新增模块的测试：guard / relations / export / importer / assertions_seed。

这些模块是在原有 340 项测试之后加的，此前完全没有自动化覆盖。
"""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from enderwriter.assertions_seed import BUILTIN_ASSERTIONS, seed_builtin
from enderwriter.chest import EnderChest
from enderwriter.export import DUP_THRESHOLD, export_markdown, organize
from enderwriter.guard import (
    Assertion,
    AssertionError_,
    guard,
    list_assertions,
    match,
    render,
    save,
    validate,
)
from enderwriter.importer import guess_layer, plan_import, run_import, split_sections
from enderwriter.relations import build_edges, edge_stats, extract_candidates


class ChestCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.chest = EnderChest(os.path.join(self.dir, "t.db"))

    def tearDown(self):
        try:
            self.chest.close()
        finally:
            shutil.rmtree(self.dir, ignore_errors=True)


# ==========================================================================
class TestGuardValidate(unittest.TestCase):
    """校验器是断言系统不变成噪音源的关键。"""

    def test_rejects_single_condition(self):
        p = validate("规则", ["operator =="])
        self.assertTrue(any("至少需要 2 个条件" in x for x in p))

    def test_rejects_generic_token(self):
        p = validate("规则", ["void Update", "Stopwatch"])
        self.assertTrue(any("过于通用" in x for x in p))

    def test_rejects_short_token(self):
        p = validate("规则", ["==", "abc"])
        self.assertTrue(any("太短" in x for x in p))

    def test_rejects_empty_rule(self):
        p = validate("", ["aaa", "bbb"])
        self.assertTrue(any("rule" in x for x in p))

    def test_accepts_good(self):
        self.assertEqual(validate("重载 operator== 里不许判空", ["operator ==", "== null"]), [])

    def test_allow_single(self):
        self.assertEqual(validate("规则", [">g__"], allow_single=True), [])
        self.assertTrue(validate("规则", [">g__"], allow_single=False))

    def test_task_only_assertion_ok(self):
        # 只有 when、没有代码条件的断言是合法的（流程性教训）
        self.assertEqual(validate("回写前必须比对字段顺序", [], when=["回写 DLL"]), [])

    def test_neither_condition_rejected(self):
        self.assertTrue(validate("规则", [], when=[]))


class TestGuardMatch(ChestCase):
    def setUp(self):
        super().setUp()
        save(
            self.chest,
            "a1",
            "重载 operator== 里不得用 == null 判空",
            detect_all=["operator ==", "== null"],
            detect_not=["ReferenceEquals"],
            severity="high",
        )

    def test_hits_when_all_present(self):
        code = "bool operator ==(A a, A b) { return a == null; }"
        hits = guard(self.chest, code)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].assertion.id, "a1")
        self.assertEqual(hits[0].line, 1)

    def test_skips_when_detect_not_present(self):
        code = "bool operator ==(A a, A b) { return ReferenceEquals(a, null); }"
        self.assertEqual(guard(self.chest, code), [])

    def test_skips_when_partial(self):
        self.assertEqual(guard(self.chest, "operator =="), [])

    def test_empty_text_no_hits(self):
        self.assertEqual(guard(self.chest, "   "), [])

    def test_line_number(self):
        code = "line1\nline2\nbool operator ==(A a) { return a == null; }"
        hits = guard(self.chest, code)
        self.assertEqual(hits[0].line, 3)

    def test_save_rejects_invalid(self):
        with self.assertRaises(AssertionError_):
            save(self.chest, "bad", "太宽", detect_all=["operator =="])

    def test_list_ordering_by_severity(self):
        save(self.chest, "b1", "低危规则", detect_all=["aaa", "bbb"], severity="low")
        ids = [a.id for a in list_assertions(self.chest)]
        self.assertEqual(ids[0], "a1")  # high 在前

    def test_min_severity_filter(self):
        save(self.chest, "b1", "低危规则", detect_all=["aaa", "bbb"], severity="low")
        only_high = list_assertions(self.chest, min_severity="high")
        self.assertEqual([a.id for a in only_high], ["a1"])

    def test_render_empty_pool(self):
        self.chest.delete("assertion", "a1")
        self.assertIn("还没有断言", render([]))

    def test_render_pass(self):
        self.assertIn("全部通过", render([], total=3))


class TestGuardTask(ChestCase):
    def test_task_triggers(self):
        save(
            self.chest,
            "w1",
            "回写 DLL 前必须比对字段顺序",
            when=["回写 DLL", "Assembly-CSharp"],
            detect_all=[],
        )
        hits = guard(self.chest, "", task="把反编译结果回写 DLL")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].via, "task")

    def test_task_no_match(self):
        save(self.chest, "w1", "规则", when=["回写 DLL"], detect_all=[])
        self.assertEqual(guard(self.chest, "", task="写 Python 脚本"), [])

    def test_code_and_task_coexist(self):
        save(self.chest, "c1", "代码规则", detect_all=["aaa", "bbb"])
        save(self.chest, "t1", "任务规则", when=["回写 DLL"], detect_all=[])
        hits = guard(self.chest, "aaa bbb", task="回写 DLL")
        self.assertEqual({h.assertion.id for h in hits}, {"c1", "t1"})


# ==========================================================================
class TestRelations(ChestCase):
    def _seed(self):
        self.chest.upsert("world", "残兽机制", "残兽机制总述。")
        self.chest.upsert("world", "残兽分级", "残兽分级属于残兽机制。残兽分级很重要。")
        self.chest.upsert("world", "残兽成长", "残兽成长是残兽机制的一部分。")
        self.chest.upsert("character", "白杨", "白杨能变成白露。白杨平时不行。")
        self.chest.upsert("character", "白露", "白杨的另一个形态。")

    def test_extract_finds_mentions(self):
        self._seed()
        cands = extract_candidates(self.chest)
        pairs = {(c.src, c.dst) for c in cands}
        self.assertIn(("残兽分级", "残兽机制"), pairs)
        self.assertIn(("白杨", "白露"), pairs)

    def test_part_of_inferred(self):
        self._seed()
        cands = extract_candidates(self.chest)
        rels = {(c.src, c.rel) for c in cands}
        self.assertIn(("残兽分级", "part_of"), rels)

    def test_no_self_edge(self):
        self._seed()
        for c in extract_candidates(self.chest):
            self.assertNotEqual(c.src, c.dst)

    def test_build_writes_edges(self):
        self._seed()
        st = build_edges(self.chest)
        self.assertGreater(st["written"], 0)
        self.assertGreater(edge_stats(self.chest)["total"], 0)

    def test_min_weight_filters(self):
        self._seed()
        strict = build_edges(self.chest, min_weight=99, include_head=False)
        self.assertEqual(strict["written"], 0)

    def test_max_per_src(self):
        # 一个源提及其他很多实体
        names = [f"目标{i}" for i in range(8)]
        self.chest.upsert("world", "枢纽", "提到：" + "、".join(names))
        for n in names:
            self.chest.upsert("world", n, "内容")
        st = build_edges(self.chest, min_weight=1, max_per_src=3, include_head=True)
        self.assertLessEqual(st["written"], 3)

    def test_empty_library(self):
        self.assertEqual(extract_candidates(self.chest), [])
        self.assertEqual(build_edges(self.chest)["written"], 0)


# ==========================================================================
class TestExport(ChestCase):
    def _seed(self):
        self.chest.upsert("world", "残兽", "残兽总述。")
        self.chest.upsert("world", "残兽分级", "强度分级。")
        self.chest.upsert("character", "白杨", "主角。")
        self.chest.upsert("world", "旧设定", "已作废内容。", data={"superseded": True})

    def test_markdown_has_sections(self):
        self._seed()
        md = export_markdown(self.chest, title="测试")
        self.assertIn("# 测试", md)
        self.assertIn("## 人物", md)
        self.assertIn("## 世界观", md)

    def test_prefix_hierarchy(self):
        """残兽分级 以 残兽 开头 → 应缩进为子条目。"""
        self._seed()
        md = export_markdown(self.chest)
        self.assertIn("#### 残兽分级", md)

    def test_superseded_marked(self):
        self._seed()
        md = export_markdown(self.chest)
        self.assertIn("已废止", md)

    def test_superseded_excluded(self):
        self._seed()
        md = export_markdown(self.chest, include_superseded=False)
        self.assertNotIn("旧设定", md.split("## 附录")[0])

    def test_content_verbatim(self):
        """原文不能被改写。"""
        self._seed()
        md = export_markdown(self.chest)
        self.assertIn("残兽总述。", md)
        self.assertIn("强度分级。", md)

    def test_organize_finds_duplicates(self):
        same = "完全一样的一段设定内容，用来触发重复检测。" * 3
        self.chest.upsert("world", "甲", same)
        self.chest.upsert("world", "乙", same)
        rep = organize(self.chest)
        self.assertTrue(rep["duplicates"])
        self.assertGreaterEqual(rep["duplicates"][0]["similarity"], DUP_THRESHOLD)

    def test_organize_no_auto_merge(self):
        """整理只报告，绝不改动内容。"""
        same = "一模一样的内容" * 20
        self.chest.upsert("world", "甲", same)
        self.chest.upsert("world", "乙", same)
        before = self.chest.counts()
        organize(self.chest)
        self.assertEqual(self.chest.counts(), before)

    def test_organize_reports_orphans(self):
        self.chest.upsert("world", "孤立", "没有关系")
        rep = organize(self.chest)
        self.assertTrue(any(o["entity"] == "孤立" for o in rep["orphans"]))

    def test_organize_reports_superseded(self):
        self._seed()
        rep = organize(self.chest)
        self.assertTrue(any(s["entity"] == "旧设定" for s in rep["superseded"]))

    def test_empty_library_export(self):
        md = export_markdown(self.chest, title="空")
        self.assertIn("# 空", md)


# ==========================================================================
class TestImporter(unittest.TestCase):
    DOC = """一、地理与气候
涟城是沿海城市，三面环海，夏季湿热多雾。地形以丘陵为主。

二、人物速查
白杨是本作主角，大学生，能变成白露。

三、逐章细纲
这里应该被跳过，因为它是写作产物不是设定。
"""

    def test_split_by_cn_heading(self):
        secs = split_sections(self.DOC)
        titles = [s["title"] for s in secs]
        self.assertIn("地理与气候", titles)
        self.assertIn("人物速查", titles)

    def test_guess_layer(self):
        self.assertEqual(guess_layer("人物速查"), "character")
        self.assertEqual(guess_layer("伏笔登记"), "foreshadowing")
        self.assertEqual(guess_layer("时间线"), "timeline")
        self.assertEqual(guess_layer("地理与气候"), "world")

    def test_plan_skips_writing_product(self):
        plan = plan_import(self.DOC)
        titles = [i["entity"] for i in plan["items"]]
        self.assertNotIn("逐章细纲", titles)
        self.assertTrue(any("逐章细纲" in s["title"] for s in plan["skipped"]))

    def test_plan_counts(self):
        plan = plan_import(self.DOC)
        self.assertEqual(plan["to_import"], 2)
        self.assertGreater(plan["total_chars"], 0)

    def test_overrides(self):
        plan = plan_import(self.DOC, overrides={"地理": "timeline"})
        item = next(i for i in plan["items"] if i["entity"] == "地理与气候")
        self.assertEqual(item["layer"], "timeline")

    def test_min_chars_filters(self):
        plan = plan_import("一、标题\n短短\n", min_chars=50)
        self.assertEqual(plan["to_import"], 0)

    def test_run_import_writes(self):
        d = tempfile.mkdtemp()
        try:
            chest = EnderChest(os.path.join(d, "t.db"))
            plan = plan_import(self.DOC)
            res = run_import(chest, plan)
            self.assertEqual(len(res["written"]), plan["to_import"])
            self.assertIsNotNone(chest.get("world", "地理与气候"))
            chest.close()
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_duplicate_titles_numbered(self):
        doc = "一、同名\n" + "内容内容" * 10 + "\n\n二、同名\n" + "别的内容" * 10 + "\n"
        plan = plan_import(doc)
        names = [i["entity"] for i in plan["items"]]
        self.assertEqual(len(names), len(set(names)))


# ==========================================================================
class TestAssertionsSeed(ChestCase):
    def test_all_builtin_valid(self):
        """内置断言必须全部通过校验——否则说明写错了。"""
        for spec in BUILTIN_ASSERTIONS:
            detect = spec.get("detect_all") or []
            problems = validate(
                str(spec.get("rule") or ""),
                detect,
                allow_single=bool(spec.get("allow_single")),
                when=spec.get("when") or (),
            )
            self.assertEqual(problems, [], f"{spec.get('id')}: {problems}")

    def test_seed_writes_all(self):
        res = seed_builtin(self.chest)
        self.assertEqual(len(res["added"]), len(BUILTIN_ASSERTIONS))
        self.assertEqual(res["rejected"], [])

    def test_seed_idempotent(self):
        seed_builtin(self.chest)
        res = seed_builtin(self.chest)
        self.assertEqual(res["added"], [])
        self.assertEqual(len(res["skipped"]), len(BUILTIN_ASSERTIONS))

    def test_seed_overwrite(self):
        seed_builtin(self.chest)
        res = seed_builtin(self.chest, overwrite=True)
        self.assertEqual(len(res["added"]), len(BUILTIN_ASSERTIONS))

    def test_seeded_assertions_work(self):
        seed_builtin(self.chest)
        code = "bool operator ==(A a, A b) { if (a == null) return false; }"
        hits = guard(self.chest, code)
        self.assertTrue(any(h.assertion.id == "unity-operator-eq-null-recursion" for h in hits))

    def test_seeded_task_assertions_work(self):
        seed_builtin(self.chest)
        hits = guard(self.chest, "", task="回写 DLL 到 Assembly-CSharp")
        ids = {h.assertion.id for h in hits}
        self.assertIn("unity-writeback-field-order", ids)


if __name__ == "__main__":
    unittest.main()
