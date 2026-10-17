"""llama.cpp / GGUF 安装目标常量。

单独拆出来是为了让 :mod:`enderwriter.install` 与 :mod:`enderwriter.llamacpp`
都能引用同一份元数据而不产生循环导入。
"""

from __future__ import annotations

__all__ = [
    "GGUF_FILE",
    "GGUF_URL",
    "GGUF_MIN_BYTES",
    "GGUF_MIRRORS",
    "LLAMA_TAG",
    "LLAMA_ASSETS",
    "LLAMA_MIN_BYTES",
    "VENDOR_DIR",
    "recommended_asset",
]

#: 目标模型文件名（Qwen2.5-3B-Instruct，Q4_K_M 量化，约 1.9GB）
GGUF_FILE: str = "qwen2.5-3b-instruct-q4_k_m.gguf"
#: 主下载源：ModelScope（国内实测 12MB/s）
GGUF_URL: str = (
    "https://modelscope.cn/models/Qwen/Qwen2.5-3B-Instruct-GGUF/resolve/master/"
    + GGUF_FILE
)
#: 备选源
GGUF_MIRRORS: tuple[str, ...] = (
    GGUF_URL,
    "https://huggingface.co/Qwen/Qwen2.5-3B-Instruct-GGUF/resolve/main/" + GGUF_FILE,
)
#: 完整性下限（低于此值视为下载失败）
GGUF_MIN_BYTES: int = 1_500_000_000

#: llama.cpp 预编译版本（nightly tag，带 Windows 各加速后端）
LLAMA_TAG: str = "b11707"
#: 各加速后端对应的资产名，按优先级排列（先真实能跑的，再兜底）
LLAMA_ASSETS: dict[str, str] = {
    "vulkan": f"llama-{LLAMA_TAG}-bin-win-vulkan-x64.zip",
    "cuda12": f"llama-{LLAMA_TAG}-bin-win-cuda-12.4-x64.zip",
    "cpu": f"llama-{LLAMA_TAG}-bin-win-cpu-x64.zip",
}
#: 二进制压缩包最低大小
LLAMA_MIN_BYTES: int = 5_000_000

#: vendor 目录（二进制与模型都放这）
VENDOR_DIR: str = "vendor"


def recommended_asset() -> str:
    """按本机环境挑一个最合适的 llama.cpp 资产。

    * 检测到 NVIDIA 显卡 → cuda12
    * 检测到 AMD/Intel 显卡 → vulkan（AMD 走 Vulkan 是官方推荐做法）
    * 都没有 → cpu
    """
    import os
    import platform

    if platform.system() != "Windows":
        return "cpu"
    gpu = os.environ.get("EW_GPU", "").lower()
    if gpu in LLAMA_ASSETS:
        return gpu
    try:
        import subprocess

        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-CimInstance Win32_VideoController).Name -join ';'"],
            capture_output=True, text=True, timeout=20, check=False,
        ).stdout.lower()
    except (OSError, subprocess.SubprocessError):
        out = ""
    if "nvidia" in out:
        return "cuda12"
    if "amd" in out or "radeon" in out or "intel" in out:
        return "vulkan"
    return "cpu"
