"""末影箱（Ender Chest）：SQLite 分格存储 + 三重索引 + 版本历史。

设计要点（严格对应规范第 2/3 节）：
  * 七个格子：人物格/世界格/时间格/伏笔格/物品格/风格格/称呼格；
  * 每条记录都带 ``version`` 与 ``last_updated``，状态变更**只追加历史，绝不覆盖**；
  * 三重索引：关键词倒排（terms）、向量（records.embedding + numpy cosine）、图（edges）；
    另有状态索引（state），用于“还活着吗 / 在哪 / 什么境界”这类硬事实；
  * 称呼格负责 别名 -> 标准名 归一化。

本模块是各并行组件的唯一交汇点，公开签名一经冻结即不得擅自更改。
"""

from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Sequence

import numpy as np

from .. import embed as _embed
from ..common import LAYERS, chapter_index, dumps, get_logger, loads, make_id, normalize_name
from ..lexicon import analyze, extract_entities, term_weights

__all__ = ["EnderChest", "Record", "Hit", "Foreshadow", "SCHEMA_VERSION"]

log = get_logger("chest")

SCHEMA_VERSION = 1

_DDL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS records (
    layer        TEXT NOT NULL,
    entity       TEXT NOT NULL,
    content      TEXT NOT NULL,
    data         TEXT NOT NULL DEFAULT '{}',
    version      INTEGER NOT NULL DEFAULT 1,
    last_updated TEXT NOT NULL,
    chapter_id   TEXT,
    embedding    BLOB,
    PRIMARY KEY (layer, entity)
);
CREATE TABLE IF NOT EXISTS history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    layer        TEXT NOT NULL,
    entity       TEXT NOT NULL,
    version      INTEGER NOT NULL,
    content      TEXT NOT NULL,
    data         TEXT NOT NULL DEFAULT '{}',
    chapter_id   TEXT,
    updated_at   TEXT NOT NULL,
    change       TEXT NOT NULL DEFAULT 'update'
);
CREATE INDEX IF NOT EXISTS idx_history ON history(layer, entity, version);
CREATE TABLE IF NOT EXISTS terms (
    term    TEXT NOT NULL,
    layer   TEXT NOT NULL,
    entity  TEXT NOT NULL,
    weight  REAL NOT NULL DEFAULT 1.0,
    PRIMARY KEY (term, layer, entity)
);
CREATE INDEX IF NOT EXISTS idx_terms_term ON terms(term);
CREATE TABLE IF NOT EXISTS aliases (
    alias     TEXT PRIMARY KEY,
    canonical TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_aliases_canon ON aliases(canonical);
CREATE TABLE IF NOT EXISTS edges (
    src        TEXT NOT NULL,
    rel        TEXT NOT NULL,
    dst        TEXT NOT NULL,
    chapter_id TEXT,
    data       TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (src, rel, dst)
);
CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst);
CREATE TABLE IF NOT EXISTS state (
    entity     TEXT NOT NULL,
    key        TEXT NOT NULL,
    value      TEXT NOT NULL,
    chapter_id TEXT,
    version    INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (entity, key)
);
CREATE TABLE IF NOT EXISTS chapters (
    chapter_id TEXT PRIMARY KEY,
    title      TEXT NOT NULL DEFAULT '',
    text       TEXT NOT NULL DEFAULT '',
    summary    TEXT NOT NULL DEFAULT '',
    word_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
-- 使用记录：一条记忆被检索到几次、最近一次是什么时候、是否已回收归档。
--
-- 单独建表而不是塞进 records.data，原因有两个：
--   1. data 参与版本管理，每次命中都 bump version 会把 history 撑爆
--   2. 使用记录是"关于记忆的元数据"，与内容本身的生命周期不同
--
-- 重要：这张表**只增不删**。末影箱在文学场景下兼当设定集，
-- 任何一条设定都可能在几百章后被再次引用，删掉等于永久丢失。
-- 所以这里只有"未使用 / 已使用"，没有"过期删除"。
CREATE TABLE IF NOT EXISTS usage (
    layer       TEXT NOT NULL,
    entity      TEXT NOT NULL,
    hit_count   INTEGER NOT NULL DEFAULT 0,
    first_hit_at TEXT,
    last_hit_at TEXT,
    archived    INTEGER NOT NULL DEFAULT 0,
    archived_at TEXT,
    note        TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (layer, entity)
);
CREATE INDEX IF NOT EXISTS idx_usage_last ON usage(last_hit_at);
-- 反馈记录：规则 / 断言命中后，人判它对不对。
--
-- 没有这张表时，误报无处消化：断言报错了你说"这是误报"，
-- 系统记不住，下次还报，报到你把功能关掉为止。
--
-- kind   assertion（技术断言 id）| rule（R1..R5）
-- verdict fp（误报）| tp（确实有问题）
CREATE TABLE IF NOT EXISTS feedback (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    kind    TEXT NOT NULL,
    ref     TEXT NOT NULL,
    verdict TEXT NOT NULL,
    note    TEXT NOT NULL DEFAULT '',
    at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_feedback_ref ON feedback(kind, ref);
"""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


@dataclass
class Record:
    """一条格子记录。``data`` 存该格子的结构化附加字段。"""

    layer: str
    entity: str
    content: str
    data: dict[str, Any] = field(default_factory=dict)
    version: int = 1
    last_updated: str = ""
    chapter_id: str | None = None

    @property
    def chapter_no(self) -> int:
        return chapter_index(self.chapter_id)

    def to_hit(self, *, band: str = "high", confidence: float | None = None) -> "Hit":
        return Hit(
            layer=self.layer,
            entity=self.entity,
            content=self.content,
            version=self.version,
            last_updated=self.last_updated,
            confidence=0.0 if confidence is None else confidence,
            band=band,
            data=dict(self.data),
        )


@dataclass
class Hit:
    """符合 §3.3 返回 Schema 的一条记忆命中。"""

    layer: str
    entity: str
    content: str
    version: int
    last_updated: str
    confidence: float = 0.0
    band: str = "high"
    in_context: bool = False
    data: dict[str, Any] = field(default_factory=dict)
    channels: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "layer": self.layer,
            "entity": self.entity,
            "content": "已在上下文" if self.in_context else self.content,
            "version": int(self.version),
            "last_updated": self.last_updated,
            "confidence": round(float(self.confidence), 4),
        }
        if self.in_context:
            out["in_context"] = True
        if self.data:
            out["data"] = self.data
        return out

    def render(self) -> str:
        return f"[{self.layer}] {self.entity}(v{self.version}): {self.content}"


@dataclass
class Foreshadow:
    """伏笔格记录，状态机 planted -> reinforced -> resolved / abandoned。"""

    id: str
    content: str
    status: str = "planted"
    planted_chapter: str | None = None
    planned_resolve: str | None = None
    reinforced_chapters: list[str] = field(default_factory=list)
    resolved_chapter: str | None = None
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def planted_no(self) -> int:
        return chapter_index(self.planted_chapter)

    @property
    def planned_no(self) -> int:
        return chapter_index(self.planned_resolve)

    @property
    def is_open(self) -> bool:
        """未回收：planted / reinforced。"""
        return self.status in ("planted", "reinforced")


class EnderChest:
    """末影箱。单线程使用；每章一个事务边界。"""

    def __init__(self, path: str | os.PathLike[str] = ":memory:", *, dim: int = _embed.DIM):
        self.path = str(path)
        if self.path != ":memory:":
            parent = os.path.dirname(os.path.abspath(self.path))
            if parent:
                os.makedirs(parent, exist_ok=True)
        self.dim = dim
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(_DDL)
        self.conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self.conn.commit()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "EnderChest":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def commit(self) -> None:
        self.conn.commit()

    # ------------------------------------------------------------------
    # 写：记录（追加版本历史）
    # ------------------------------------------------------------------
    def upsert(
        self,
        layer: str,
        entity: str,
        content: str,
        *,
        data: dict[str, Any] | None = None,
        chapter_id: str | None = None,
        keywords: Sequence[str] | None = None,
        embedding: np.ndarray | None = None,
        change: str = "update",
        commit: bool = True,
    ) -> Record:
        """写入/更新一条记录。已存在则 version+1，旧值留在 history。"""
        if layer not in LAYERS:
            raise ValueError(f"未知格子 {layer!r}；可选：{LAYERS}")
        entity = str(entity) if layer == "alias" else self.resolve_entity(entity)
        content = str(content)
        payload = dumps(data or {})
        vec = embedding if embedding is not None else _embed.embed(f"{entity} {content}")

        row = self.conn.execute(
            "SELECT version FROM records WHERE layer=? AND entity=?", (layer, entity)
        ).fetchone()
        version = (int(row["version"]) + 1) if row else 1
        ts = _now()
        self.conn.execute(
            """
            INSERT INTO records(layer, entity, content, data, version, last_updated, chapter_id, embedding)
            VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(layer, entity) DO UPDATE SET
                content=excluded.content, data=excluded.data, version=excluded.version,
                last_updated=excluded.last_updated, chapter_id=excluded.chapter_id,
                embedding=excluded.embedding
            """,
            (layer, entity, content, payload, version, ts, chapter_id, _embed.to_blob(vec)),
        )
        self.conn.execute(
            """
            INSERT INTO history(layer, entity, version, content, data, chapter_id, updated_at, change)
            VALUES(?,?,?,?,?,?,?,?)
            """,
            (layer, entity, version, content, payload, chapter_id, ts, change),
        )
        self._index_terms(layer, entity, content, keywords, data)
        if commit:
            self.conn.commit()
        return Record(layer, entity, content, dict(data or {}), version, ts, chapter_id)

    def _index_terms(
        self,
        layer: str,
        entity: str,
        content: str,
        keywords: Sequence[str] | None,
        data: dict[str, Any] | None,
    ) -> None:
        """重建该记录的倒排词条：字段加权 + 显式关键词加倍。"""
        self.conn.execute("DELETE FROM terms WHERE layer=? AND entity=?", (layer, entity))
        weights = term_weights(content, entity=entity)
        if data:
            title = str(data.get("title") or data.get("name") or "")
            if title:
                for t, w in term_weights("", title=title).items():
                    weights[t] = weights.get(t, 0.0) + w
        for kw in keywords or ():
            for t, w in term_weights(str(kw), entity=str(kw)).items():
                weights[t] = weights.get(t, 0.0) + w * 2.0
        rows = [(t, layer, entity, float(w)) for t, w in weights.items()]
        if rows:
            self.conn.executemany(
                "INSERT OR REPLACE INTO terms(term, layer, entity, weight) VALUES(?,?,?,?)", rows
            )

    # ------------------------------------------------------------------
    # 写：伏笔格状态机
    # ------------------------------------------------------------------
    def set_foreshadow(
        self,
        fs_id: str,
        content: str,
        *,
        status: str = "planted",
        planted_chapter: str | None = None,
        planned_resolve: str | None = None,
        reinforced_chapters: Sequence[str] | None = None,
        resolved_chapter: str | None = None,
        chapter_id: str | None = None,
        extra: dict[str, Any] | None = None,
        commit: bool = True,
    ) -> Foreshadow:
        """写入/更新伏笔。未给出的字段沿用已有记录，避免状态丢失。"""
        if status not in ("planted", "reinforced", "resolved", "abandoned"):
            raise ValueError(f"非法伏笔状态：{status!r}")
        prev = self.get("foreshadowing", fs_id)
        base = prev.data if prev else {}
        content = content or (prev.content if prev else "")
        planted_chapter = planted_chapter or base.get("planted_chapter")
        planned_resolve = planned_resolve or base.get("planned_resolve")
        resolved_chapter = resolved_chapter or base.get("resolved_chapter")
        chapters = list(reinforced_chapters) if reinforced_chapters is not None else list(base.get("reinforced_chapters", []))
        data: dict[str, Any] = {}
        if extra:
            data.update(extra)
        # 显式参数永远压过 extra：extra 常来自上一版 data 快照，
        # 若无条件 update 会把刚写入的强化/回收字段覆盖回旧值。
        data.update(
            {
                "status": status,
                "planted_chapter": planted_chapter,
                "planned_resolve": planned_resolve,
                "reinforced_chapters": chapters,
                "resolved_chapter": resolved_chapter,
            }
        )
        rec = self.upsert(
            "foreshadowing",
            fs_id,
            content,
            data=data,
            chapter_id=chapter_id or planted_chapter,
            keywords=[fs_id],
            commit=commit,
        )
        return Foreshadow(
            id=fs_id,
            content=rec.content,
            status=status,
            planted_chapter=planted_chapter,
            planned_resolve=planned_resolve,
            reinforced_chapters=chapters,
            resolved_chapter=resolved_chapter,
            data=data,
        )

    def reinforce_foreshadow(self, fs_id: str, chapter_id: str, *, note: str = "") -> Foreshadow:
        """强化：伏笔被再次提及但未回收。"""
        rec = self._require_foreshadow(fs_id)
        chapters = list(rec.data.get("reinforced_chapters", []))
        if chapter_id not in chapters:
            chapters.append(chapter_id)
        extra = dict(rec.data)
        if note:
            extra["last_note"] = note
        return self.set_foreshadow(
            fs_id,
            rec.content,
            status="reinforced",
            planned_resolve=rec.data.get("planned_resolve"),
            reinforced_chapters=chapters,
            resolved_chapter=rec.data.get("resolved_chapter"),
            chapter_id=chapter_id,
            extra=extra,
        )

    def resolve_foreshadow(self, fs_id: str, chapter_id: str, *, note: str = "") -> Foreshadow:
        """回收：伏笔在正文中兑现。"""
        rec = self._require_foreshadow(fs_id)
        extra = dict(rec.data)
        if note:
            extra["resolve_note"] = note
        return self.set_foreshadow(
            fs_id,
            rec.content,
            status="resolved",
            planned_resolve=rec.data.get("planned_resolve"),
            reinforced_chapters=list(rec.data.get("reinforced_chapters", [])),
            resolved_chapter=chapter_id,
            chapter_id=chapter_id,
            extra=extra,
        )

    def abandon_foreshadow(self, fs_id: str, chapter_id: str, *, reason: str = "") -> Foreshadow:
        """废弃：长期未强化，经巡检判定不再回收。"""
        rec = self._require_foreshadow(fs_id)
        extra = dict(rec.data)
        if reason:
            extra["abandon_reason"] = reason
        return self.set_foreshadow(
            fs_id,
            rec.content,
            status="abandoned",
            planned_resolve=rec.data.get("planned_resolve"),
            reinforced_chapters=list(rec.data.get("reinforced_chapters", [])),
            resolved_chapter=rec.data.get("resolved_chapter"),
            chapter_id=chapter_id,
            extra=extra,
        )

    def _require_foreshadow(self, fs_id: str) -> Record:
        rec = self.get("foreshadowing", fs_id)
        if rec is None:
            raise KeyError(f"伏笔不存在：{fs_id}")
        return rec

    def all_foreshadowings(self, *, only_open: bool = False) -> list[Foreshadow]:
        rows = self.conn.execute(
            "SELECT * FROM records WHERE layer='foreshadowing' ORDER BY entity"
        ).fetchall()
        out: list[Foreshadow] = []
        for r in rows:
            d = loads(r["data"], {}) or {}
            fs = Foreshadow(
                id=r["entity"],
                content=r["content"],
                status=d.get("status", "planted"),
                planted_chapter=d.get("planted_chapter"),
                planned_resolve=d.get("planned_resolve"),
                reinforced_chapters=list(d.get("reinforced_chapters", [])),
                resolved_chapter=d.get("resolved_chapter"),
                data=d,
            )
            if only_open and not fs.is_open:
                continue
            out.append(fs)
        return out

    def next_foreshadow_id(self) -> str:
        """下一个可用伏笔 ID（fs_001 起，跳过已占用）。"""
        used = {f.id for f in self.all_foreshadowings()}
        n = 0
        while True:
            n += 1
            cand = make_id("fs", n)
            if cand not in used:
                return cand

    def next_chapter_id(self) -> str:
        """下一个章节 ID：取已有章节与记录引用过的最大章号 +1。"""
        best = 0
        for row in self.iter_chapters():
            best = max(best, chapter_index(str(row.get("chapter_id") or "")))
        for entity in self.all_entities():
            for rec in self.get_any(entity):
                if rec.chapter_id:
                    best = max(best, chapter_index(rec.chapter_id))
        return make_id("ch", best + 1)

    # ------------------------------------------------------------------
    # 写：状态索引 / 称呼格 / 图 / 章节正文
    # ------------------------------------------------------------------
    def set_state(
        self,
        entity: str,
        key: str,
        value: Any,
        *,
        chapter_id: str | None = None,
        commit: bool = True,
    ) -> None:
        """写状态索引（硬事实）：存活、位置、境界、持有物等。只记最新值，版本递增。"""
        canonical = self.resolve_entity(entity)
        val = value if isinstance(value, str) else dumps(value)
        row = self.conn.execute(
            "SELECT version FROM state WHERE entity=? AND key=?", (canonical, key)
        ).fetchone()
        version = (int(row["version"]) + 1) if row else 1
        self.conn.execute(
            """
            INSERT INTO state(entity, key, value, chapter_id, version, updated_at)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(entity, key) DO UPDATE SET
                value=excluded.value, chapter_id=excluded.chapter_id,
                version=excluded.version, updated_at=excluded.updated_at
            """,
            (canonical, key, val, chapter_id, version, _now()),
        )
        if commit:
            self.conn.commit()

    def get_state(self, entity: str, key: str) -> Any:
        row = self.conn.execute(
            "SELECT value FROM state WHERE entity=? AND key=?",
            (self.resolve_entity(entity), key),
        ).fetchone()
        if row is None:
            return None
        try:
            return loads(row["value"])
        except Exception:
            return row["value"]

    def states_of(self, entity: str) -> dict[str, Any]:
        """某实体的全部硬事实（供矛盾检测与上下文组装）。"""
        rows = self.conn.execute(
            "SELECT key, value FROM state WHERE entity=? ORDER BY key",
            (self.resolve_entity(entity),),
        ).fetchall()
        out: dict[str, Any] = {}
        for r in rows:
            try:
                out[r["key"]] = loads(r["value"])
            except Exception:
                out[r["key"]] = r["value"]
        return out

    def state_chapter(self, entity: str, key: str) -> str | None:
        row = self.conn.execute(
            "SELECT chapter_id FROM state WHERE entity=? AND key=?",
            (self.resolve_entity(entity), key),
        ).fetchone()
        return row["chapter_id"] if row else None

    def link_alias(self, alias: str, canonical: str, *, commit: bool = True) -> None:
        """称呼格：登记 别名 -> 标准名；标准名自身也登记，保证幂等解析。"""
        a, c = normalize_name(alias), normalize_name(canonical)
        if not a or not c:
            return
        self.conn.executemany(
            "INSERT OR REPLACE INTO aliases(alias, canonical) VALUES(?,?)", [(a, c), (c, c)]
        )
        if commit:
            self.conn.commit()

    def resolve_entity(self, name: str) -> str:
        """别名 -> 标准名；未登记则返回归一化原名。"""
        n = normalize_name(name)
        if not n:
            return n
        row = self.conn.execute("SELECT canonical FROM aliases WHERE alias=?", (n,)).fetchone()
        return row["canonical"] if row else n

    def display_name(self, canonical: str) -> str:
        """标准名 -> 可读别名（优先返回登记过的别名）。"""
        c = normalize_name(canonical)
        rows = self.conn.execute(
            "SELECT alias FROM aliases WHERE canonical=? ORDER BY length(alias) DESC", (c,)
        ).fetchall()
        return rows[0]["alias"] if rows else canonical

    def synonyms(self, entity: str) -> list[str]:
        c = self.resolve_entity(entity)
        rows = self.conn.execute(
            "SELECT alias FROM aliases WHERE canonical=? ORDER BY alias", (c,)
        ).fetchall()
        return [r["alias"] for r in rows]

    def add_edge(
        self,
        src: str,
        rel: str,
        dst: str,
        *,
        chapter_id: str | None = None,
        data: dict[str, Any] | None = None,
        commit: bool = True,
    ) -> None:
        s, d = self.resolve_entity(src), self.resolve_entity(dst)
        self.conn.execute(
            "INSERT OR REPLACE INTO edges(src, rel, dst, chapter_id, data) VALUES(?,?,?,?,?)",
            (s, rel, d, chapter_id, dumps(data or {})),
        )
        if commit:
            self.conn.commit()

    def put_chapter(
        self,
        chapter_id: str,
        text: str,
        *,
        title: str = "",
        summary: str = "",
        commit: bool = True,
    ) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO chapters(chapter_id, title, text, summary, word_count, created_at)
            VALUES(?,?,?,?,?,?)
            """,
            (chapter_id, title, text, summary, len(text), _now()),
        )
        if commit:
            self.conn.commit()

    def get_chapter(self, chapter_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM chapters WHERE chapter_id=?", (chapter_id,)
        ).fetchone()
        return dict(row) if row else None

    def recent_chapters(self, n: int = 1, *, before: str | None = None) -> list[dict[str, Any]]:
        """最近 n 章正文（供上下文 P2 层）。``before`` 用于排除当前章。"""
        if before:
            rows = self.conn.execute(
                "SELECT * FROM chapters WHERE chapter_id < ? ORDER BY chapter_id DESC LIMIT ?",
                (before, n),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM chapters ORDER BY chapter_id DESC LIMIT ?", (n,)
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def iter_chapters(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM chapters ORDER BY chapter_id").fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # 读：单条 / 历史 / 邻域 / 词表
    # ------------------------------------------------------------------
    def delete(self, layer: str, entity: str) -> bool:
        """删除一条记录及其索引。

        连带清理：
          * records 行
          * terms 倒排词条里指向该记录的行
          * edges 里以该实体为端点的边
          * state 表里的状态行

        history 保留（那是追加式审计轨迹，不该被删掉，
        以便日后查"这条是什么时候没的"）。

        返回是否真的删掉了一条记录。
        """
        e = self.resolve_entity(entity) if layer != "alias" else normalize_name(entity)
        row = self.conn.execute(
            "SELECT 1 FROM records WHERE layer=? AND entity=?", (layer, e)
        ).fetchone()
        if not row:
            return False

        self.conn.execute("DELETE FROM records WHERE layer=? AND entity=?", (layer, e))
        for table in ("terms", "state"):
            try:
                self.conn.execute(
                    f"DELETE FROM {table} WHERE layer=? AND entity=?", (layer, e)
                )
            except sqlite3.Error:
                # 某些构建里没有该表，忽略
                pass
        try:
            self.conn.execute(
                "DELETE FROM edges WHERE src=? OR dst=?", (e, e)
            )
        except sqlite3.Error:
            pass
        self.conn.commit()
        return True

    # ------------------------------------------------------------------
    # 元信息（当前章节等）
    # ------------------------------------------------------------------
    def set_meta(self, key: str, value: str, *, commit: bool = True) -> None:
        """写入一条库级元信息。"""
        try:
            self.conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(key), str(value)),
            )
            if commit:
                self.conn.commit()
        except sqlite3.Error as exc:
            log.warning("写 meta 失败 %s：%s", key, exc)

    def get_meta(self, key: str, default: str = "") -> str:
        """读取一条库级元信息。"""
        try:
            row = self.conn.execute(
                "SELECT value FROM meta WHERE key=?", (str(key),)
            ).fetchone()
        except sqlite3.Error:
            return default
        return str(row["value"]) if row else default

    @property
    def current_chapter(self) -> str:
        """这个作品「写到第几章」——用于按叙事位置检索。"""
        return self.get_meta("current_chapter", "")

    # ------------------------------------------------------------------
    # 反馈记录：规则/断言命中的对错判定
    # ------------------------------------------------------------------
    def add_feedback(
        self, kind: str, ref: str, verdict: str, *, note: str = "", commit: bool = True
    ) -> None:
        """记一次判定：``fp``=误报，``tp``=确实有问题。"""
        v = str(verdict or "").strip().lower()
        if v not in ("fp", "tp"):
            raise ValueError(f"verdict 只能是 fp / tp，收到 {verdict!r}")
        try:
            self.conn.execute(
                "INSERT INTO feedback(kind, ref, verdict, note, at) VALUES(?,?,?,?,?)",
                (str(kind), str(ref), v, str(note or ""), _now()),
            )
            if commit:
                self.conn.commit()
        except sqlite3.Error as exc:
            log.warning("写反馈失败：%s", exc)

    def feedback_stats(self, kind: str = "", ref: str = "") -> dict[str, Any]:
        """统计：每条规则/断言被判过几次误报、几次真问题。"""
        out: dict[str, dict[str, int]] = {}
        try:
            sql = "SELECT kind, ref, verdict, COUNT(*) AS n FROM feedback"
            cond, params = [], []
            if kind:
                cond.append("kind=?")
                params.append(kind)
            if ref:
                cond.append("ref=?")
                params.append(ref)
            if cond:
                sql += " WHERE " + " AND ".join(cond)
            sql += " GROUP BY kind, ref, verdict"
            rows = self.conn.execute(sql, params).fetchall()
        except sqlite3.Error:
            return {}
        for r in rows:
            key = f"{r['kind']}:{r['ref']}"
            slot = out.setdefault(key, {"fp": 0, "tp": 0})
            slot[str(r["verdict"])] = int(r["n"])
        for slot in out.values():
            total = slot["fp"] + slot["tp"]
            slot["total"] = total
            slot["fp_rate"] = round(slot["fp"] / total, 3) if total else 0.0
        return out

    def counts(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT layer, COUNT(*) AS n FROM records GROUP BY layer"
        ).fetchall()
        out = {layer: 0 for layer in LAYERS}
        for r in rows:
            out[r["layer"]] = int(r["n"])
        return out

    # ------------------------------------------------------------------
    # 使用记录（回收归档）
    #
    # 设计约束：**只标记，不删除**。
    # 末影箱在文学场景下兼当设定集，一条设定可能在几百章后被再次引用，
    # 删掉等于永久丢失。所以这里只有「未使用 / 已使用（归档）」两种状态。
    # ------------------------------------------------------------------
    def bump_usage(self, layer: str, entity: str, *, commit: bool = True) -> None:
        """记一次命中：计数 +1，刷新最近使用时间，并置为「已回收归档」。

        「AI 用了就算回收」——这条记录已经进入过写作上下文、发挥过作用。
        但**内容一条不删**：归档只是状态标记，设定集里的条目
        随时还要能查、能引用、能参与一致性核对。

        不参与版本管理，也不写 history——使用记录是元数据，
        不该把内容版本号推高。
        """
        now = _now()
        try:
            self.conn.execute(
                "INSERT INTO usage(layer, entity, hit_count, first_hit_at, last_hit_at, archived, archived_at) "
                "VALUES(?, ?, 1, ?, ?, 1, ?) "
                "ON CONFLICT(layer, entity) DO UPDATE SET "
                "hit_count = hit_count + 1, last_hit_at = excluded.last_hit_at, "
                "archived = 1, "
                "archived_at = COALESCE(usage.archived_at, excluded.archived_at)",
                (layer, entity, now, now, now),
            )
            if commit:
                self.conn.commit()
        except sqlite3.Error:
            # 使用记录失败绝不该影响检索本身
            pass

    def usage_of(self, layer: str, entity: str) -> dict[str, Any] | None:
        """某条记录的使用情况；从未命中过返回 None。"""
        try:
            row = self.conn.execute(
                "SELECT * FROM usage WHERE layer=? AND entity=?", (layer, entity)
            ).fetchone()
        except sqlite3.Error:
            return None
        if not row:
            return None
        return {
            "hit_count": int(row["hit_count"] or 0),
            "first_hit_at": row["first_hit_at"],
            "last_hit_at": row["last_hit_at"],
            "archived": bool(row["archived"]),
            "archived_at": row["archived_at"],
            "note": row["note"] or "",
        }

    def usage_map(self) -> dict[tuple[str, str], dict[str, Any]]:
        """整库的使用情况，键是 (layer, entity)。"""
        out: dict[tuple[str, str], dict[str, Any]] = {}
        try:
            rows = self.conn.execute("SELECT * FROM usage").fetchall()
        except sqlite3.Error:
            return out
        for row in rows:
            out[(row["layer"], row["entity"])] = {
                "hit_count": int(row["hit_count"] or 0),
                "first_hit_at": row["first_hit_at"],
                "last_hit_at": row["last_hit_at"],
                "archived": bool(row["archived"]),
                "archived_at": row["archived_at"],
                "note": row["note"] or "",
            }
        return out

    def set_archived(
        self, layer: str, entity: str, archived: bool = True, *, note: str = ""
    ) -> bool:
        """手动设置/取消「已回收归档」。

        归档只影响面板上的归类与提示，**不影响检索**——
        设定集里的每一条都必须随时可查。
        """
        now = _now()
        try:
            if archived:
                self.conn.execute(
                    "INSERT INTO usage(layer, entity, hit_count, archived, archived_at, note) "
                    "VALUES(?, ?, 0, 1, ?, ?) "
                    "ON CONFLICT(layer, entity) DO UPDATE SET "
                    "archived = 1, archived_at = excluded.archived_at, "
                    "note = excluded.note",
                    (layer, entity, now, note),
                )
            else:
                self.conn.execute(
                    "INSERT INTO usage(layer, entity, hit_count, archived) "
                    "VALUES(?, ?, 0, 0) "
                    "ON CONFLICT(layer, entity) DO UPDATE SET archived = 0, archived_at = NULL",
                    (layer, entity),
                )
            self.conn.commit()
            return True
        except sqlite3.Error:
            return False

    def usage_summary(self) -> dict[str, int]:
        """使用概况：已归档 / 用过未归档 / 从未使用。"""
        total = 0
        try:
            total = int(
                self.conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
            )
        except sqlite3.Error:
            return {"total": 0, "archived": 0, "used": 0, "unused": 0}

        archived = used = 0
        try:
            row = self.conn.execute(
                "SELECT "
                "SUM(CASE WHEN archived=1 THEN 1 ELSE 0 END) AS a, "
                "SUM(CASE WHEN archived=0 AND hit_count>0 THEN 1 ELSE 0 END) AS u "
                "FROM usage"
            ).fetchone()
            archived = int(row["a"] or 0)
            used = int(row["u"] or 0)
        except sqlite3.Error:
            pass

        return {
            "total": total,
            "archived": archived,
            "used": used,
            "unused": max(0, total - archived - used),
        }

    def get(self, layer: str, entity: str) -> Record | None:
        e = self.resolve_entity(entity) if layer != "alias" else normalize_name(entity)
        row = self.conn.execute(
            "SELECT * FROM records WHERE layer=? AND entity=?", (layer, e)
        ).fetchone()
        return self._row_to_record(row) if row else None

    def get_any(self, entity: str, layers: Sequence[str] | None = None) -> list[Record]:
        """跨格子取同一实体的记录（可限定 layers）。"""
        e = self.resolve_entity(entity)
        ls = list(layers or LAYERS)
        q = ",".join("?" * len(ls))
        rows = self.conn.execute(
            f"SELECT * FROM records WHERE entity=? AND layer IN ({q}) ORDER BY layer", (e, *ls)
        ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def history(self, layer: str, entity: str) -> list[Record]:
        """该记录的全部历史版本（追加不覆盖的证据）。"""
        e = self.resolve_entity(entity) if layer != "alias" else normalize_name(entity)
        rows = self.conn.execute(
            "SELECT * FROM history WHERE layer=? AND entity=? ORDER BY version", (layer, e)
        ).fetchall()
        return [
            Record(
                layer=r["layer"],
                entity=r["entity"],
                content=r["content"],
                data=loads(r["data"], {}) or {},
                version=int(r["version"]),
                last_updated=r["updated_at"],
                chapter_id=r["chapter_id"],
            )
            for r in rows
        ]

    def neighbors(self, entity: str, *, hops: int = 1, rel: str | None = None) -> list[tuple[str, str, str]]:
        """图索引邻域，返回 [(src, rel, dst)]。"""
        frontier = {self.resolve_entity(entity)}
        seen: set[str] = set(frontier)
        found: list[tuple[str, str, str]] = []
        for _ in range(max(1, hops)):
            if not frontier:
                break
            nxt: set[str] = set()
            for node in frontier:
                if rel:
                    rows = self.conn.execute(
                        "SELECT src,rel,dst FROM edges WHERE (src=? OR dst=?) AND rel=?",
                        (node, node, rel),
                    ).fetchall()
                else:
                    rows = self.conn.execute(
                        "SELECT src,rel,dst FROM edges WHERE src=? OR dst=?", (node, node)
                    ).fetchall()
                for r in rows:
                    trip = (r["src"], r["rel"], r["dst"])
                    if trip not in found:
                        found.append(trip)
                    for x in (r["src"], r["dst"]):
                        if x not in seen:
                            seen.add(x)
                            nxt.add(x)
            frontier = nxt
        return found

    def all_entities(self, layers: Sequence[str] | None = None) -> list[str]:
        ls = list(layers or LAYERS)
        q = ",".join("?" * len(ls))
        rows = self.conn.execute(
            f"SELECT DISTINCT entity FROM records WHERE layer IN ({q}) ORDER BY entity", tuple(ls)
        ).fetchall()
        return [r["entity"] for r in rows]

    def entity_vocab(self) -> frozenset[str]:
        """可识别实体词表（含别名），供流式检测 / 异步预取使用。"""
        vocab: set[str] = set()
        for r in self.conn.execute("SELECT entity FROM records"):
            vocab.add(r["entity"])
        for r in self.conn.execute("SELECT alias FROM aliases"):
            vocab.add(r["alias"])
        return frozenset(v for v in vocab if len(v) >= 2)

    def detect_entities(self, text: str) -> list[str]:
        """按词表从文本中抽取实体（长词优先，防子串误命中）。"""
        return extract_entities(text, self.entity_vocab())

    def stats(self) -> dict[str, Any]:
        out: dict[str, Any] = {"path": self.path, "records": {}, "total_records": 0}
        for layer in LAYERS:
            c = self.conn.execute(
                "SELECT COUNT(*) AS c FROM records WHERE layer=?", (layer,)
            ).fetchone()["c"]
            out["records"][layer] = int(c)
            out["total_records"] += int(c)
        for key, table in (
            ("history", "history"),
            ("terms", "terms"),
            ("edges", "edges"),
            ("aliases", "aliases"),
            ("chapters", "chapters"),
            ("state", "state"),
        ):
            out[key] = int(
                self.conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
            )
        out["open_foreshadowings"] = len(self.all_foreshadowings(only_open=True))
        return out

    def _row_to_record(self, row: sqlite3.Row) -> Record:
        return Record(
            layer=row["layer"],
            entity=row["entity"],
            content=row["content"],
            data=loads(row["data"], {}) or {},
            version=int(row["version"]),
            last_updated=row["last_updated"],
            chapter_id=row["chapter_id"],
        )

    # ------------------------------------------------------------------
    # 读：三重索引检索（关键词 / 向量 / 图）
    # ------------------------------------------------------------------
    def search_terms(
        self,
        terms: Iterable[str],
        *,
        layers: Sequence[str] | None = None,
        limit: int = 50,
    ) -> list[tuple[Record, float]]:
        """关键词倒排检索。打分 = 词权重和 × IDF / 长度归一。"""
        term_list = [t for t in terms if t]
        if not term_list:
            return []
        ls = list(layers or LAYERS)
        ql = ",".join("?" * len(term_list))
        ll = ",".join("?" * len(ls))
        rows = self.conn.execute(
            f"""
            SELECT layer, entity, SUM(weight) AS score
            FROM terms
            WHERE term IN ({ql}) AND layer IN ({ll})
            GROUP BY layer, entity
            """,
            (*term_list, *ls),
        ).fetchall()
        if not rows:
            return []
        total = max(
            1, int(self.conn.execute("SELECT COUNT(*) AS c FROM records").fetchone()["c"])
        )
        out: list[tuple[Record, float]] = []
        for r in rows:
            rec = self.get(r["layer"], r["entity"])
            if rec is None:
                continue
            raw = float(r["score"])
            idf = 1.0 + float(np.log(total / (1.0 + raw)))
            score = raw * idf / (1.0 + 0.01 * len(rec.content))
            out.append((rec, score))
        out.sort(key=lambda x: (-x[1], x[0].layer, x[0].entity))
        return out[:limit]

    def search_vector(
        self,
        query: str | np.ndarray,
        *,
        layers: Sequence[str] | None = None,
        limit: int = 50,
    ) -> list[tuple[Record, float]]:
        """向量检索：numpy 暴力 cosine（无 sqlite_vec 依赖，可注入真嵌入）。"""
        qv = query if isinstance(query, np.ndarray) else _embed.embed(query)
        qv = np.asarray(qv, dtype=np.float32)
        if float(np.linalg.norm(qv)) == 0.0:
            return []
        ls = list(layers or LAYERS)
        ll = ",".join("?" * len(ls))
        rows = self.conn.execute(
            f"SELECT layer, entity, embedding FROM records WHERE layer IN ({ll})", tuple(ls)
        ).fetchall()
        if not rows:
            return []
        mat = np.vstack([_embed.from_blob(r["embedding"], self.dim) for r in rows])
        sims = _embed.cosine_matrix(qv, mat)
        out: list[tuple[Record, float]] = []
        for i in np.argsort(-sims)[:limit]:
            s = float(sims[int(i)])
            if s <= 0.0:
                continue
            rec = self.get(rows[int(i)]["layer"], rows[int(i)]["entity"])
            if rec is None:
                continue
            out.append((rec, s))
        return out

    def search_graph(
        self,
        entities: Sequence[str],
        *,
        hops: int = 1,
        layers: Sequence[str] | None = None,
        limit: int = 50,
    ) -> list[tuple[Record, float]]:
        """图索引检索：从种子实体沿边扩展，距离越近分越高。"""
        seeds = [e for e in entities if e]
        if not seeds:
            return []
        ls = list(layers or LAYERS)
        scores: dict[tuple[str, str], float] = {}
        for seed in seeds:
            root = self.resolve_entity(seed)
            # 种子实体本身即最高相关（0 跳）
            for r in self.get_any(root, ls):
                key = (r.layer, r.entity)
                scores[key] = max(scores.get(key, 0.0), 1.0)
            frontier = {root}
            seen = {root}
            for depth in range(1, max(1, hops) + 1):
                nxt: set[str] = set()
                for node in frontier:
                    for s, _rel, d in self.neighbors(node, hops=1):
                        for other in (s, d):
                            if other in seen:
                                continue
                            seen.add(other)
                            nxt.add(other)
                            for r in self.get_any(other, ls):
                                key = (r.layer, r.entity)
                                scores[key] = max(scores.get(key, 0.0), 1.0 / (depth + 1))
                frontier = nxt
                if not frontier:
                    break
        out: list[tuple[Record, float]] = []
        for (layer, entity), s in scores.items():
            rec = self.get(layer, entity)
            if rec is not None:
                out.append((rec, s))
        out.sort(key=lambda x: (-x[1], x[0].layer, x[0].entity))
        return out[:limit]

    def search(
        self,
        query: str,
        entities: Sequence[str] = (),
        *,
        layers: Sequence[str] | None = None,
        limit: int = 50,
    ) -> list[tuple[Record, float]]:
        """三路召回合并：同一记录取最大分，并记录命中通道数。"""
        merged: dict[tuple[str, str], tuple[Record, float, int]] = {}

        def add(pairs: Iterable[tuple[Record, float]]) -> None:
            for rec, sc in pairs:
                key = (rec.layer, rec.entity)
                old = merged.get(key)
                if old is None:
                    merged[key] = (rec, sc, 1)
                else:
                    best = max(old[1], sc)
                    merged[key] = (old[0], best, old[2] + 1)

        add(self.search_terms(analyze(query), layers=layers, limit=limit))
        add(self.search_vector(query, layers=layers, limit=limit))
        if entities:
            add(self.search_graph(entities, layers=layers, limit=limit))
        out = [(rec, sc * (1.0 + 0.15 * (n - 1))) for rec, sc, n in merged.values()]
        out.sort(key=lambda x: (-x[1], x[0].layer, x[0].entity))
        return out[:limit]
