"""本地 GGUF 推理后端（llama.cpp）。

为什么需要它：规范里"本地 Qwen 3B"这一层，本质要求是
**在本机跑一个 3B 级别模型、低显存占用、离线可调**。
Ollama 是官方推荐载体，但在境外大文件被限速的网络环境下，
1.5GB 安装包 + 1.9GB 模型常常根本下不动。

因此这里提供一条等价且更轻的落地路径：

    llama-server（llama.cpp，单文件二进制，约 30MB）
      +  ModelScope 的 Qwen2.5-3B-Instruct Q4_K_M GGUF

两者协议一致（都是 OpenAI 风格的 HTTP 服务），对上层完全透明——
`Orchestrator` / `MemoryKeeper` / `WriterBackAgent` 只认 :class:`LLMBackend`
接口，不关心底下是 Ollama 还是 llama.cpp。

后端选择优先级（``get_backend()``）：

    1. 显式指定 name
    2. 环境变量 EW_LLM
    3. 自动探测：llama-server 可用 → ollama 可用 → Stub

设计约束：只用标准库；llama-server 以子进程方式托管，退出时清理。
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any

from .common import get_logger

__all__ = [
    "DEFAULT_GGUF",
    "DEFAULT_PORT",
    "LLamaCppBackend",
    "find_llama_server",
    "find_gguf",
    "load_gguf_backend",
]

log = get_logger("llamacpp")

_IS_WIN = platform.system() == "Windows"
#: llama-server 默认端口（避开 Ollama 的 11434）
DEFAULT_PORT: int = 11435
#: 模型文件名约定
DEFAULT_GGUF: str = "qwen2.5-3b-instruct-q4_k_m.gguf"


# ==========================================================================
# 1. 定位二进制与模型
# ==========================================================================
def _vendor_dir() -> str:
    """包的 vendor 目录：<repo>/vendor。"""
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.abspath(os.path.join(here, os.pardir, "vendor"))


def find_llama_server() -> str | None:
    """找 llama-server 可执行文件。

    顺序：环境变量 EW_LLAMA_SERVER → PATH → vendor/llama/*
    """
    env = os.environ.get("EW_LLAMA_SERVER")
    if env and os.path.isfile(env):
        return env

    name = "llama-server.exe" if _IS_WIN else "llama-server"
    found = shutil.which("llama-server")
    if found:
        return found

    vendor = _vendor_dir()
    # 优先 GPU 版（vulkan/cuda），再退回 cpu 版
    for sub in ("llama-vulkan", "llama-cuda", "llama", ""):
        root = os.path.join(vendor, sub) if sub else vendor
        if not os.path.isdir(root):
            continue
        direct = os.path.join(root, name)
        if os.path.isfile(direct):
            return direct
        # release 包里通常再套一层 llama-b11707-bin-win-vulkan-x64/
        for entry in sorted(os.listdir(root)):
            cand = os.path.join(root, entry, name)
            if os.path.isfile(cand):
                return cand
    return None


def find_gguf(name: str = DEFAULT_GGUF) -> str | None:
    """找 GGUF 模型文件。顺序：EW_GGUF → vendor/models/*.gguf。"""
    env = os.environ.get("EW_GGUF")
    if env and os.path.isfile(env):
        return env

    models_dir = os.path.join(_vendor_dir(), "models")
    exact = os.path.join(models_dir, name)
    if os.path.isfile(exact):
        return exact
    if os.path.isdir(models_dir):
        candidates = sorted(
            os.path.join(models_dir, f)
            for f in os.listdir(models_dir)
            if f.lower().endswith(".gguf")
        )
        if candidates:
            return candidates[0]
    return None


# ==========================================================================
# 2. 进程托管
# ==========================================================================
def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) != 0


class LLamaCppBackend:
    """把 llama-server 当成 OpenAI 兼容的本地模型来用。

    * ``start()`` 拉起子进程并等就绪（已在线则直接复用）
    * ``generate()`` / ``chat()`` 走 ``/v1/chat/completions``
    * ``close()`` 只在"由我启动"时关掉进程，避免杀掉用户自己的服务
    """

    name = "llamacpp"

    def __init__(
        self,
        model_path: str | None = None,
        server_path: str | None = None,
        *,
        port: int = DEFAULT_PORT,
        n_ctx: int = 8192,
        n_gpu_layers: int = 99,
        autostart: bool = True,
    ) -> None:
        self.model_path = model_path or find_gguf()
        self.server_path = server_path or find_llama_server()
        self.port = int(port)
        self.n_ctx = int(n_ctx)
        self.n_gpu_layers = int(n_gpu_layers)
        self.autostart = bool(autostart)
        self._proc: subprocess.Popen[bytes] | None = None
        self._owns_proc = False
        self.calls: dict[str, int] = {"generate": 0, "chat": 0}

    # -- 生命周期 --------------------------------------------------------
    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _healthy(self, *, timeout: float = 1.5) -> bool:
        try:
            req = urllib.request.Request(f"{self.base_url}/health")
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - 本机
                return resp.status == 200
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError):
            return False

    def start(self, *, wait: float = 180.0) -> bool:
        """确保服务在线。返回是否就绪。"""
        if self._healthy():
            log.debug("llama-server 已在 %s 运行，复用", self.base_url)
            return True
        if not self.autostart:
            return False
        if not self.server_path or not os.path.isfile(self.server_path):
            log.warning("找不到 llama-server 可执行文件（EW_LLAMA_SERVER 或 vendor/llama/）")
            return False
        if not self.model_path or not os.path.isfile(self.model_path):
            log.warning("找不到 GGUF 模型文件（EW_GGUF 或 vendor/models/）")
            return False

        cmd = [
            self.server_path,
            "-m", self.model_path,
            "--port", str(self.port),
            "-c", str(self.n_ctx),
            "-ngl", str(self.n_gpu_layers),
            "--no-webui",
            "-t", str(max(2, (os.cpu_count() or 4) // 2)),
        ]
        log.info("启动 llama-server：%s", " ".join(cmd))
        popen_kw: dict[str, Any] = {
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "stdin": subprocess.DEVNULL,
        }
        if _IS_WIN:
            popen_kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self._proc = subprocess.Popen(cmd, **popen_kw)  # noqa: S603
            self._owns_proc = True
        except OSError as exc:
            log.warning("启动 llama-server 失败：%s", exc)
            return False

        deadline = time.time() + wait
        while time.time() < deadline:
            if self._healthy(timeout=1.0):
                return True
            if self._proc.poll() is not None:
                log.warning("llama-server 提前退出（退出码 %s）", self._proc.returncode)
                return False
            time.sleep(1.0)
        log.warning("llama-server 在 %.0f 秒内未就绪", wait)
        return False

    def close(self) -> None:
        """只管自己启动的进程。"""
        if self._owns_proc and self._proc is not None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=10)
            except (subprocess.TimeoutExpired, OSError):
                try:
                    self._proc.kill()
                except OSError:
                    pass
            self._proc = None
            self._owns_proc = False

    def __enter__(self) -> "LLamaCppBackend":
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- 推理 ------------------------------------------------------------
    def _post(self, path: str, payload: dict[str, Any], *, timeout: float = 300.0) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - 本机
            return json.loads(resp.read().decode("utf-8", "replace"))

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int = 512,
        temperature: float = 0.7,
    ) -> str:
        """标准 chat 补全。"""
        self.calls["chat"] += 1
        if not self.start():
            raise RuntimeError("llama-server 不可用：请先运行 `enderwriter install`")
        data = self._post(
            "/v1/chat/completions",
            {
                "messages": messages,
                "max_tokens": int(max_tokens),
                "temperature": float(temperature),
                "stream": False,
            },
        )
        try:
            return str(data["choices"][0]["message"]["content"]).strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"llama-server 返回结构异常：{data!r}") from exc

    def generate(self, prompt: str, *, max_tokens: int = 512) -> str:
        """单轮补全：把 prompt 当 user 消息，要求只出正文。"""
        self.calls["generate"] += 1
        system = "你是中文长篇小说写作助手。只输出正文段落，不要解释、不要小标题、不要 Markdown。"
        text = self.chat(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": str(prompt)},
            ],
            max_tokens=max_tokens,
        ).strip()
        if not text:
            raise RuntimeError("llama-server 返回空正文；可降低 --window 或换模型。")
        return text

    # ---- LLMBackend 协议的其余三个方法 ----------------------------------
    # 结构化输出（细纲 / 回写）对 3B 模型来说直接生成 JSON 很容易格式跑偏，
    # 因此策略是：**让模型写正文，让确定性逻辑管结构**——
    # 细纲与回写报告用本地确定性推导（保证 100% 过 Schema），
    # 关键词抽取用模型 + 词法双通道。

    def plan(self, outline_req: dict) -> dict:
        """章节细纲：确定性构造，保证 §3.1 九字段齐全且合法。"""
        from .llm import StubBackend

        return StubBackend().plan(outline_req)

    def writeback(self, text: str, outline: dict) -> dict:
        """回写报告：确定性推导（从正文/细纲抽事实），保证过 §3.5。"""
        from .llm import StubBackend

        return StubBackend().writeback(text, outline)

    def extract_keywords(self, text: str) -> list[str]:
        """关键词：先问模型，拿不到就退回确定性词法。"""
        from .llm import StubBackend

        try:
            raw = self.chat(
                [
                    {
                        "role": "system",
                        "content": "你是关键词抽取器。只输出关键词，每行一个，不要编号、不要解释。",
                    },
                    {"role": "user", "content": f"抽取人名、地名、物品、事件：\n{str(text)[:1500]}"},
                ],
                max_tokens=160,
            )
        except RuntimeError:
            return StubBackend().extract_keywords(text)
        out = []
        for line in raw.splitlines():
            item = line.strip().strip("，。、-*·•)）（(")
            if 1 < len(item) <= 30:
                out.append(item)
        if out:
            seen: list[str] = []
            for x in out:
                if x not in seen:
                    seen.append(x)
            return seen[:24]
        return StubBackend().extract_keywords(text)


# ==========================================================================
# 3. 便捷加载
# ==========================================================================
def load_gguf_backend(
    *,
    model_path: str | None = None,
    port: int = DEFAULT_PORT,
    n_gpu_layers: int = 99,
    autostart: bool = True,
) -> LLamaCppBackend | None:
    """尝试构造并启动后端；条件不满足返回 None。"""
    backend = LLamaCppBackend(
        model_path=model_path, port=port, n_gpu_layers=n_gpu_layers, autostart=autostart
    )
    if backend.server_path is None or backend.model_path is None:
        return None
    if autostart and not backend.start():
        return None
    return backend


if __name__ == "__main__":  # pragma: no cover
    srv = find_llama_server()
    gguf = find_gguf()
    print(f"llama-server: {srv}")
    print(f"GGUF: {gguf}")
    if srv and gguf:
        with LLamaCppBackend() as b:
            print(b.generate("用一句话介绍你自己", max_tokens=64))
    else:
        sys.exit(1)
