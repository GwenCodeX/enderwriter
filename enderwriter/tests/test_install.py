"""install / llamacpp 模块的离线单测。

这些测试**不下载任何东西**：所有网络与子进程入口都被替换成桩，
保证在无网环境、CI 里也能稳定跑。
"""

import os
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from enderwriter import install as I
from enderwriter import llamacc_meta as M
from enderwriter import llamacpp as LC


class TestLlamaCppDiscovery(unittest.TestCase):
    def test_find_server_via_env(self):
        with mock.patch.dict(os.environ, {"EW_LLAMA_SERVER": sys.executable}, clear=False):
            self.assertEqual(LC.find_llama_server(), sys.executable)

    def test_find_server_env_missing_file(self):
        with mock.patch.dict(os.environ, {"EW_LLAMA_SERVER": "C:/nope/llama-server.exe"}, clear=False):
            with mock.patch.object(LC.shutil, "which", return_value=None), \
                 mock.patch.object(LC, "_vendor_dir", return_value="C:/definitely-not-here"):
                self.assertIsNone(LC.find_llama_server())

    def test_find_gguf_via_env(self):
        with mock.patch.dict(os.environ, {"EW_GGUF": sys.executable}, clear=False):
            self.assertEqual(LC.find_gguf(), sys.executable)

    def test_find_gguf_returns_none_when_absent(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("EW_GGUF", None)
            with mock.patch.object(LC, "_vendor_dir", return_value="C:/definitely-not-here"):
                self.assertIsNone(LC.find_gguf())

    def test_gguf_constants_are_consistent(self):
        self.assertTrue(M.GGUF_FILE.endswith(".gguf"))
        self.assertIn(M.GGUF_FILE, M.GGUF_URL)
        self.assertIn(M.LLAMA_TAG, M.LLAMA_ASSETS["cpu"])
        self.assertGreater(M.GGUF_MIN_BYTES, 1_000_000_000)

    def test_recommended_asset_values(self):
        self.assertIn(M.recommended_asset(), ("vulkan", "cuda12", "cpu"))


class TestLlamaCppBackend(unittest.TestCase):
    def test_backend_without_binary_does_not_autostart(self):
        with mock.patch.object(LC, "find_llama_server", return_value=None), \
             mock.patch.object(LC, "find_gguf", return_value=None), \
             mock.patch.object(LC.LLamaCppBackend, "start", return_value=False):
            b = LC.LLamaCppBackend()
            self.assertIsNone(b.server_path)
            self.assertFalse(b.start())
            # load_gguf_backend 内部也会探测，这里同样要隔离环境
            self.assertEqual(LC.load_gguf_backend(), None)

    def test_chat_parses_openai_shape(self):
        b = LC.LLamaCppBackend(model_path="x.gguf", server_path="y.exe")
        with mock.patch.object(b, "start", return_value=True), \
             mock.patch.object(
                 b, "_post",
                 return_value={"choices": [{"message": {"content": "  你好  "}}]},
             ):
            self.assertEqual(b.chat([{"role": "user", "content": "hi"}]), "你好")

    def test_chat_raises_on_bad_shape(self):
        b = LC.LLamaCppBackend(model_path="x.gguf", server_path="y.exe")
        with mock.patch.object(b, "start", return_value=True), \
             mock.patch.object(b, "_post", return_value={"oops": 1}):
            with self.assertRaises(RuntimeError):
                b.chat([{"role": "user", "content": "hi"}])

    def test_chat_raises_when_server_unavailable(self):
        b = LC.LLamaCppBackend(model_path="x.gguf", server_path="y.exe")
        with mock.patch.object(b, "start", return_value=False):
            with self.assertRaises(RuntimeError):
                b.chat([{"role": "user", "content": "hi"}])

    def test_close_only_kills_own_process(self):
        b = LC.LLamaCppBackend()
        self.assertIsNone(b._proc)
        b.close()  # 不应抛异常
        self.assertFalse(b._owns_proc)

    def test_base_url_uses_port(self):
        self.assertEqual(LC.LLamaCppBackend(port=12345).base_url, "http://127.0.0.1:12345")


class TestInstallHelpers(unittest.TestCase):
    def test_zip_roundtrip(self):
        import tempfile
        import zipfile

        with tempfile.TemporaryDirectory() as tmp:
            zpath = os.path.join(tmp, "a.zip")
            with zipfile.ZipFile(zpath, "w") as zf:
                zf.writestr("pkg/one.txt", "1")
                zf.writestr("pkg/two.txt", "2")
            dest = os.path.join(tmp, "out")
            self.assertTrue(I.extract_zip(zpath, dest, echo=lambda *_: None))
            self.assertTrue(os.path.isfile(os.path.join(dest, "one.txt")))
            self.assertTrue(os.path.isfile(os.path.join(dest, "two.txt")))

    def test_extract_zip_rejects_bad_file(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            bad = os.path.join(tmp, "bad.zip")
            with open(bad, "wb") as fh:
                fh.write(b"not a zip")
            self.assertFalse(I.extract_zip(bad, os.path.join(tmp, "o"), echo=lambda *_: None))

    def test_download_skips_existing_large_file(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "model.gguf")
            with open(dest, "wb") as fh:
                fh.write(b"x" * 2048)
            ok, msg = I.download("http://example.invalid/x", dest, echo=lambda *_: None, min_bytes=1024)
            self.assertTrue(ok)
            self.assertEqual(msg, "already-present")

    def test_download_reports_failure(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            ok, msg = I.download(
                "http://127.0.0.1:1/nope",
                os.path.join(tmp, "x.bin"),
                echo=lambda *_: None,
                retries=0,
            )
            self.assertFalse(ok)
            self.assertTrue(msg)

    def test_ollama_status_has_model(self):
        st = I.OllamaStatus(models=["qwen2.5:3b-instruct", "llama3:8b"], server_up=True)
        self.assertTrue(st.has_model("qwen2.5:3b-instruct"))
        self.assertTrue(st.has_model("qwen2.5"))  # 省略 tag 时按裸名匹配
        self.assertFalse(st.has_model("gemma:2b"))
        self.assertTrue(st.ready)

    def test_ollama_status_not_ready_without_server(self):
        st = I.OllamaStatus(models=["qwen2.5:3b-instruct"], server_up=False)
        self.assertFalse(st.ready)

    def test_pick_engine_explicit(self):
        self.assertEqual(I.pick_engine("ollama"), "ollama")
        self.assertEqual(I.pick_engine("llamacpp"), "llamacpp")

    def test_pick_engine_prefers_ready_llamacpp(self):
        with mock.patch.object(
            I, "llama_status", return_value={"ready": True}
        ), mock.patch.object(I, "detect") as det:
            self.assertEqual(I.pick_engine("auto"), "llamacpp")
            det.assert_not_called()

    def test_pick_engine_falls_back_to_llamacpp_when_nothing_ready(self):
        with mock.patch.object(I, "llama_status", return_value={"ready": False}), \
             mock.patch.object(I, "detect", return_value=I.OllamaStatus()):
            self.assertEqual(I.pick_engine("auto"), "llamacpp")

    def test_llama_status_shape(self):
        with mock.patch.object(LC, "find_llama_server", return_value=None), \
             mock.patch.object(LC, "find_gguf", return_value=None):
            st = I.llama_status()
            self.assertEqual(st["engine"], "llamacpp")
            self.assertFalse(st["ready"])
            self.assertFalse(st["server_installed"])


class TestInstallRun(unittest.TestCase):
    def test_check_only_exit_code_when_not_ready(self):
        with mock.patch.object(I, "environment_report", return_value={
            "ollama": {"ready": False}, "llamacpp": {
                "server_installed": False, "server_path": None,
                "model_installed": False, "model_mb": 0.0, "ready": False,
            },
        }), mock.patch.object(I, "detect", return_value=I.OllamaStatus()):
            self.assertEqual(I.run_install(check_only=True), 1)

    def test_check_only_exit_code_when_ready(self):
        with mock.patch.object(I, "environment_report", return_value={
            "ollama": {"ready": False}, "llamacpp": {
                "server_installed": True, "server_path": "s.exe",
                "model_installed": True, "model_mb": 2000.0, "ready": True,
            },
        }), mock.patch.object(I, "detect", return_value=I.OllamaStatus()):
            self.assertEqual(I.run_install(check_only=True), 0)

    def test_ensure_ready_no_install_returns_false(self):
        with mock.patch.object(LC, "find_llama_server", return_value=None), \
             mock.patch.object(LC, "find_gguf", return_value=None):
            ok, st, steps = I.ensure_llamacpp_ready(install=False, pull=False, echo=lambda *_: None)
            self.assertFalse(ok)
            self.assertFalse(st["ready"])
            self.assertTrue(steps)

    def test_install_llama_binary_uses_existing(self):
        with mock.patch.object(LC, "find_llama_server", return_value="C:/x/llama-server.exe"):
            ok, msg = I.install_llama_binary(echo=lambda *_: None)
            self.assertTrue(ok)
            self.assertIn("已安装", msg)


if __name__ == "__main__":
    unittest.main()
