"""分层记忆（作品库 + 全局库）的单元测试。

全部在临时目录里跑，不碰真实数据，也不需要网络。
"""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from enderwriter import memory as M
from enderwriter.bridge import handle


class TestRouting(unittest.TestCase):
    def test_style_goes_global(self):
        self.assertEqual(M.route("style"), M.SCOPE_GLOBAL)

    def test_other_layers_go_work(self):
        for layer in ("character", "world", "timeline", "foreshadowing", "item"):
            self.assertEqual(M.route(layer), M.SCOPE_WORK, layer)

    def test_explicit_scope_wins(self):
        self.assertEqual(M.route("character", scope="global"), M.SCOPE_GLOBAL)
        self.assertEqual(M.route("style", scope="work"), M.SCOPE_WORK)

    def test_unknown_scope_falls_back_to_layer(self):
        self.assertEqual(M.route("character", scope="bogus"), M.SCOPE_WORK)

    def test_layer_partition_is_complete(self):
        from enderwriter.common import LAYERS

        self.assertEqual(set(M.GLOBAL_LAYERS) | set(M.WORK_LAYERS), set(LAYERS))


class TestSlugify(unittest.TestCase):
    def test_ascii(self):
        self.assertEqual(M.slugify("My Novel"), "My_Novel")

    def test_empty(self):
        self.assertEqual(M.slugify(""), "untitled")
        self.assertEqual(M.slugify("   "), "untitled")

    def test_path_separators_removed(self):
        s = M.slugify("a/b\\c:d")
        self.assertNotIn("/", s)
        self.assertNotIn("\\", s)
        self.assertNotIn(":", s)

    def test_cjk_kept_and_hashed(self):
        s = M.slugify("示例卷")
        self.assertTrue(s.startswith("示例卷"))
        self.assertNotEqual(s, M.slugify("青霄剑宗"))

    def test_long_name_truncated(self):
        self.assertLessEqual(len(M.slugify("x" * 100)), 46)


class TestWorkMemory(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="ew_mem_")
        self.mem = M.WorkMemory("测试作品", home=self.home)

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def test_paths_are_layered(self):
        self.assertIn(os.path.join("works"), self.mem.work_path)
        self.assertTrue(self.mem.work_path.endswith(".db"))
        self.assertTrue(self.mem.global_path.endswith("global.db"))
        self.assertNotEqual(self.mem.work_path, self.mem.global_path)

    def test_creates_dirs_on_open(self):
        chest = self.mem.open(M.SCOPE_WORK)
        chest.close()
        self.assertTrue(os.path.isfile(self.mem.work_path))
        chest = self.mem.open(M.SCOPE_GLOBAL)
        chest.close()
        self.assertTrue(os.path.isfile(self.mem.global_path))

    def test_list_works(self):
        chest = self.mem.open(M.SCOPE_WORK)
        chest.close()
        works = M.list_works(self.home)
        self.assertEqual(len(works), 1)
        self.assertIn("测试作品", works[0]["id"])


class TestBridgeLayering(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="ew_br_")

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def call(self, **kw):
        kw.setdefault("home", self.home)
        return handle(kw)

    def test_remember_routes_by_layer(self):
        r = self.call(op="remember", layer="character", entity="张三", content="主角", work="书A")
        self.assertTrue(r["ok"])
        self.assertEqual(r["scope"], M.SCOPE_WORK)

        r = self.call(op="remember", layer="style", entity="节奏", content="慢热易流失", work="书A")
        self.assertTrue(r["ok"])
        self.assertEqual(r["scope"], M.SCOPE_GLOBAL)

    def test_remember_explicit_scope_overrides(self):
        r = self.call(op="remember", layer="world", entity="大陆", content="斗气大陆", scope="global", work="书A")
        self.assertEqual(r["scope"], M.SCOPE_GLOBAL)

    def test_remember_requires_fields(self):
        self.assertFalse(self.call(op="remember", layer="character", entity="", content="x")["ok"])
        self.assertFalse(self.call(op="remember", layer="character", entity="x", content="")["ok"])

    def test_remember_rejects_bad_layer(self):
        r = self.call(op="remember", layer="nope", entity="x", content="y")
        self.assertFalse(r["ok"])

    def test_recall_sees_both_layers(self):
        self.call(op="remember", layer="character", entity="张三", content="青霄剑宗弟子", work="书A")
        self.call(op="remember", layer="item", entity="断魂钉", content="宗门凭物", work="书A")
        self.call(op="remember", layer="style", entity="开篇节奏", content="前三章建立处境", work="书A")

        r = self.call(op="recall", entities=["张三", "开篇节奏"], work="书A", budget=800)
        self.assertTrue(r["ok"])
        self.assertIn(M.SCOPE_WORK, r["scopes"])
        self.assertIn(M.SCOPE_GLOBAL, r["scopes"])

        work_text = str(r["scopes"][M.SCOPE_WORK])
        global_text = str(r["scopes"][M.SCOPE_GLOBAL])
        self.assertIn("张三", work_text)
        self.assertIn("开篇节奏", global_text)

    def test_works_are_isolated(self):
        self.call(op="remember", layer="character", entity="张三", content="书A 的主角", work="书A")
        r = self.call(op="recall", entities=["张三"], work="书B", budget=400)
        w = r["scopes"][M.SCOPE_WORK]
        total = sum(len(w.get(b) or []) for b in ("high", "medium", "low"))
        self.assertEqual(total, 0)

    def test_global_layer_shared_across_works(self):
        self.call(op="remember", layer="style", entity="伏笔密度", content="单章不超2条", work="书A")
        r = self.call(op="recall", entities=["伏笔密度"], work="书B", budget=400)
        g = r["scopes"][M.SCOPE_GLOBAL]
        total = sum(len(g.get(b) or []) for b in ("high", "medium", "low"))
        self.assertGreater(total, 0, "全局库应跨作品共享")

    def test_works_op(self):
        self.call(op="remember", layer="character", entity="甲", content="x", work="书A")
        r = self.call(op="works")
        self.assertTrue(r["ok"])
        self.assertEqual(r["count"], 1)

    def test_overview_shape(self):
        self.call(op="remember", layer="character", entity="甲", content="x", work="书A")
        self.call(op="remember", layer="style", entity="节奏", content="y", work="书A")
        r = self.call(op="overview", work="书A")
        self.assertTrue(r["ok"])
        self.assertEqual(r["work_block"]["name"], "书A")
        self.assertEqual(r["work_block"]["stats"]["total_records"], 1)
        self.assertEqual(len(r["global_block"]["lessons"]), 1)

    def test_recall_without_work_uses_global_only(self):
        self.call(op="remember", layer="style", entity="节奏", content="z")
        r = self.call(op="recall", entities=["节奏"], budget=400)
        self.assertNotIn(M.SCOPE_WORK, r["scopes"])
        self.assertIn(M.SCOPE_GLOBAL, r["scopes"])

    def test_persistence_across_reopen(self):
        self.call(op="remember", layer="character", entity="甲", content="第一次写入", work="书A")
        # 新进程语义：重新构造 WorkMemory 读同一个 home
        mem = M.WorkMemory("书A", home=self.home)
        chest = mem.open(M.SCOPE_WORK)
        try:
            rec = chest.get("character", "甲")
            self.assertIsNotNone(rec)
            self.assertEqual(rec.content, "第一次写入")
        finally:
            chest.close()

    def test_entries_layer_aware(self):
        self.call(op="remember", layer="character", entity="张三", content="主角", work="书A")
        r = self.call(op="entries", layer="character", work="书A")
        self.assertTrue(r["ok"])
        self.assertEqual(r["count"], 1)
        self.assertEqual(r["items"][0]["entity"], "张三")

    def test_entries_never_injects_seed_data(self):
        """回归：没指定 work/db 时绝不能凭空灌示例数据。

        曾经 entries 的条件写成 `if work and not db`，面板不传 work 时会
        掉进临时库分支并 seed_demo()，于是新对话"浏览条目"看到的是
        示例故事的张三/李四/王五/赵六。
        """
        r = self.call(op="entries", layer="character", session_id="session-brand-new-0000")
        self.assertTrue(r["ok"])
        self.assertEqual(r["count"], 0, f"不该有任何条目，实际: {r.get('items')}")
        self.assertEqual(r.get("items"), [])

    def test_stats_never_injects_seed_data(self):
        r = self.call(op="stats", session_id="session-brand-new-1111")
        self.assertTrue(r["ok"])
        self.assertEqual(r["stats"]["total_records"], 0)

    def test_patrol_never_injects_seed_data(self):
        r = self.call(op="patrol", chapter="ch_001", session_id="session-brand-new-2222")
        self.assertTrue(r["ok"])
        self.assertEqual(r["items"], [])

    def test_search_never_injects_seed_data(self):
        r = self.call(
            op="search", entities=["张三"], query_context="x",
            session_id="session-brand-new-3333", budget=400,
        )
        self.assertTrue(r["ok"])
        total = sum(r["counts"].values())
        self.assertEqual(total, 0)

    def test_entries_missing_work_db(self):
        r = self.call(op="entries", layer="character", work="不存在的作品")
        self.assertTrue(r["ok"])
        self.assertEqual(r["count"], 0)


class TestSessionWork(unittest.TestCase):
    """默认按对话隔离：不同 session 各有一套独立的作品库。"""

    def test_session_work_is_stable(self):
        a = M.session_work("session-aaaa-1111-2222-3333-444455556666")
        b = M.session_work("session-aaaa-1111-2222-3333-444455556666")
        self.assertEqual(a, b)
        self.assertTrue(a.startswith(M.SESSION_PREFIX))

    def test_different_sessions_differ(self):
        a = M.session_work("session-aaaa-1111")
        b = M.session_work("session-bbbb-2222")
        self.assertNotEqual(a, b)

    def test_empty_session(self):
        self.assertEqual(M.session_work(""), "")

    def test_resolve_work_prefers_explicit(self):
        sid = "session-xyz-1234"
        self.assertEqual(M.resolve_work(work="示例卷", session_id=sid), "示例卷")

    def test_resolve_work_falls_back_to_session(self):
        sid = "session-xyz-1234"
        got = M.resolve_work(work="", session_id=sid)
        self.assertEqual(got, M.session_work(sid))

    def test_resolve_work_without_session(self):
        self.assertEqual(M.resolve_work(work=None, session_id=None), "")


class TestSessionIsolation(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="ew_sess_")
        self.a = "session-aaaa-1111-2222-3333-444455556666"
        self.b = "session-bbbb-7777-8888-9999-000011112222"

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def call(self, **kw):
        kw.setdefault("home", self.home)
        return handle(kw)

    def names(self, res, scope):
        r = res["scopes"].get(scope) or {}
        return [h["entity"] for b in ("high", "medium", "low") for h in (r.get(b) or [])]

    def test_two_sessions_are_isolated(self):
        self.call(op="remember", layer="character", entity="张三", content="A 的人物", session_id=self.a)
        self.call(op="remember", layer="character", entity="赵六", content="B 的人物", session_id=self.b)

        ra = self.call(op="recall", entities=["张三", "赵六"], session_id=self.a, budget=600)
        self.assertIn("张三", self.names(ra, M.SCOPE_WORK))
        self.assertNotIn("赵六", self.names(ra, M.SCOPE_WORK))

        rb = self.call(op="recall", entities=["张三", "赵六"], session_id=self.b, budget=600)
        self.assertIn("赵六", self.names(rb, M.SCOPE_WORK))
        self.assertNotIn("张三", self.names(rb, M.SCOPE_WORK))

    def test_global_layer_shared_between_sessions(self):
        self.call(op="remember", layer="style", entity="开篇节奏", content="前三章建立处境", session_id=self.a)
        rb = self.call(op="recall", entities=["开篇节奏"], session_id=self.b, budget=400)
        self.assertIn("开篇节奏", self.names(rb, M.SCOPE_GLOBAL))

    def test_explicit_work_overrides_session(self):
        self.call(op="remember", layer="character", entity="张三", content="共享作品的人物", work="共享本", session_id=self.a)
        rb = self.call(op="recall", entities=["张三"], work="共享本", session_id=self.b, budget=400)
        self.assertIn("张三", self.names(rb, M.SCOPE_WORK))

    def test_clear_session_scope_only_affects_one(self):
        self.call(op="remember", layer="character", entity="张三", content="A", session_id=self.a)
        self.call(op="remember", layer="character", entity="赵六", content="B", session_id=self.b)
        r = self.call(op="clear", target="work", session_id=self.a)
        self.assertEqual(r["ok_count"], 1)
        rb = self.call(op="recall", entities=["赵六"], session_id=self.b, budget=400)
        self.assertIn("赵六", self.names(rb, M.SCOPE_WORK))

    def test_works_marks_current(self):
        self.call(op="remember", layer="character", entity="甲", content="x", session_id=self.a)
        r = self.call(op="works", session_id=self.a)
        cur = [w for w in r["works"] if w.get("is_current")]
        self.assertEqual(len(cur), 1)


class TestClear(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="ew_clr_")

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def call(self, **kw):
        kw.setdefault("home", self.home)
        return handle(kw)

    def seed(self, work="书A"):
        self.call(op="remember", layer="character", entity="张三", content="主角", work=work)
        self.call(op="remember", layer="style", entity="节奏", content="经验", work=work)

    def test_clear_global_keeps_work(self):
        self.seed()
        r = self.call(op="clear", target="global")
        self.assertTrue(r["ok"])
        self.assertEqual(r["ok_count"], 1)
        ov = self.call(op="overview", work="书A")
        self.assertEqual(ov["work_block"]["stats"]["total_records"], 1)
        self.assertEqual(len(ov["global_block"].get("lessons", [])), 0)

    def test_clear_work_keeps_global(self):
        self.seed()
        r = self.call(op="clear", target="work", work="书A")
        self.assertTrue(r["ok"])
        ov = self.call(op="overview", work="书A")
        self.assertFalse(ov["work_block"]["exists"])
        self.assertEqual(len(ov["global_block"].get("lessons", [])), 1)

    def test_clear_all(self):
        self.seed()
        r = self.call(op="clear", target="all", work="书A")
        self.assertTrue(r["ok"])
        self.assertEqual(r["ok_count"], 2)
        ov = self.call(op="overview", work="书A")
        self.assertFalse(ov["work_block"]["exists"])
        self.assertFalse(ov["global_block"]["exists"])

    def test_clear_creates_backup(self):
        self.seed()
        r = self.call(op="clear", target="work", work="书A")
        backups = r["backups"]
        self.assertEqual(len(backups), 1)
        self.assertTrue(os.path.isfile(backups[0]))
        self.assertIn(".bak-", backups[0])

    def test_clear_missing_is_not_error(self):
        r = self.call(op="clear", target="work", work="不存在的书")
        self.assertTrue(r["ok"])
        self.assertEqual(r["ok_count"], 0)
        self.assertEqual(r["cleared"][0]["status"], "not_found")

    def test_clear_requires_target(self):
        self.assertFalse(self.call(op="clear")["ok"])

    def test_clear_rejects_unknown_target(self):
        self.assertFalse(self.call(op="clear", target="bogus")["ok"])

    def test_clear_work_without_name_fails(self):
        self.assertFalse(self.call(op="clear", target="work")["ok"])

    def test_clear_by_explicit_name(self):
        self.seed("书X")
        r = self.call(op="clear", target="work:书X")
        self.assertTrue(r["ok"])
        self.assertEqual(r["ok_count"], 1)

    def test_cleared_data_really_gone(self):
        self.seed()
        self.call(op="clear", target="work", work="书A")
        r = self.call(op="recall", entities=["张三"], work="书A", budget=400)
        w = r["scopes"][M.SCOPE_WORK]
        self.assertEqual(sum(len(w.get(b) or []) for b in ("high", "medium", "low")), 0)


if __name__ == "__main__":
    unittest.main()
