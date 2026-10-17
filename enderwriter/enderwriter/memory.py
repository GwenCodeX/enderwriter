"""分层记忆：按作品分库 + 全局文学库。

规范里的末影箱是单一长期记忆；实际写作中两类信息天然不同：

  * **作品细节**（人物 / 世界 / 时间线 / 伏笔 / 物品）
    —— 属于某一部作品，跟着作品走，跨章节跨对话共享，换作品就该换库。
  * **文学经验**（风格 / 手法 / 节奏 / 教训）
    —— 属于作者本人，跨所有作品通用，写在哪本书都成立。

所以存储分两层：

    <home>/works/<作品id>.db     作品库：一个作品一个文件
    <home>/global.db             全局库：风格与文学经验，所有作品共享

检索时两层都查，但**作品库优先**（当前作品的设定比通用经验更该进上下文）。

写入时按内容自动路由（:func:`route`）：
  显式指定 scope 就用指定的；否则看 layer —— style 进全局，其余进作品。
"""

from __future__ import annotations

import hashlib
import os
import re
from typing import Any

from .common import LAYERS, get_logger

__all__ = [
    "SCOPE_WORK",
    "SCOPE_GLOBAL",
    "SESSION_PREFIX",
    "GLOBAL_LAYERS",
    "WorkMemory",
    "default_home",
    "list_works",
    "resolve_work",
    "route",
    "session_work",
    "slugify",
]

log = get_logger("memory")

SCOPE_WORK = "work"
SCOPE_GLOBAL = "global"

#: 这些层是"文学经验"，默认进全局库
GLOBAL_LAYERS: frozenset[str] = frozenset({"style"})
#: 这些层是"作品细节"，默认进作品库
WORK_LAYERS: frozenset[str] = frozenset(set(LAYERS) - GLOBAL_LAYERS)

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def default_home() -> str:
    """记忆根目录。EW_HOME 优先，否则放在包目录的兄弟位置。"""
    env = os.environ.get("EW_HOME")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    here = os.path.dirname(os.path.abspath(__file__))
    # <repo>/enderwriter/memory  —— 与 package 同级，不污染源码目录
    return os.path.join(os.path.dirname(here), "memory")


def slugify(name: str, *, max_len: int = 40) -> str:
    """把作品名转成安全的文件名片段。

    中文保留（现代文件系统都支持），只替换路径分隔符与控制字符；
    全中文名再加一段短哈希，避免不同作品撞名。
    """
    raw = (name or "").strip()
    if not raw:
        return "untitled"
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", raw)
    cleaned = re.sub(r"\s+", "_", cleaned).strip("._") or "untitled"
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len]
    if _CJK_RE.search(cleaned):
        digest = hashlib.blake2b(raw.encode("utf-8"), digest_size=3).hexdigest()
        return f"{cleaned}-{digest}"
    return cleaned


def route(layer: str, *, scope: str | None = None) -> str:
    """决定一条记录该进哪个库。

    ``scope`` 显式给出时直接采用（用户/工具说了算）；
    否则按层路由：style → 全局，其余 → 作品。
    """
    if scope in (SCOPE_WORK, SCOPE_GLOBAL):
        return scope
    if layer == "style":
        return SCOPE_GLOBAL
    return SCOPE_WORK


#: 会话级作品的命名前缀：每个对话默认一套独立记忆
SESSION_PREFIX = "对话-"


def session_work(session_id: str) -> str:
    """把会话 ID 变成该对话专属的作品名。

    默认情况下一个对话就是一套独立记忆，跨对话互不可见；
    用户想接入某个已存在的作品时，显式传 work 即可（见 :func:`resolve_work`）。
    """
    sid = (session_id or "").strip()
    if not sid:
        return ""
    # 取会话 ID 的稳定短标识，避免文件名过长
    short = re.sub(r"[^0-9a-zA-Z]", "", sid)[-8:] or "default"
    return f"{SESSION_PREFIX}{short}"


def resolve_work(*, work: str | None = None, session_id: str | None = None) -> str:
    """解析"这次该用哪个作品库"。

    优先级：
      1. 显式给定的 ``work``（用户说"用作品《示例卷》"）
      2. 当前会话专属作品（默认行为：按对话隔离）
      3. 空字符串（无会话时只用全局库）
    """
    explicit = (work or "").strip()
    if explicit:
        return explicit
    return session_work(session_id or "")


class WorkMemory:
    """一个作品库 + 全局库的组合视图。

    用法::

        mem = WorkMemory("斗破苍穹")
        with mem.work_chest() as chest:      # 作品库
            chest.upsert("character", "萧炎", "主角")
        with mem.global_chest() as chest:    # 全局库
            chest.upsert("style", "节奏", "前三章慢，第四章提速")
    """

    def __init__(self, work: str | None = None, *, home: str | None = None) -> None:
        self.home = os.path.abspath(home or default_home())
        name = (work or "").strip()

        # 兼容传入"库 ID"（如 示例小说-5b512f）。
        # 面板的作品列表同时给出 name 与 id，调用方可能传 id。
        # 若直接 slugify(id) 会再追加一次哈希（示例小说-5b512f-97cc05），
        # 拼出一个不存在的文件，导致所有操作静默失败。
        raw_id = ""
        if name:
            if os.path.isfile(os.path.join(self.home, "works", f"{name}.db")):
                raw_id = name
            else:
                for item in list_works(self.home):
                    if item.get("id") == name and item.get("name"):
                        name = item["name"]
                        raw_id = item["id"]
                        break

        self.work_name = name
        self.work_id = raw_id or (slugify(self.work_name) if self.work_name else "")

    # -- 路径 ----------------------------------------------------------
    @property
    def work_path(self) -> str:
        return os.path.join(self.home, "works", f"{self.work_id}.db")

    @property
    def global_path(self) -> str:
        return os.path.join(self.home, "global.db")

    def path_for(self, scope: str) -> str:
        return self.global_path if scope == SCOPE_GLOBAL else self.work_path

    def exists(self, scope: str) -> bool:
        return bool(self.path_for(scope)) and os.path.isfile(self.path_for(scope))

    # -- 打开 ----------------------------------------------------------
    def open(self, scope: str = SCOPE_WORK):
        """打开指定层的末影箱（惰性建目录）。

        作品库里会写入一行 meta('work_name')，记录**真实作品名**，
        这样列作品时不必从 slug 化过的文件名反推。
        """
        from .chest import EnderChest

        path = self.path_for(scope)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        chest = EnderChest(path)
        if scope == SCOPE_WORK and self.work_name:
            try:
                chest.conn.execute(
                    "INSERT INTO meta(key, value) VALUES('work_name', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (self.work_name,),
                )
                chest.conn.commit()
            except Exception as exc:  # noqa: BLE001 - 元信息写失败不该影响使用
                log.warning("写入 work_name 失败：%s", exc)
        return chest

    def work_chest(self):
        return self.open(SCOPE_WORK)

    def global_chest(self):
        return self.open(SCOPE_GLOBAL)

    # -- 概况 ----------------------------------------------------------
    def describe(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "home": self.home,
            "work": {
                "name": self.work_name,
                "id": self.work_id,
                "path": self.work_path if self.work_id else "",
                "exists": self.exists(SCOPE_WORK) if self.work_id else False,
            },
            "global": {
                "path": self.global_path,
                "exists": self.exists(SCOPE_GLOBAL),
            },
        }
        for scope in (SCOPE_WORK, SCOPE_GLOBAL):
            if not out[scope]["exists"] and scope == SCOPE_WORK:
                continue
            try:
                chest = self.open(scope)
                try:
                    out[scope]["stats"] = dict(chest.stats())
                finally:
                    chest.close()
            except Exception as exc:  # noqa: BLE001 - 概况接口不能因单库损坏而整体失败
                out[scope]["error"] = str(exc)
        return out


def list_works(home: str | None = None) -> list[dict[str, Any]]:
    """列出所有作品库。

    作品名从库内 meta 表读（``work_name``），而不是从文件名反推——
    文件名经过了 slugify，带哈希后缀，反推会失真。
    """
    base = os.path.join(os.path.abspath(home or default_home()), "works")
    if not os.path.isdir(base):
        return []
    out: list[dict[str, Any]] = []
    for name in sorted(os.listdir(base)):
        if not name.endswith(".db"):
            continue
        path = os.path.join(base, name)
        stamp = os.path.getmtime(path)
        item: dict[str, Any] = {
            "id": name[: -len(".db")],
            "path": path,
            "size_kb": round(os.path.getsize(path) / 1024, 1),
            "updated_at": stamp,
        }

        # 从库内读回真实作品名与统计
        try:
            import sqlite3

            conn = sqlite3.connect(path)
            try:
                row = conn.execute(
                    "SELECT value FROM meta WHERE key = 'work_name'"
                ).fetchone()
                if row and row[0]:
                    item["name"] = str(row[0])
                else:
                    # 老库没有 work_name：从 slug 里剥掉哈希后缀还原。
                    # slugify 对含中文的名字会追加 "-xxxxxx"，剥掉即可。
                    stem = item["id"]
                    m = re.match(r"^(?P<base>.+)-(?P<hex>[0-9a-f]{6})$", stem)
                    item["name"] = m.group("base") if m else stem
                counts = dict(
                    conn.execute(
                        "SELECT layer, COUNT(*) FROM records GROUP BY layer"
                    ).fetchall()
                )
                item["records"] = counts
                item["total"] = sum(int(v or 0) for v in counts.values())
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001 - 单个库损坏不影响列表
            log.warning("读取作品库 %s 失败：%s", name, exc)

        item.setdefault("name", item["id"])
        out.append(item)
    return out
