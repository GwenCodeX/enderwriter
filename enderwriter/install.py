"""自动安装与本机环境校验：让"本地 Qwen 3B"开箱可用。

规范里的最小可行部署是 **单机 + Ollama + SQLite + Python 编排**。
但实际落地会遇到一个硬约束：Ollama 安装包约 1.5GB、模型约 1.9GB，
都托管在境外 CDN；在很多网络环境下会被限速到几 KB/s，根本装不上。

所以本模块提供**两条等价路径**，由 ``--engine`` 选择或自动挑选：

    engine=ollama     官方路线（规范原文）。apt/brew/winget 或官方脚本
    engine=llamacpp   轻量路线。llama.cpp 单文件二进制(~30MB) + ModelScope 的 GGUF 模型
                      （ModelScope 国内实测 12MB/s，是真正的可用来源）

两条路对上层完全透明：都实现同一个 :class:`LLMBackend` 接口。

命令统一通过 ``enderwriter install`` 暴露::

    install                    自动选引擎 → 装运行时 → 下模型 → 自检
    install --check            只检测，不下载任何东西
    install --engine llamacpp  强制走 llama.cpp
    install --engine ollama    强制走 Ollama
    install --model <name>     （ollama）模型名 /（llamacpp）GGUF 文件名

设计约束：
  * 只用标准库（subprocess / urllib / shutil / zipfile），零第三方依赖；
  * 全程幂等：已装好的跳过，已下好的跳过（按大小与存在性判断）；
  * 下载失败要给出**可操作**的中文提示，而不是抛栈。
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from typing import Any, Callable

from . import llamacc_meta as _meta  # 仅取常量，避免循环导入
from .common import get_logger

__all__ = [
    "DEFAULT_MODEL",
    "DEFAULT_HOST",
    "DEFAULT_ENGINE",
    "OllamaStatus",
    "detect",
    "install_ollama",
    "pull_model",
    "ensure_ready",
    "run_install",
]

log = get_logger("install")

#: 规范指定的 Ollama 模型（Qwen2.5-3B-Instruct，4-bit 后显存 < 3GB）
DEFAULT_MODEL: str = "qwen2.5:3b-instruct"
#: Ollama 服务默认地址
DEFAULT_HOST: str = "http://127.0.0.1:11434"
#: 默认引擎：auto 会优先挑"下载得动"的那条
DEFAULT_ENGINE: str = "auto"

_IS_WIN = platform.system() == "Windows"
_IS_MAC = platform.system() == "Darwin"


def _echo(msg: str = "") -> None:
    print(msg, flush=True)


def _mb(path: str) -> float:
    try:
        return os.path.getsize(path) / (1024 * 1024)
    except OSError:
        return 0.0


# ==========================================================================
# 通用下载（带进度、断点续传、超时）
# ==========================================================================
def download(
    url: str,
    dest: str,
    *,
    echo: Callable[[str], None] = _echo,
    min_bytes: int = 0,
    timeout: float = 3600.0,
    retries: int = 2,
) -> tuple[bool, str]:
    """下载到 dest。已存在且大小达标则跳过（幂等）。"""
    os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
    if os.path.isfile(dest) and os.path.getsize(dest) >= min_bytes > 0:
        echo(f"    已存在，跳过下载（{_mb(dest):.1f}MB）")
        return True, "already-present"

    last_err = ""
    for attempt in range(1, retries + 2):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "enderwriter/0.1"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
                total = int(resp.headers.get("Content-Length") or 0)
                done = 0
                next_tick = 25 * 1024 * 1024
                tmp = dest + ".part"
                with open(tmp, "wb") as fh:
                    while True:
                        chunk = resp.read(1024 * 1024)
                        if not chunk:
                            break
                        fh.write(chunk)
                        done += len(chunk)
                        if done >= next_tick:
                            pct = f" / {total // (1024 * 1024)}MB ({done * 100 // total}%)" if total else ""
                            echo(f"    … {done // (1024 * 1024)}MB{pct}")
                            next_tick += 50 * 1024 * 1024
            os.replace(tmp, dest)
            if min_bytes and os.path.getsize(dest) < min_bytes:
                return False, f"文件过小（{os.path.getsize(dest)} < {min_bytes}）"
            echo(f"    完成：{_mb(dest):.1f}MB")
            return True, "downloaded"
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError) as exc:
            last_err = f"{type(exc).__name__}: {exc}"
            echo(f"    第 {attempt} 次下载失败：{last_err}")
            for leftover in (dest + ".part",):
                if os.path.isfile(leftover):
                    try:
                        os.remove(leftover)
                    except OSError:
                        pass
            if attempt <= retries:
                time.sleep(2.0 * attempt)
    return False, last_err


def extract_zip(zip_path: str, dest_dir: str, *, echo: Callable[[str], None] = _echo) -> bool:
    """解压到 dest_dir（扁平化：若压缩包只有单一顶层目录则剥掉）。"""
    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
            tops = {n.split("/", 1)[0] for n in names if "/" in n}
            strip = bool(tops) and len(tops) == 1 and not any(
                n.count("/") == 0 for n in names
            )
            prefix = (list(tops)[0] + "/") if strip else ""
            os.makedirs(dest_dir, exist_ok=True)
            for member in zf.namelist():
                if member.endswith("/"):
                    continue
                rel = member[len(prefix):] if prefix and member.startswith(prefix) else member
                if not rel:
                    continue
                target = os.path.join(dest_dir, rel)
                os.makedirs(os.path.dirname(target) or dest_dir, exist_ok=True)
                with zf.open(member) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
            echo(f"    解压完成：{len(names)} 个文件")
            return True
    except (zipfile.BadZipFile, OSError) as exc:
        echo(f"    解压失败：{exc}")
        return False


# ==========================================================================
# 1. Ollama 路径
# ==========================================================================
class OllamaStatus:
    """Ollama 环境盘点结果。"""

    def __init__(
        self,
        *,
        cli_path: str | None = None,
        server_up: bool = False,
        version: str = "",
        models: list[str] | None = None,
        host: str = DEFAULT_HOST,
    ) -> None:
        self.cli_path = cli_path
        self.server_up = server_up
        self.version = version
        self.models = list(models or [])
        self.host = host

    @property
    def cli_installed(self) -> bool:
        return bool(self.cli_path)

    def has_model(self, model: str) -> bool:
        want = (model or "").strip()
        if not want:
            return False
        for m in self.models:
            if m == want:
                return True
            if ":" not in want and m.split(":", 1)[0] == want:
                return True
        return False

    @property
    def ready(self) -> bool:
        return self.server_up and bool(self.models)

    def to_dict(self) -> dict[str, Any]:
        return {
            "cli_installed": self.cli_installed,
            "cli_path": self.cli_path,
            "server_up": self.server_up,
            "version": self.version,
            "models": list(self.models),
            "host": self.host,
            "ready": self.ready,
        }

    def describe(self) -> str:
        parts = [
            f"命令行：{'已装 → ' + str(self.cli_path) if self.cli_installed else '未找到'}",
            f"服务 {self.host}：{'在线' if self.server_up else '未响应'}",
        ]
        if self.version:
            parts.append(f"版本 {self.version}")
        parts.append(
            f"模型 {len(self.models)} 个" + (f"（{', '.join(self.models)}）" if self.models else "")
        )
        return "；".join(parts)


def _find_ollama_cli() -> str | None:
    found = shutil.which("ollama")
    if found:
        return found
    cands: list[str] = []
    if _IS_WIN:
        cands.append(os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Ollama", "ollama.exe"))
        cands.append(os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Ollama", "ollama.exe"))
    else:
        cands += ["/usr/local/bin/ollama", "/usr/bin/ollama", "/opt/homebrew/bin/ollama"]
    for p in cands:
        if p and os.path.isfile(p):
            return p
    return None


def _http_json(url: str, *, timeout: float = 3.0) -> Any:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - 本机
        return json.loads(resp.read().decode("utf-8", "replace"))


def _server_models(host: str, *, timeout: float = 3.0) -> tuple[bool, list[str]]:
    try:
        data = _http_json(f"{host.rstrip('/')}/api/tags", timeout=timeout)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError):
        return False, []
    names: list[str] = []
    if isinstance(data, dict):
        for item in data.get("models") or []:
            if isinstance(item, dict) and item.get("name"):
                names.append(str(item["name"]))
    return True, sorted(names)


def _cli_version(cli: str) -> str:
    try:
        out = subprocess.run([cli, "--version"], capture_output=True, text=True, timeout=20, check=False)
        text = (out.stdout or out.stderr or "").strip()
        return text.splitlines()[0] if text else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def detect(*, host: str | None = None, timeout: float = 3.0) -> OllamaStatus:
    """盘点 Ollama 就绪度（不改变状态）。"""
    host = host or os.environ.get("EW_OLLAMA_HOST") or DEFAULT_HOST
    cli = _find_ollama_cli()
    up, models = _server_models(host, timeout=timeout)
    return OllamaStatus(
        cli_path=cli, server_up=up, version=_cli_version(cli) if cli else "", models=models, host=host
    )


def _run(cmd: list[str], *, timeout: float | None = None) -> tuple[int, str]:
    log.debug("执行：%s", " ".join(cmd))
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError:
        return 127, f"命令不存在：{cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, f"命令超时：{' '.join(cmd)}"
    except OSError as exc:
        return 1, str(exc)
    return proc.returncode, ((proc.stdout or "") + (proc.stderr or "")).strip()


def install_ollama(*, echo: Callable[[str], None] = _echo) -> tuple[bool, str]:
    """安装 Ollama。Windows 走 winget→官方包直连 双通道。"""
    existing = _find_ollama_cli()
    if existing:
        return True, f"已安装：{existing}"

    if _IS_WIN:
        winget = shutil.which("winget")
        if winget:
            echo("    尝试 winget（最多 10 分钟）…")
            code, out = _run(
                [winget, "install", "--id", "Ollama.Ollama", "-e",
                 "--accept-source-agreements", "--accept-package-agreements", "--silent"],
                timeout=600,
            )
            if code == 0 and _find_ollama_cli():
                return True, out[-400:] or "winget 安装完成"
            echo("    winget 未成功，改直连官方安装包…")
        import tempfile

        dst = os.path.join(tempfile.gettempdir(), "OllamaSetup.exe")
        ok, msg = download("https://ollama.com/download/OllamaSetup.exe", dst, echo=echo,
                           min_bytes=100 * 1024 * 1024)
        if not ok:
            return False, f"下载安装包失败：{msg}"
        echo("    静默安装中…")
        code, out = _run([dst, "/VERYSILENT", "/NORESTART", "/SUPPRESSMSGBOXES"], timeout=1800)
        cli = _find_ollama_cli()
        return (cli is not None), (f"安装完成：{cli}" if cli else f"安装程序退出码 {code}，未找到 ollama.exe")

    if _IS_MAC:
        brew = shutil.which("brew")
        if not brew:
            return False, "未找到 Homebrew，请手动安装：https://ollama.com/download/mac"
        code, out = _run([brew, "install", "ollama"], timeout=1800)
        return (code == 0 and _find_ollama_cli() is not None), out[-800:]

    curl, sh = shutil.which("curl"), shutil.which("sh")
    if not curl or not sh:
        return False, "未找到 curl/sh，请手动安装：https://ollama.com/download/linux"
    code, out = _run([sh, "-c", "curl -fsSL https://ollama.com/install.sh | sh"], timeout=1800)
    return (code == 0 and _find_ollama_cli() is not None), out[-800:]


def _ensure_ollama_server(*, host: str, echo: Callable[[str], None], wait: float = 60.0) -> bool:
    up, _ = _server_models(host)
    if up:
        return True
    cli = _find_ollama_cli()
    if not cli:
        return False
    echo("    服务未运行，后台拉起 ollama serve…")
    kw: dict[str, Any] = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                          "stdin": subprocess.DEVNULL}
    if _IS_WIN:
        kw["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
    else:
        kw["start_new_session"] = True
    try:
        subprocess.Popen([cli, "serve"], **kw)  # noqa: S603
    except OSError as exc:
        log.warning("启动 ollama serve 失败：%s", exc)
        return False
    deadline = time.time() + wait
    while time.time() < deadline:
        time.sleep(1.5)
        if _server_models(host)[0]:
            return True
    return False


def pull_model(
    model: str = DEFAULT_MODEL,
    *,
    host: str | None = None,
    echo: Callable[[str], None] = _echo,
    timeout: float = 3600.0,
) -> tuple[bool, str]:
    """拉取 Ollama 模型（有 CLI 用 CLI，否则走 HTTP）。"""
    host = host or os.environ.get("EW_OLLAMA_HOST") or DEFAULT_HOST
    cli = _find_ollama_cli()
    if cli:
        echo(f"    拉取 {model}…")
        code, out = _run([cli, "pull", model], timeout=timeout)
        return (code == 0), (out[-400:] if code != 0 else "拉取完成")
    payload = json.dumps({"name": model, "stream": False}).encode("utf-8")
    req = urllib.request.Request(
        f"{host.rstrip('/')}/api/pull", data=payload,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - 本机
            return True, resp.read().decode("utf-8", "replace")[-400:]
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
        return False, f"HTTP 拉取失败：{exc}"


# ==========================================================================
# 2. llama.cpp 路径（Ollama 下载不动时的等效方案）
# ==========================================================================
def vendor_dir() -> str:
    """<repo>/vendor —— 二进制与模型都放这里，全部离线可删。"""
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.abspath(os.path.join(here, os.pardir, _meta.VENDOR_DIR))


def llama_status() -> dict[str, Any]:
    """盘点 llama.cpp 就绪度。"""
    from .llamacpp import find_gguf, find_llama_server

    srv = find_llama_server()
    gguf = find_gguf(_meta.GGUF_FILE)
    gguf_mb = _mb(gguf) if gguf else 0.0
    return {
        "engine": "llamacpp",
        "server_path": srv,
        "server_installed": bool(srv),
        "model_path": gguf,
        "model_mb": round(gguf_mb, 1),
        "model_installed": bool(gguf),
        "ready": bool(srv and gguf),
    }


def install_llama_binary(
    *,
    asset_key: str | None = None,
    echo: Callable[[str], None] = _echo,
) -> tuple[bool, str]:
    """下载并解压 llama.cpp 预编译二进制到 vendor/llama/。"""
    from .llamacpp import find_llama_server

    existing = find_llama_server()
    if existing:
        return True, f"已安装：{existing}"

    key = (asset_key or _meta.recommended_asset()).lower()
    if key not in _meta.LLAMA_ASSETS:
        key = "cpu"
    asset = _meta.LLAMA_ASSETS[key]
    echo(f"    加速后端：{key}（{asset}）")

    vendor = vendor_dir()
    zip_path = os.path.join(vendor, asset)
    urls = [
        f"https://github.com/ggml-org/llama.cpp/releases/download/{_meta.LLAMA_TAG}/{asset}",
        f"https://ghfast.top/https://github.com/ggml-org/llama.cpp/releases/download/{_meta.LLAMA_TAG}/{asset}",
    ]
    ok = False
    last = ""
    for url in urls:
        echo(f"    下载 {url.split('/')[2]} …")
        ok, last = download(url, zip_path, echo=echo, min_bytes=_meta.LLAMA_MIN_BYTES, retries=1)
        if ok:
            break
    if not ok:
        return False, (
            f"所有下载源均失败：{last}\n"
            "    如为网络限制，可手动下载后放到 vendor/ 目录再重跑：\n"
            f"      {urls[0]}"
        )

    # 目录按加速后端区分，便于同时保留多份（find_llama_server 优先 GPU 版）
    sub = {"vulkan": "llama-vulkan", "cuda12": "llama-cuda", "cpu": "llama"}.get(key, "llama")
    dest = os.path.join(vendor, sub)
    if not extract_zip(zip_path, dest, echo=echo):
        return False, "解压失败"
    try:
        os.remove(zip_path)
    except OSError:
        pass
    srv = find_llama_server()
    return (srv is not None), (f"安装完成：{srv}" if srv else "解压后未找到 llama-server.exe")


def install_gguf(*, echo: Callable[[str], None] = _echo) -> tuple[bool, str]:
    """下载 Qwen2.5-3B-Instruct Q4_K_M GGUF 到 vendor/models/。"""
    from .llamacpp import find_gguf

    existing = find_gguf(_meta.GGUF_FILE)
    if existing and _mb(existing) >= _meta.GGUF_MIN_BYTES / (1024 * 1024):
        return True, f"已存在：{existing}（{_mb(existing):.0f}MB）"

    dest = os.path.join(vendor_dir(), "models", _meta.GGUF_FILE)
    echo(f"    目标：{dest}")
    ok = False
    last = ""
    for url in _meta.GGUF_MIRRORS:
        host = url.split("/")[2]
        echo(f"    下载源 {host} …")
        ok, last = download(url, dest, echo=echo, min_bytes=_meta.GGUF_MIN_BYTES, retries=1)
        if ok:
            break
        echo(f"    {host} 不可用，换下一个源")
    if not ok:
        return False, (
            f"模型下载失败：{last}\n"
            "    可手动下载后放入 vendor/models/：\n"
            f"      {_meta.GGUF_URL}"
        )
    return True, f"模型就绪：{dest}"


def ensure_llamacpp_ready(
    *,
    asset_key: str | None = None,
    install: bool = True,
    pull: bool = True,
    echo: Callable[[str], None] = _echo,
) -> tuple[bool, dict[str, Any], list[str]]:
    """把 llama.cpp 路径推到可用状态。"""
    steps: list[str] = []

    def say(msg: str) -> None:
        steps.append(msg)
        echo(msg)

    # 1) 二进制
    st = llama_status()
    say(f"[1/4] 检测：llama-server={'有' if st['server_installed'] else '无'}，"
        f"模型={'有' if st['model_installed'] else '无'}")
    if st["server_installed"]:
        say("[1/4] llama.cpp 二进制已就绪，跳过。")
    elif not install:
        say("[1/4] 缺少 llama.cpp 二进制，且指定 --no-install，跳过。")
        return False, st, steps
    else:
        ok, msg = install_llama_binary(asset_key=asset_key, echo=echo)
        say(f"[1/4] 安装 llama.cpp：{'成功' if ok else '失败'} —— {msg}")
        if not ok:
            return False, llama_status(), steps

    # 2) 模型
    st = llama_status()
    if st["model_installed"]:
        say(f"[2/4] 模型已就绪（{st['model_mb']:.0f}MB），跳过。")
    elif not pull:
        say("[2/4] 缺少模型，且指定 --no-pull，跳过。")
        return False, st, steps
    else:
        ok, msg = install_gguf(echo=echo)
        say(f"[2/4] 下载模型：{'成功' if ok else '失败'} —— {msg}")
        if not ok:
            return False, llama_status(), steps

    # 3) 启动服务
    say("[3/4] 启动 llama-server …")
    ok_start, detail = _llamacpp_self_check(echo=echo)
    say(f"[3/4] 启动：{'成功' if ok_start else '失败'} —— {detail}")
    if not ok_start:
        return False, llama_status(), steps

    # 4) 推理自检
    say("[4/4] 完成。")
    return True, llama_status(), steps


def _llamacpp_self_check(*, echo: Callable[[str], None] = _echo) -> tuple[bool, str]:
    """启动 llama-server 并发一次最小请求。"""
    from .llamacpp import LLamaCppBackend

    backend = LLamaCppBackend(autostart=True)
    try:
        if not backend.start():
            return False, "llama-server 未能就绪（可能是显存不足或 Vulkan 驱动缺失，可试 --engine llamacpp --gpu cpu）"
        text = backend.generate("用一个词回答：你好", max_tokens=16)
        if not text:
            return False, "模型返回空内容"
        return True, f"返回 {text[:40]!r}"
    except RuntimeError as exc:
        return False, str(exc)
    finally:
        backend.close()


# ==========================================================================
# 3. 统一入口
# ==========================================================================
def pick_engine(engine: str = DEFAULT_ENGINE) -> str:
    """解析 --engine。auto 时：已有 llama.cpp 就用它，否则按"能装成"排序。"""
    engine = (engine or DEFAULT_ENGINE).lower()
    if engine in ("ollama", "llamacpp"):
        return engine
    st = llama_status()
    if st["ready"]:
        return "llamacpp"
    ollama = detect()
    if ollama.cli_installed and ollama.models:
        return "ollama"
    # 都没齐：优先 llamacpp（体积小得多，成功率更高）
    return "llamacpp"


def environment_report(engine: str = DEFAULT_ENGINE) -> dict[str, Any]:
    """当前环境的完整盘点（供 --check 与 CLI 展示）。"""
    o = detect()
    return {
        "engine": pick_engine(engine),
        "ollama": o.to_dict(),
        "llamacpp": llama_status(),
    }


def run_install(
    *,
    engine: str = DEFAULT_ENGINE,
    model: str | None = None,
    host: str | None = None,
    check_only: bool = False,
    install: bool = True,
    pull: bool = True,
    asset_key: str | None = None,
) -> int:
    """``enderwriter install`` 的实现。返回进程退出码。"""
    chosen = pick_engine(engine)
    echo = _echo

    echo("══ EnderWriter 本地模型环境 ══")
    echo(f"  引擎：{chosen}" + ("（自动选择）" if (engine or DEFAULT_ENGINE) == "auto" else ""))
    if chosen == "ollama":
        echo(f"  模型：{model or DEFAULT_MODEL}")
        echo(f"  地址：{host or os.environ.get('EW_OLLAMA_HOST') or DEFAULT_HOST}")
    else:
        from .llamacpp import DEFAULT_PORT

        echo(f"  模型：{model or _meta.GGUF_FILE}")
        echo(f"  地址：http://127.0.0.1:{DEFAULT_PORT}（llama-server）")
    echo("")

    if check_only:
        rep = environment_report(engine)
        echo("  检测结果：")
        echo(f"    Ollama    : {detect().describe()}")
        lst = rep["llamacpp"]
        echo(f"    llama.cpp : 二进制={'有' if lst['server_installed'] else '无'}"
             f"（{lst['server_path'] or '-'}）；模型={'有' if lst['model_installed'] else '无'}"
             f"（{lst['model_mb']:.0f}MB）")
        ready = rep["ollama"].get("ready") or lst["ready"]
        echo("")
        echo("  " + ("✅ 环境就绪，可直接 `$env:EW_LLM='<引擎>'` 跑 demo"
                      if ready else "⚠ 环境未就绪，运行 `enderwriter install` 自动安装"))
        return 0 if ready else 1

    if chosen == "ollama":
        status = detect(host=host)
        echo(f"[1/4] 检测：{status.describe()}")
        if not status.cli_installed:
            if not install:
                echo("[1/4] 未安装 Ollama，且指定 --no-install，中止。")
                return 1
            ok, msg = install_ollama(echo=echo)
            echo(f"[1/4] 安装 Ollama：{'成功' if ok else '失败'} —— {msg}")
            if not ok:
                return 1
        else:
            echo("[1/4] Ollama 已安装，跳过。")

        if not _ensure_ollama_server(host=host or os.environ.get("EW_OLLAMA_HOST") or DEFAULT_HOST, echo=echo):
            echo("[2/4] 服务无法启动。")
            return 1
        echo("[2/4] 服务在线。")

        target = model or DEFAULT_MODEL
        status = detect(host=host)
        if status.has_model(target):
            echo(f"[3/4] 模型 {target} 已就绪，跳过。")
        elif not pull:
            echo(f"[3/4] 缺少 {target}，且指定 --no-pull，中止。")
            return 1
        else:
            ok, msg = pull_model(target, host=host, echo=echo)
            echo(f"[3/4] 拉取 {target}：{'成功' if ok else '失败'} —— {msg}")
            if not ok:
                echo("\n  提示：若下载过慢，这套网络环境可以改走 llama.cpp：")
                echo("    enderwriter install --engine llamacpp")
                return 1

        ok_check, detail = _ollama_self_check(target, host=host or DEFAULT_HOST)
        echo(f"[4/4] 推理自检：{'通过' if ok_check else '失败'} —— {detail}")
        final = ("✅ Ollama 就绪！\n      $env:EW_LLM='ollama'\n      python -m enderwriter demo"
                 if ok_check else "❌ 自检未通过，请看上面原因。")
        echo("\n  " + ("═" * 46) + "\n  " + final)
        return 0 if ok_check else 1

    ok, st, _steps = ensure_llamacpp_ready(
        asset_key=asset_key, install=install, pull=pull, echo=echo
    )
    echo("\n  " + ("═" * 46))
    if ok:
        echo("  ✅ llama.cpp + Qwen2.5-3B 就绪！接下来：")
        echo("       $env:EW_LLM='llamacpp'")
        echo("       python -m enderwriter demo")
        return 0
    echo("  ❌ 环境未完全就绪，请看上面的失败原因。")
    if not st.get("model_installed"):
        echo("     若模型下载失败，可用浏览器手动下载：")
        echo(f"       {_meta.GGUF_URL}")
        echo(f"     放到 {os.path.join(vendor_dir(), 'models')} 下，再重跑本命令。")
    return 1


def _ollama_self_check(model: str, *, host: str) -> tuple[bool, str]:
    payload = json.dumps(
        {"model": model, "prompt": "用一个词回答：你好", "stream": False,
         "options": {"num_predict": 12}}
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{host.rstrip('/')}/api/generate", data=payload,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:  # noqa: S310 - 本机
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    text = str((data or {}).get("response") or "").strip()
    return (bool(text), f"返回 {text[:40]!r}" if text else "模型返回空内容")


def ensure_ready(
    *,
    model: str | None = None,
    host: str | None = None,
    engine: str = DEFAULT_ENGINE,
    install: bool = True,
    pull: bool = True,
    echo: Callable[[str], None] = _echo,
) -> tuple[bool, dict[str, Any], list[str]]:
    """把环境推到"可用"，返回 (成功, 最终盘点, 过程步骤)。"""
    chosen = pick_engine(engine)
    if chosen == "ollama":
        steps: list[str] = []

        def say(msg: str) -> None:
            steps.append(msg)
            echo(msg)

        st = detect(host=host)
        say(f"[1/4] {st.describe()}")
        if not st.cli_installed:
            if not install:
                return False, st.to_dict(), steps
            ok, msg = install_ollama(echo=echo)
            say(f"[1/4] 安装 Ollama：{'成功' if ok else '失败'} —— {msg}")
            if not ok:
                return False, detect(host=host).to_dict(), steps
        target = model or DEFAULT_MODEL
        if not _ensure_ollama_server(host=host or DEFAULT_HOST, echo=echo):
            say("[2/4] 服务无法启动")
            return False, detect(host=host).to_dict(), steps
        say("[2/4] 服务在线")
        st = detect(host=host)
        if not st.has_model(target):
            if not pull:
                say(f"[3/4] 缺少 {target}，跳过")
                return False, st.to_dict(), steps
            ok, msg = pull_model(target, host=host, echo=echo)
            say(f"[3/4] 拉取模型：{'成功' if ok else '失败'} —— {msg}")
            if not ok:
                return False, detect(host=host).to_dict(), steps
        ok_check, detail = _ollama_self_check(target, host=host or DEFAULT_HOST)
        say(f"[4/4] 自检：{'通过' if ok_check else '失败'} —— {detail}")
        return ok_check, detect(host=host).to_dict(), steps

    ok, st, steps = ensure_llamacpp_ready(install=install, pull=pull, echo=echo)
    return ok, st, steps


if __name__ == "__main__":  # pragma: no cover
    sys.exit(run_install())
