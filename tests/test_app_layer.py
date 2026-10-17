"""应用层（llm / seed / cli）的自测：离线、确定性、契约齐全。

运行：``cd <包根>; & $py -m unittest discover -s tests``
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from enderwriter import cli as CLI
from enderwriter import common as C
from enderwriter import llm as LLM
from enderwriter import seed as SEED
from enderwriter.chest import EnderChest


def _quiet(fn, *a, **kw):
    """在捕获 stdout/stderr 的前提下调用 fn，返回 (rc, out, err)。"""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = fn(*a, **kw)
    return rc, out.getvalue(), err.getvalue()


class TestStubBackend(unittest.TestCase):
    def setUp(self):
        self.b = LLM.StubBackend(vocabulary=["沈砚", "苏晚", "断魂崖", "照骨灯"])

    def test_plan_is_schema_valid(self):
        out = self.b.plan({
            "chapter_id": "ch_007",
            "chapter_goal": "回收照骨灯的誓言",
            "characters": [{"name": "沈砚", "importance": "core"}],
            "locations": ["断魂崖"],
            "items": ["照骨灯"],
            "resolve": ["fs_001"],
            "plant": [{"content": "灯芯裂纹多了一道"}],
        })
        C.validate(out, C.schema("chapter_outline"))
        self.assertEqual(set(C.schema("chapter_outline")["required"]) - set(out), set())
        self.assertEqual(out["chapter_id"], "ch_007")
        self.assertEqual(out["foreshadowing_to_resolve"][0]["id"], "fs_001")

    def test_plan_minimal_request_still_has_nine_fields(self):
        out = self.b.plan({"chapter_id": 5})
        self.assertEqual(out["chapter_id"], "ch_005")
        self.assertEqual(set(C.schema("chapter_outline")["required"]) - set(out), set())

    def test_plan_is_deterministic(self):
        req = {"chapter_id": "ch_002", "chapter_goal": "x"}
        self.assertEqual(self.b.plan(req), self.b.plan(dict(req)))

    def test_generate_reproducible_and_bounded(self):
        prompt = "角色: 沈砚, 苏晚\n地点: 断魂崖\n物品: 照骨灯\n目标: 夺灯\n情绪: 压抑"
        a = self.b.generate(prompt, max_tokens=120)
        b = self.b.generate(prompt, max_tokens=120)
        self.assertEqual(a, b)
        self.assertLessEqual(C.est_tokens(a), 120)
        self.assertGreater(len(a), 40)
        self.assertIn("沈砚", a)

    def test_writeback_is_schema_valid(self):
        outline = self.b.plan({
            "chapter_id": "ch_004",
            "characters": ["沈砚"],
            "resolve": ["fs_001", "fs_002"],
            "plant": [{"content": "新伏笔"}],
        })
        text = self.b.generate("角色: 沈砚", max_tokens=100)
        report = self.b.writeback(text, outline)
        C.validate(report, C.schema("writeback_report"))
        self.assertEqual(report["chapter_id"], "ch_004")
        statuses = {f["id"]: f["status"] for f in report["updated"]["foreshadowing"]}
        self.assertIn("fs_001", statuses)
        self.assertIn("planted", statuses.values())

    def test_writeback_placeholder_ids_avoid_known(self):
        b = LLM.StubBackend(known_foreshadowings=["fs_101", "fs_102"])
        report = b.writeback("正文", {"chapter_id": "ch_002", "foreshadowing_to_plant": [{"content": "a"}, {"content": "b"}]})
        ids = [f["id"] for f in report["updated"]["foreshadowing"]]
        self.assertNotIn("fs_101", ids)

    def test_extract_keywords(self):
        kw = self.b.extract_keywords("沈砚在断魂崖点亮了照骨灯。沈砚没有回头。")
        self.assertIn("沈砚", kw)
        self.assertIn("照骨灯", kw)

    def test_base_class_is_abstract(self):
        base = LLM.LLMBackend()
        for name, args in (("plan", ({},)), ("generate", ("x",)),
                           ("extract_keywords", ("x",)), ("writeback", ("x", {}))):
            with self.assertRaises(NotImplementedError):
                getattr(base, name)(*args)
        base.close()  # 默认 no-op


class TestOllamaBackend(unittest.TestCase):
    def test_bad_url_raises_runtime_error(self):
        b = LLM.OllamaBackend("http://127.0.0.1:9/api/generate", "dummy", timeout=2.0)
        with self.assertRaises(RuntimeError) as ctx:
            b.generate("hi")
        self.assertIn("Ollama", str(ctx.exception))

    def test_defaults_from_env(self):
        old_url, old_model = os.environ.get("EW_OLLAMA_URL"), os.environ.get("EW_OLLAMA_MODEL")
        try:
            os.environ["EW_OLLAMA_URL"] = "http://127.0.0.1:9/api/generate"
            os.environ["EW_OLLAMA_MODEL"] = "test-model"
            b = LLM.OllamaBackend()
            self.assertEqual(b.url, "http://127.0.0.1:9/api/generate")
            self.assertEqual(b.model, "test-model")
        finally:
            for k, v in (("EW_OLLAMA_URL", old_url), ("EW_OLLAMA_MODEL", old_model)):
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_loose_json_parsing(self):
        self.assertEqual(LLM._loads_json_loose('```json\n{"a":1}\n```'), {"a": 1})
        self.assertEqual(LLM._loads_json_loose('说明：{"a":2} 完'), {"a": 2})
        with self.assertRaises(C.SchemaError):
            LLM._loads_json_loose("没有 JSON")

    def test_schema_error_wrapper_is_readable(self):
        err = LLM.schema_error("plan", C.SchemaError("#: 缺少必填字段 'mood'"), {"chapter_id": "x"})
        self.assertIsInstance(err, C.SchemaError)
        self.assertIn("plan", str(err))
        self.assertIn("mood", str(err))


class TestGetBackend(unittest.TestCase):
    def test_default_is_stub(self):
        old = os.environ.pop("EW_LLM", None)
        try:
            self.assertIsInstance(LLM.get_backend(), LLM.StubBackend)
        finally:
            if old is not None:
                os.environ["EW_LLM"] = old

    def test_env_selects_stub(self):
        old = os.environ.get("EW_LLM")
        os.environ["EW_LLM"] = "stub"
        try:
            self.assertIsInstance(LLM.get_backend(), LLM.StubBackend)
        finally:
            if old is None:
                os.environ.pop("EW_LLM", None)
            else:
                os.environ["EW_LLM"] = old

    def test_unknown_name_raises(self):
        with self.assertRaises(ValueError):
            LLM.get_backend("bogus")

    def test_ollama_degrades_offline(self):
        old = os.environ.get("EW_OLLAMA_URL")
        os.environ["EW_OLLAMA_URL"] = "http://127.0.0.1:9/api/generate"
        try:
            guard = os.environ.pop("EW_LLM", None)
            with contextlib.redirect_stderr(io.StringIO()) as err:
                backend = LLM.get_backend("ollama")
            self.assertIsInstance(backend, LLM.StubBackend)
            self.assertIn("降级", err.getvalue())
            if guard is not None:
                os.environ["EW_LLM"] = guard
        finally:
            if old is None:
                os.environ.pop("EW_OLLAMA_URL", None)
            else:
                os.environ["EW_OLLAMA_URL"] = old


class TestSeed(unittest.TestCase):
    def setUp(self):
        self.chest = EnderChest(":memory:")
        self.stats = SEED.seed_demo(self.chest)

    def tearDown(self):
        self.chest.close()

    def test_required_counts(self):
        r = self.stats["records"]
        self.assertGreaterEqual(r["character"], 3)
        self.assertGreaterEqual(r["world"], 2)
        self.assertGreaterEqual(r["timeline"], 2)
        self.assertGreaterEqual(r["foreshadowing"], 3)
        self.assertGreaterEqual(r["item"], 2)
        self.assertGreaterEqual(r["style"], 1)
        self.assertGreaterEqual(self.stats["aliases"], 2)
        self.assertGreaterEqual(self.stats["edges"], 3)
        self.assertGreaterEqual(self.stats["states"], 3)
        self.assertEqual(len(self.stats["chapters"]), 2)

    def test_core_and_secondary_present(self):
        imps = {
            self.chest.get("character", e).data.get("importance")
            for e in self.chest.all_entities(layers=["character"])
        }
        self.assertIn("core", imps)
        self.assertIn("secondary", imps)

    def test_foreshadow_states_consistent(self):
        fss = self.chest.all_foreshadowings()
        self.assertGreaterEqual(len(fss), 3)
        self.assertTrue({"planted", "reinforced"} <= {f.status for f in fss})
        for f in fss:
            self.assertGreater(f.planted_no, 0, f"{f.id} 缺 planted_chapter")
            self.assertGreater(f.planned_no, f.planted_no, f"{f.id} planned_resolve 不晚于 planted")
            if f.resolved_chapter:
                self.assertGreaterEqual(C.chapter_index(f.resolved_chapter), f.planted_no)

    def test_aliases_edges_states(self):
        self.assertEqual(self.chest.resolve_entity("沈师弟"), "沈砚")
        self.assertEqual(self.chest.resolve_entity("小砚"), "沈砚")
        self.assertEqual(self.chest.resolve_entity("苏姑娘"), "苏晚")
        self.assertEqual(self.chest.resolve_entity("守灯人"), "苏晚")
        self.assertIn("沈师弟", self.chest.synonyms("沈砚"))
        self.assertTrue(self.chest.neighbors("沈砚"))
        self.assertIs(self.chest.get_state("沈砚", "alive"), True)
        self.assertIs(self.chest.get_state("裴照", "alive"), False)
        self.assertTrue(self.chest.get_state("沈砚", "realm"))

    def test_dead_character_consistent_with_text(self):
        ch1 = self.chest.get_chapter("ch_001")["text"]
        self.assertNotIn("裴照说", ch1)
        self.assertIs(self.chest.get_state("裴照", "alive"), False)

    def test_chapters_stored(self):
        for cid in ("ch_001", "ch_002"):
            self.assertIsNotNone(self.chest.get_chapter(cid))
        self.assertGreater(C.est_tokens(self.chest.get_chapter("ch_002")["text"]), 100)

    def test_seed_is_deterministic(self):
        with EnderChest(":memory:") as other:
            st2 = SEED.seed_demo(other)
            self.assertEqual({k: v for k, v in st2.items() if k != "entities"},
                             {k: v for k, v in self.stats.items() if k != "entities"})
            self.assertEqual(other.get_chapter("ch_001")["text"],
                             self.chest.get_chapter("ch_001")["text"])

    def test_seed_rejects_none(self):
        with self.assertRaises(TypeError):
            SEED.seed_demo(None)


class TestDraftRequest(unittest.TestCase):
    def test_roundtrip_into_plan(self):
        with EnderChest(":memory:") as chest:
            SEED.seed_demo(chest)
            req = LLM.draft_request(chest)
            self.assertEqual(req["chapter_id"], "ch_003")
            self.assertTrue(req["characters"])
            outline = LLM.StubBackend().plan(req)
            C.validate(outline, C.schema("chapter_outline"))

    def test_next_chapter_id_fallback(self):
        with EnderChest(":memory:") as chest:
            self.assertEqual(LLM.next_chapter_id(chest), "ch_001")
            chest.put_chapter("ch_009", "x")
            self.assertEqual(LLM.next_chapter_id(chest), "ch_010")


class TestCliBasics(unittest.TestCase):
    def test_help_and_no_command(self):
        rc, out, _ = _quiet(CLI.main, [])
        self.assertEqual(rc, 0)
        self.assertIn("demo", out)
        rc, out, _ = _quiet(CLI.main, ["--help"])
        self.assertEqual(rc, 0)
        self.assertIn("export-schemas", out)

    def test_stats(self):
        rc, out, _ = _quiet(CLI.main, ["stats"])
        self.assertEqual(rc, 0)
        self.assertIn("末影箱统计", out)
        self.assertIn("人物格", out)

    def test_stats_json(self):
        rc, out, _ = _quiet(CLI.main, ["stats", "--json"])
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual(data["records"]["character"], 4)

    def test_export_schemas(self):
        with tempfile.TemporaryDirectory() as d:
            dest = os.path.join(d, "schemas")
            rc, out, _ = _quiet(CLI.main, ["export-schemas", "--out", dest])
            self.assertEqual(rc, 0)
            files = sorted(os.listdir(dest))
            self.assertEqual(files, sorted(f"{n}.json" for n in C.SCHEMAS))
            with open(os.path.join(dest, "chapter_outline.json"), encoding="utf-8") as fh:
                self.assertIn("chapter_goal", fh.read())

    def test_query_rejects_empty_entities(self):
        rc, _, err = _quiet(CLI.main, ["query", "-e", " , "])
        self.assertNotEqual(rc, 0)
        self.assertIn("实体", err)

    def test_query_ok(self):
        rc, out, _ = _quiet(CLI.main, ["query", "-e", "沈师弟,断魂崖", "-q", "月蚀", "--budget", "300"])
        self.assertEqual(rc, 0)
        self.assertIn("search_memory", out)
        self.assertIn("沈师弟→沈砚", out)

    def test_query_json_is_memory_result(self):
        rc, out, _ = _quiet(CLI.main, ["query", "-e", "沈砚", "--json"])
        self.assertEqual(rc, 0)
        C.validate(json.loads(out), C.schema("memory_result"))

    def test_patrol_ok(self):
        rc, out, _ = _quiet(CLI.main, ["patrol", "--chapter", "ch_009"])
        self.assertEqual(rc, 0)
        self.assertIn("伏笔巡检", out)

    def test_patrol_bad_chapter(self):
        rc, _, err = _quiet(CLI.main, ["patrol", "--chapter", "九"])
        self.assertNotEqual(rc, 0)
        self.assertIn("非法章节号", err)

    def test_db_path_persists(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "ew.db")
            rc, _, _ = _quiet(CLI.main, ["stats", "--db", db])
            self.assertEqual(rc, 0)
            self.assertTrue(os.path.exists(db))
            rc, out, _ = _quiet(CLI.main, ["stats", "--db", db, "--json"])
            self.assertEqual(json.loads(out)["records"]["character"], 4)


class TestCliLazyImports(unittest.TestCase):
    """协作模块缺失时必须给出中文提示并返回非零码，且 cli 本身可导入。"""

    def test_not_ready_message(self):
        orig = CLI.lazy_import

        def boom(module):
            raise CLI.NotReady(module, "：模拟缺失")

        CLI.lazy_import = boom
        try:
            rc, out, err = _quiet(CLI.main, ["demo"])
        finally:
            CLI.lazy_import = orig
        self.assertEqual(rc, CLI.EXIT_NOT_READY)
        self.assertIn("模块尚未就绪", err)
        self.assertIn("retrieval", err)

    def test_missing_attribute(self):
        with self.assertRaises(CLI.NotReady):
            CLI.lazy_attr("cli", "definitely_not_here")

    def test_cli_imports_without_peers(self):
        # 只要 import 成功就说明没有顶层硬依赖
        self.assertTrue(callable(CLI.main))


class TestCliDemo(unittest.TestCase):
    def test_demo_runs_offline(self):
        rc, out, err = _quiet(CLI.main, ["demo", "--window", "6000", "--words", "220"])
        self.assertEqual(rc, 0, msg=err)
        for stage in ("①", "②", "③", "④", "⑤", "⑥"):
            self.assertIn(stage, out)
        self.assertIn("末影箱统计", out)
        self.assertIn("伏笔总览", out)
        self.assertIn("演示完成", out)
        self.assertIn("StubBackend", out)

    def test_demo_json_summary(self):
        rc, out, _ = _quiet(CLI.main, ["demo", "--words", "120", "--json"])
        self.assertEqual(rc, 0)
        payload = json.loads(out.strip().splitlines()[-1])
        C.validate(payload["outline"], C.schema("chapter_outline"))
        self.assertEqual(payload["backend"], "StubBackend")
        self.assertLessEqual(payload["context"]["total_tokens"], int(6000 * 0.7) + 6000 * 0.7)

    def test_demo_writes_chapter(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "demo.db")
            rc, out, _ = _quiet(CLI.main, ["demo", "--db", db, "--words", "150"])
            self.assertEqual(rc, 0, msg=out[-800:])
            with EnderChest(db) as chest:
                self.assertIsNotNone(chest.get_chapter("ch_003"))
                self.assertGreaterEqual(chest.stats()["chapters"], 3)

    def test_demo_llm_ollama_degrades(self):
        old = os.environ.get("EW_OLLAMA_URL")
        os.environ["EW_OLLAMA_URL"] = "http://127.0.0.1:9/api/generate"
        try:
            rc, out, err = _quiet(CLI.main, ["demo", "--llm", "ollama", "--words", "120"])
        finally:
            if old is None:
                os.environ.pop("EW_OLLAMA_URL", None)
            else:
                os.environ["EW_OLLAMA_URL"] = old
        self.assertEqual(rc, 0)
        self.assertIn("StubBackend", out)
        self.assertIn("降级", err)


class TestModuleEntry(unittest.TestCase):
    def test_main_module_importable(self):
        import importlib
        mod = importlib.import_module("enderwriter.__main__")
        self.assertTrue(callable(mod.main))


if __name__ == "__main__":
    unittest.main()
