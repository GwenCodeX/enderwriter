"""JSON 桥：给外部进程（如 DSH 插件）提供一条稳定的命令行 RPC 入口。

设计目标：**一次调用一个 JSON，绝不往 stdout 混非 JSON**。
所有日志走 stderr，stdout 只输出一个 JSON 对象，方便 Node/Python 任何一侧解析。

用法::

    python -m enderwriter.bridge '{"op":"search","entities":["张三"],"query":"月蚀"}'
    python -m enderwriter.bridge --op stats --db story.db
    get-content req.json | python -m enderwriter.bridge --stdin

支持的 op：

    info          环境与库概况（不需要 db 也可）
    search        search_memory 检索（entities / query_context / layers / budget）
    patrol        伏笔巡检（chapter / grace / stale_threshold）
    stats         末影箱统计
    writeback     应用一份 §3.5 回写报告
    demo          跑一次六阶段流水线，返回结构化摘要
    install       环境检测（check=True 只检查）

退出码：0 成功；1 业务错误（JSON 里带 error 字段）；2 参数错误。
"""

from __future__ import annotations

import io
import json
import os
import sys
import traceback
from typing import Any

__all__ = ["handle", "main"]

#: 包根目录（enderwriter/ 的上级），用于定位默认数据库
_PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _ok(payload: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, **payload}


def _err(message: str, *, code: str = "bad_request", detail: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {"ok": False, "error": {"code": code, "message": message}}
    if detail:
        out["error"]["detail"] = detail
    return out


# --------------------------------------------------------------------------
# 末影箱打开
# --------------------------------------------------------------------------
def _open(db: str | None, *, seed: bool = True):
    from .chest import EnderChest

    path = (db or "").strip() or ":memory:"
    chest = EnderChest(path)
    if seed:
        stats = chest.stats()
        if stats["total_records"] == 0 and stats["chapters"] == 0:
            from .seed import seed_demo

            seed_demo(chest)
    return chest


def _open_scoped(args: dict[str, Any], *, layer: str | None = None):
    """按分层记忆打开末影箱，返回 (chest, meta)。

    规则：
      * 显式给了 ``db`` → 单库模式（只有这种情况才会灌示例数据）；
      * 否则按 work/session_id 解析出作品库或全局库。
    读取类 op 一律走这里，避免"没传 work 就掉进临时库 + 种子数据"的老问题。
    """
    db = str(args.get("db") or "").strip()
    if db:
        return _open(db, seed=bool(args.get("seed", True))), {"mode": "single", "path": db}

    from .memory import SCOPE_GLOBAL, SCOPE_WORK, route

    mem = _memory(args)
    scope = route(layer) if layer else SCOPE_WORK
    if scope == SCOPE_WORK and not mem.exists(SCOPE_WORK):
        return None, {
            "mode": "scoped",
            "scope": scope,
            "path": mem.work_path,
            "exists": False,
        }
    return mem.open(scope), {
        "mode": "scoped",
        "scope": scope,
        "path": mem.path_for(scope),
        "work": mem.work_name,
        "exists": True,
    }


def _close(chest) -> None:
    try:
        chest.close()
    except Exception:  # noqa: BLE001 - 关闭失败不影响结果
        pass


# --------------------------------------------------------------------------
# 分层记忆（作品库 + 全局文学库）
# --------------------------------------------------------------------------
def _memory(args: dict[str, Any]):
    """按参数构造 WorkMemory。

    作品名解析优先级：显式 work > 会话专属作品（默认按对话隔离）> 无。
    """
    from .memory import WorkMemory, resolve_work

    name = resolve_work(
        work=str(args.get("work") or ""),
        session_id=str(args.get("session_id") or ""),
    )
    return WorkMemory(name or None, home=args.get("home") or None)


def op_remember(args: dict[str, Any]) -> dict[str, Any]:
    """写入一条记忆，自动路由到作品库或全局库。

    参数：
      layer    六格之一（character/world/timeline/foreshadowing/item/style）
      entity   实体名（人物名、设定 ID、伏笔 ID…）
      content  正文
      scope    work | global，缺省按层自动判断
      work     作品名（scope=work 时必填）
      keywords 附加关键词
    """
    from .memory import SCOPE_WORK, route

    layer = str(args.get("layer") or "").strip()
    entity = str(args.get("entity") or "").strip()
    content = str(args.get("content") or "").strip()
    if not entity:
        return _err("entity 必填", code="bad_request")
    if not content:
        return _err("content 必填", code="bad_request")
    if layer and layer not in ("character", "world", "timeline", "foreshadowing", "item", "style"):
        return _err(f"未知 layer：{layer!r}", code="bad_request")

    scope = route(layer or "style", scope=args.get("scope"))
    mem = _memory(args)
    if scope == SCOPE_WORK and not mem.work_id:
        # 没给作品名时退回全局，避免静默写进一个叫 untitled 的库
        scope = "global"

    keywords = args.get("keywords") or []
    if isinstance(keywords, str):
        keywords = [k.strip() for k in keywords.replace("，", ",").split(",") if k.strip()]

    chest = mem.open(scope)
    try:
        if layer == "foreshadowing":
            # 伏笔不能走普通 upsert：
            # set_foreshadow 会把 status / planted_chapter / planned_resolve
            # 归一进 data，并保证未提供的字段沿用旧值。
            # 走 upsert 的话这些字段散落在 data 里，patrol 读不到，巡检永远为空。
            extra = args.get("data") if isinstance(args.get("data"), dict) else {}
            status = str(extra.get("status") or "planted")

            # 埋设章节：优先显式指定，其次库内最近章节。
            # 两处都没有就留空——**不编造**章节号，否则陈旧判断会误报。
            planted = (
                args.get("chapter_id")
                or extra.get("planted_chapter")
                or None
            )
            if not planted:
                try:
                    recent = chest.recent_chapters(1)
                    if recent:
                        planted = recent[0].get("chapter_id") or None
                except Exception:  # noqa: BLE001 - 章节表缺失不该阻断写入
                    planted = None

            fs = chest.set_foreshadow(
                entity,
                content,
                status=status,
                planted_chapter=planted,
                planned_resolve=extra.get("planned_resolve") or None,
                reinforced_chapters=extra.get("reinforced_chapters"),
                resolved_chapter=extra.get("resolved_chapter") or None,
                chapter_id=planted,
                extra=extra,
            )
            rec_layer = "foreshadowing"
            rec_version = fs.data.get("version") or 1
            try:
                got = chest.get("foreshadowing", entity)
                rec_version = got.version if got else 1
            except Exception:  # noqa: BLE001
                pass
        else:
            rec = chest.upsert(
                layer or "style",
                entity,
                content,
                data=args.get("data") if isinstance(args.get("data"), dict) else None,
                chapter_id=args.get("chapter_id") or None,
                keywords=keywords or None,
            )
            rec_layer = rec.layer
            rec_version = rec.version
        total = chest.stats()["total_records"]
    finally:
        _close(chest)

    return _ok(
        {
            "scope": scope,
            "work": mem.work_name,
            "path": mem.path_for(scope),
            "record": {
                "layer": rec_layer,
                "entity": entity,
                "version": rec_version,
                "content": content,
            },
            "total_in_scope": total,
        }
    )


def op_recall(args: dict[str, Any]) -> dict[str, Any]:
    """跨两层检索：作品库优先，全局库补充。"""
    from .common import SchemaError
    from .memory import SCOPE_GLOBAL, SCOPE_WORK
    from .retrieval import MemoryKeeper

    entities = args.get("entities") or []
    if isinstance(entities, str):
        entities = [x.strip() for x in entities.replace("，", ",").split(",") if x.strip()]
    entities = [str(e).strip() for e in entities if str(e).strip()]
    if not entities:
        return _err("entities 不能为空", code="bad_request")

    budget = int(args.get("budget") or 800)
    # 作品库拿大头，全局库拿小头（经验类内容通常更短）
    work_budget = int(budget * 0.7)
    global_budget = max(120, budget - work_budget)

    mem = _memory(args)
    scopes: list[tuple[str, int]] = []
    if mem.work_id:
        scopes.append((SCOPE_WORK, work_budget))
    scopes.append((SCOPE_GLOBAL, global_budget))

    results: dict[str, Any] = {}
    errors: list[str] = []
    for scope, b in scopes:
        if scope == SCOPE_WORK and not mem.exists(SCOPE_WORK):
            results[scope] = {"high": [], "medium": [], "low": [], "total_tokens": 0, "note": "作品库尚未建立"}
            continue
        try:
            chest = mem.open(scope)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{scope}: {exc}")
            continue
        try:
            keeper = MemoryKeeper(chest)
            req = {"entities": entities, "query_context": str(args.get("query_context") or ""), "budget": b}
            if args.get("layers"):
                req["layers"] = args["layers"]
            # 按叙事位置检索：写第 N 章时只看这一章之前成立的设定
            at_chapter = str(args.get("at_chapter") or "").strip()
            if at_chapter:
                req["at_chapter"] = at_chapter
            results[scope] = keeper.search(req)
        except SchemaError as exc:
            errors.append(f"{scope}: {exc}")
        finally:
            _close(chest)

    merged_tokens = sum(int(r.get("total_tokens") or 0) for r in results.values() if isinstance(r, dict))
    # 各库里被显式标记为「已废止」的实体：正文不返回，但要把名字报出来，
    # 否则用户会以为库中根本没有这条设定。
    superseded: list[str] = []
    for r in results.values():
        if isinstance(r, dict):
            superseded.extend(r.get("superseded") or [])
    return _ok(
        {
            "work": mem.work_name,
            "scopes": results,
            "superseded": sorted(set(superseded)),
            "total_tokens": merged_tokens,
            "errors": errors,
        }
    )


def op_works(args: dict[str, Any]) -> dict[str, Any]:
    """列出所有作品库，并标出当前会话/当前指定的那一本。"""
    from .memory import list_works, slugify

    items = list_works(args.get("home") or None)
    current = _memory(args).work_name
    cur_slug = slugify(current) if current else ""
    for it in items:
        # 库文件名是 slug（中文名会带哈希后缀），用前缀匹配判断"是不是当前这本"
        it["is_current"] = bool(cur_slug) and it["id"] == cur_slug
    return _ok({"works": items, "count": len(items), "current": current})


def op_clear(args: dict[str, Any]) -> dict[str, Any]:
    """手动清除记忆。

    target:
      work     清除当前作品的库（人物/世界/时间线/伏笔/物品 + 正文）
      global   清除全局文学库（风格与经验）
      all      两者都清
      work:<作品名>  清除指定作品的库
    会先备份为 .bak-<时间戳>，再删除原文件，误删可手动恢复。
    """
    import shutil
    import time

    from .memory import SCOPE_GLOBAL, SCOPE_WORK, WorkMemory, default_home

    target = str(args.get("target") or "").strip()
    if not target:
        return _err("target 必填：work | global | all | work:<作品名>", code="bad_request")

    home = os.path.abspath(args.get("home") or default_home())
    stamp = time.strftime("%Y%m%d-%H%M%S")
    cleared: list[dict[str, Any]] = []

    def clear_one(path: str, label: str) -> None:
        if not path or not os.path.isfile(path):
            cleared.append({"target": label, "path": path, "status": "not_found"})
            return
        backup = f"{path}.bak-{stamp}"
        try:
            shutil.move(path, backup)
            size_kb = round(os.path.getsize(backup) / 1024, 1)
            cleared.append(
                {"target": label, "path": path, "status": "cleared", "backup": backup, "size_kb": size_kb}
            )
        except OSError as exc:
            cleared.append({"target": label, "path": path, "status": "failed", "error": str(exc)})

    if target == "all":
        mem = _memory(args)
        if mem.work_id:
            clear_one(mem.work_path, f"作品库「{mem.work_name}」")
        clear_one(mem.global_path, "全局文学库")
    elif target == "global":
        clear_one(os.path.join(home, "global.db"), "全局文学库")
    elif target.startswith("work:"):
        name = target.split(":", 1)[1].strip()
        if not name:
            return _err("work: 后面要跟作品名", code="bad_request")
        mem = WorkMemory(name, home=home)
        clear_one(mem.work_path, f"作品库「{name}」")
    elif target == "work":
        # 走 _memory，这样"清当前对话的作品"也能正确解析到会话库
        mem = _memory(args)
        if not mem.work_id:
            return _err("清除作品库需要指定 work 或 session_id", code="bad_request")
        clear_one(mem.work_path, f"作品库「{mem.work_name}」")
    else:
        return _err(f"未知 target：{target!r}", code="bad_request")

    failed = [c for c in cleared if c["status"] == "failed"]
    return _ok(
        {
            "cleared": cleared,
            "backups": [c["backup"] for c in cleared if c.get("backup")],
            "ok_count": len([c for c in cleared if c["status"] == "cleared"]),
            "failed": failed,
        }
    )


def op_forget_entry(args: dict[str, Any]) -> dict[str, Any]:
    """删除单条记忆（按图层 + 实体名）。

    与 op_clear 的区别：clear 删整库，这里只删一条词条。
    删之前会把整库备份为 .bak-<时间戳>，所以误删可以恢复。

    参数：
      layer   图层（character/world/timeline/foreshadowing/item/style）
      entity  实体名
      work    可选；省略时按会话解析（全局库层 style 自动路由）
    """
    import shutil
    import time

    layer = str(args.get("layer") or "").strip()
    entity = str(args.get("entity") or "").strip()
    if not layer or not entity:
        return _err("layer 与 entity 都必填", code="bad_request")

    mem = _memory(args)
    stamp = time.strftime("%Y%m%d-%H%M%S")

    # style 层在全局库，其余在作品库
    is_global_layer = layer == "style"
    if is_global_layer:
        path = mem.global_path
        label = "全局文学库"
    else:
        if not mem.work_id:
            return _err(
                "删除作品库词条需要指定 work 或 session_id", code="bad_request"
            )
        path = mem.work_path
        label = f"作品库「{mem.work_name}」"

    if not os.path.isfile(path):
        return _ok(
            {
                "removed": False,
                "reason": "not_found",
                "detail": f"{label} 不存在",
                "layer": layer,
                "entity": entity,
            }
        )

    # 备份，保证可恢复
    backup = f"{path}.bak-{stamp}"
    try:
        shutil.copy2(path, backup)
    except OSError as exc:
        return _err(f"备份失败，已中止删除：{exc}", code="backup_failed")

    from .chest import EnderChest

    chest = EnderChest(path)
    try:
        rec = chest.get(layer, entity)
        if rec is None:
            return _ok(
                {
                    "removed": False,
                    "reason": "not_found",
                    "detail": f"{label} 里没有「{entity}」",
                    "layer": layer,
                    "entity": entity,
                    "backup": backup,
                }
            )
        removed_version = rec.version
        chest.delete(layer, entity)
        remaining = chest.counts().get(layer, 0)
    finally:
        chest.close()

    return _ok(
        {
            "removed": True,
            "layer": layer,
            "entity": entity,
            "removed_version": removed_version,
            "remaining_in_layer": remaining,
            "backup": backup,
            "scope": label,
        }
    )


def op_overview(args: dict[str, Any]) -> dict[str, Any]:
    """面板用：两层记忆的完整概况 + 伏笔 + 统计。"""
    from .memory import SCOPE_GLOBAL, SCOPE_WORK, list_works

    mem = _memory(args)
    info: dict[str, Any] = {"home": mem.home, "works": list_works(args.get("home") or None)}

    # 作品层
    work_block: dict[str, Any] = {
        "name": mem.work_name,
        "id": mem.work_id,
        "exists": mem.exists(SCOPE_WORK) if mem.work_id else False,
    }
    if mem.work_id and work_block["exists"]:
        chest = mem.open(SCOPE_WORK)
        try:
            work_block["stats"] = dict(chest.stats())
            work_block["foreshadowings"] = [
                {
                    "id": f.id,
                    "content": f.content,
                    "status": f.status,
                    "planted_chapter": f.planted_chapter,
                    "planned_resolve": f.planned_resolve,
                    "resolved_chapter": f.resolved_chapter,
                    "is_open": f.is_open,
                }
                for f in chest.all_foreshadowings()
            ]
            chapter = str(args.get("chapter") or "")
            if chapter:
                from .writeback import patrol

                work_block["patrol"] = {
                    "chapter": chapter,
                    "items": patrol(chest, chapter),
                }
        finally:
            _close(chest)

    # 全局层
    global_block: dict[str, Any] = {"exists": mem.exists(SCOPE_GLOBAL)}
    if global_block["exists"]:
        chest = mem.open(SCOPE_GLOBAL)
        try:
            global_block["stats"] = dict(chest.stats())
            lessons = []
            for entity in chest.all_entities(["style"]):
                rec = chest.get("style", entity)
                if rec is not None:
                    lessons.append(
                        {
                            "entity": rec.entity,
                            "content": rec.content,
                            "version": rec.version,
                            "last_updated": rec.last_updated,
                        }
                    )
            global_block["lessons"] = lessons
        finally:
            _close(chest)

    # 技术层：独立库，存跨项目的工程经验（Unity / C# / 反编译等），
    # 与文学经验分开，避免检索时互相污染。
    tech_path = os.path.join(mem.home, "tech.db")
    tech_block: dict[str, Any] = {"exists": os.path.isfile(tech_path), "path": tech_path}
    if tech_block["exists"]:
        from .chest import EnderChest

        try:
            tchest = EnderChest(tech_path)
            try:
                tech_block["stats"] = dict(tchest.stats())
                notes = []
                for entity in tchest.all_entities():
                    rec = tchest.get("style", entity)
                    if rec is None:
                        # 技术条目可能落在任意层，逐个层找
                        for layer in ("assertion", "style", "world", "character", "item"):
                            rec = tchest.get(layer, entity)
                            if rec is not None:
                                break
                    if rec is not None:
                        notes.append(
                            {
                                "entity": rec.entity,
                                "layer": rec.layer,
                                "content": rec.content,
                                "version": rec.version,
                                "last_updated": rec.last_updated,
                            }
                        )
                tech_block["notes"] = notes
            finally:
                tchest.close()
        except Exception as exc:  # noqa: BLE001 - 技术库损坏不该让面板整体失败
            tech_block["error"] = str(exc)

    info["work_block"] = work_block
    info["global_block"] = global_block
    info["tech_block"] = tech_block
    # 面板开关的持久化状态：存在作品库 meta 里（没作品时退到全局库）。
    # 它只控制面板是否展示内容，**不影响工具与已存数据**。
    try:
        info["enabled"] = _panel_enabled(mem)
    except Exception:  # noqa: BLE001
        info["enabled"] = True
    return _ok(info)


def _panel_enabled(mem: Any) -> bool:
    """读取面板显示开关（默认开）。"""
    from .memory import SCOPE_GLOBAL, SCOPE_WORK

    scope = SCOPE_WORK if mem.work_id else SCOPE_GLOBAL
    path = mem.path_for(scope)
    if not os.path.isfile(path):
        return True
    chest = mem.open(scope)
    try:
        return chest.get_meta("panel_enabled", "1") != "0"
    finally:
        _close(chest)


def op_set_enabled(args: dict[str, Any]) -> dict[str, Any]:
    """持久化面板显示开关。

    注意：这只是「面板是否展示内容」的偏好，**不会停用工具，也不删数据**。
    想真正停用整个插件，应该改配置 ``enabled: false``。
    """
    from .memory import SCOPE_GLOBAL, SCOPE_WORK

    raw = args.get("enabled")
    if isinstance(raw, str):
        enabled = raw.strip().lower() not in ("0", "false", "no", "off")
    else:
        enabled = True if raw is None else bool(raw)

    mem = _memory(args)
    scope = SCOPE_WORK if mem.work_id else SCOPE_GLOBAL
    chest = mem.open(scope)
    try:
        chest.set_meta("panel_enabled", "1" if enabled else "0")
        saved = chest.get_meta("panel_enabled", "1") != "0"
    finally:
        _close(chest)

    return _ok(
        {
            "enabled": saved,
            "scope": scope,
            "note": "只影响面板显示；工具与记忆数据不受影响",
        }
    )


# --------------------------------------------------------------------------
# 各 op 实现
# --------------------------------------------------------------------------
def op_info(args: dict[str, Any]) -> dict[str, Any]:
    from . import install as I
    from .llm import BACKENDS

    rep = I.environment_report("auto")
    gguf = rep.get("llamacpp") or {}
    oll = rep.get("ollama") or {}
    return _ok(
        {
            "version": __import__("enderwriter").__version__,
            "package_root": _PKG_ROOT,
            "engine": rep.get("engine"),
            "backends": list(BACKENDS),
            "llamacpp": {
                "server": gguf.get("server_path"),
                "model": gguf.get("model_path"),
                "model_mb": gguf.get("model_mb"),
                "ready": gguf.get("ready"),
            },
            "ollama": {
                "installed": oll.get("cli_installed"),
                "server_up": oll.get("server_up"),
                "models": oll.get("models"),
            },
        }
    )


def op_search(args: dict[str, Any]) -> dict[str, Any]:
    from .common import SchemaError, est_tokens
    from .retrieval import MemoryKeeper

    entities = args.get("entities") or []
    if isinstance(entities, str):
        entities = [x.strip() for x in entities.replace("，", ",").split(",") if x.strip()]
    entities = [str(e).strip() for e in entities if str(e).strip()]
    if not entities:
        return _err("entities 不能为空：请至少给一个实体名（如 张三）", code="bad_request")

    req = {
        "entities": entities,
        "query_context": str(args.get("query_context") or ""),
        "budget": int(args.get("budget") or 800),
    }
    layers = args.get("layers")
    if isinstance(layers, str):
        layers = [x.strip() for x in layers.split(",") if x.strip()]
    if layers:
        req["layers"] = list(layers)

    chest, meta = _open_scoped(args)
    if chest is None:
        return _ok(
            {
                "result": {"high": [], "medium": [], "low": [], "total_tokens": 0},
                "counts": {"high": 0, "medium": 0, "low": 0},
                "total_tokens": 0,
                "note": "作品库尚未建立",
                "scope": meta,
            }
        )
    try:
        keeper = MemoryKeeper(chest)
        result = keeper.search(req)
    except SchemaError as exc:
        return _err(f"检索参数不符合 §3.2 Schema：{exc}", code="schema_error")
    finally:
        _close(chest)

    return _ok(
        {
            "result": result,
            "counts": {band: len(result.get(band) or []) for band in ("high", "medium", "low")},
            "total_tokens": result.get("total_tokens", 0),
            "scope": meta,
        }
    )


def op_patrol(args: dict[str, Any]) -> dict[str, Any]:
    from .writeback import patrol

    chapter = str(args.get("chapter") or "").strip()
    if not chapter:
        return _err("chapter 必填，形如 ch_042", code="bad_request")
    chest, meta = _open_scoped(args)
    if chest is None:
        return _ok({"chapter": chapter, "items": [], "count": 0, "open_foreshadowings": [],
                    "note": "作品库尚未建立", "scope": meta})
    try:
        items = patrol(
            chest,
            chapter,
            grace=int(args.get("grace") or 3),
            stale_threshold=int(args.get("stale_threshold") or 30),
        )
        open_fs = [
            {"id": f.id, "status": f.status, "planted": f.planted_chapter,
             "planned_resolve": f.planned_resolve}
            for f in chest.all_foreshadowings(only_open=True)
        ]
    finally:
        _close(chest)
    return _ok({"chapter": chapter, "items": items, "count": len(items), "open_foreshadowings": open_fs})


def op_stats(args: dict[str, Any]) -> dict[str, Any]:
    chest, meta = _open_scoped(args)
    if chest is None:
        return _ok({"stats": {"records": {}, "total_records": 0, "history": 0, "terms": 0,
                              "edges": 0, "chapters": 0, "open_foreshadowings": 0},
                    "note": "作品库尚未建立", "scope": meta})
    try:
        st = dict(chest.stats())
        st["db"] = args.get("db") or ":memory:"
    finally:
        _close(chest)
    return _ok({"stats": st})


def op_writeback(args: dict[str, Any]) -> dict[str, Any]:
    from .common import SchemaError
    from .writeback import WriteBackAgent

    report = args.get("report")
    if not isinstance(report, dict):
        return _err("report 必填：应为 §3.5 回写报告对象", code="bad_request")
    chest = _open(args.get("db"), seed=False)
    try:
        agent = WriteBackAgent(chest)
        applied = agent.apply(report)
        warnings = agent.detect_contradictions(report, chapter_id=report.get("chapter_id"))
    except SchemaError as exc:
        return _err(f"回写报告不符合 §3.5 Schema：{exc}", code="schema_error")
    finally:
        _close(chest)
    return _ok({"applied": applied, "warnings": warnings})


def op_demo(args: dict[str, Any]) -> dict[str, Any]:
    """跑一次六阶段流水线，返回结构化摘要（不打印长正文）。"""
    from .llm import get_backend
    from .orchestrator import Orchestrator
    from .retrieval import MemoryKeeper
    from .writeback import WriteBackAgent, patrol

    window = int(args.get("window") or 8000)
    backend = get_backend(args.get("llm") or os.environ.get("EW_LLM"))
    chest = _open(args.get("db"), seed=True)
    try:
        from .llm import draft_request

        keeper = MemoryKeeper(chest)
        orch = Orchestrator(chest, window_tokens=window, keeper=keeper)

        # ① 规划
        req = draft_request(chest)
        outline = backend.plan(req)
        # ② 检索
        seeds = [c["name"] for c in (outline.get("active_characters") or [])]
        seeds += list(outline.get("locations") or [])
        retrieval = keeper.search(
            {"entities": seeds or ["主角"], "query_context": outline.get("chapter_goal") or "",
             "budget": 800}
        )
        # ②b 组装
        ctx = orch.assemble_context(outline, current_chapter=outline.get("chapter_id"))
        # ③ 生成
        text = backend.generate(ctx["text"], max_tokens=int(args.get("words") or 480))
        # ④ 动态追加
        pre = orch.prefetch(text)
        # ⑤ 回写
        report = backend.writeback(text, outline)
        applied = WriteBackAgent(chest).apply(report)
        # ⑥ 巡检
        items = patrol(chest, outline.get("chapter_id") or "ch_001")
        st = dict(chest.stats())
    finally:
        _close(chest)
        try:
            backend.close()
        except Exception:  # noqa: BLE001
            pass

    return _ok(
        {
            "chapter_id": outline.get("chapter_id"),
            "chapter_goal": outline.get("chapter_goal"),
            "outline": outline,
            "retrieval": {
                "counts": {b: len(retrieval.get(b) or []) for b in ("high", "medium", "low")},
                "total_tokens": retrieval.get("total_tokens", 0),
            },
            "context": {
                "budget": ctx.get("budget"),
                "total_tokens": ctx.get("total_tokens"),
                "dropped": ctx.get("dropped"),
            },
            "text": text,
            "text_chars": len(text),
            "prefetch": {"entities": pre.get("entities"), "prefetched": pre.get("prefetched")},
            "writeback": applied,
            "patrol": items,
            "stats": st,
        }
    )


def op_install(args: dict[str, Any]) -> dict[str, Any]:
    from . import install as I

    rep = I.environment_report(args.get("engine") or "auto")
    if args.get("check"):
        return _ok({"check": True, "report": rep})
    return _ok(
        {
            "check": False,
            "report": rep,
            "hint": "自动安装需要在终端执行：python -m enderwriter install",
        }
    )


def op_entries(args: dict[str, Any]) -> dict[str, Any]:
    """列出某个格子的全部条目（供 Web 面板浏览）。

    指定 ``work`` 时读该作品库；否则读 ``db``；都没有则用临时库。
    """
    from .common import LAYERS, clip_tokens

    layer = str(args.get("layer") or "character").strip()
    if layer not in LAYERS:
        return _err(f"未知格子：{layer!r}；可选 {list(LAYERS)}", code="bad_request")
    limit = int(args.get("limit") or 200)

    work = str(args.get("work") or "").strip()
    # 只有显式给了 db 才走单库模式；否则一律走分层记忆，
    # 由 work / session_id 解析出该读哪一本。
    # 之前这里的条件写成了 `work and not db`，面板不传 work 时会掉进
    # else 分支开一个临时库并自动灌种子数据 —— 于是"浏览条目"看到的是
    # 示例故事的人物，而不是本对话的记忆。
    if args.get("db"):
        chest = _open(args.get("db"), seed=bool(args.get("seed", True)))
    else:
        from .memory import SCOPE_WORK, route

        mem = _memory(args)
        scope = route(layer)
        if scope == SCOPE_WORK and not mem.exists(SCOPE_WORK):
            return _ok({"layer": layer, "count": 0, "items": [], "note": "作品库尚未建立"})
        chest = mem.open(scope)
    try:
        names = sorted(chest.all_entities([layer]))
        usage_map = chest.usage_map()
        items: list[dict[str, Any]] = []
        for entity in names[:limit]:
            rec = chest.get(layer, entity)
            if rec is None:
                continue
            full = str(rec.content or "")
            u = usage_map.get((layer, rec.entity)) or {}
            items.append(
                {
                    "layer": rec.layer,
                    "entity": rec.entity,
                    # 面板要能看全文：设定类条目常有 300-1000 字，
                    # 截断到 220 会把内容切碎。full=True 时不截。
                    "content": full if args.get("full") else clip_tokens(full, 2000),
                    "chars": len(full),
                    "version": rec.version,
                    "last_updated": rec.last_updated,
                    "chapter_id": rec.chapter_id,
                    "data": rec.data,
                    # 使用情况：被检索到过就算「已回收归档」，但内容永不删除
                    "usage": {
                        "hit_count": int(u.get("hit_count") or 0),
                        "last_hit_at": u.get("last_hit_at"),
                        "archived": bool(u.get("archived")),
                    },
                }
            )
        foreshadowings: list[dict[str, Any]] = []
        if layer == "foreshadowing":
            for f in chest.all_foreshadowings():
                foreshadowings.append(
                    {
                        "id": f.id,
                        "content": clip_tokens(str(f.content or ""), 200),
                        "status": f.status,
                        "planted_chapter": f.planted_chapter,
                        "planned_resolve": f.planned_resolve,
                        "reinforced_chapters": list(f.reinforced_chapters or []),
                        "resolved_chapter": f.resolved_chapter,
                        "is_open": f.is_open,
                    }
                )
    finally:
        _close(chest)

    out: dict[str, Any] = {"layer": layer, "count": len(items), "items": items}
    if foreshadowings:
        out["foreshadowings"] = foreshadowings
    return _ok(out)


def op_check(args: dict[str, Any]) -> dict[str, Any]:
    """一致性检查：用确定性规则核实正文与已确立设定的冲突。

    参数：text（必填）、entities（可选，限定检查范围）、rules（可选，如 ["R1","R2"]）
    返回：{ok, issues[], facts_used, rules_run[]}
    """
    from .review import CONFLICT_RULES, check_text, extract_facts

    text = str(args.get("text") or "")
    if not text.strip():
        return _err("text 必填：要检查的正文", code="bad_request")

    entities = args.get("entities") or []
    if isinstance(entities, str):
        entities = [x.strip() for x in entities.replace("，", ",").split(",") if x.strip()]

    rules = args.get("rules")
    if isinstance(rules, str):
        rules = [x.strip() for x in rules.split(",") if x.strip()]

    chest, meta = _open_scoped(args)
    if chest is None:
        # 库不存在 = 没有事实 = 检查没执行。
        # 注意：外层 ok 表示「调用是否成功」，这里调用是成功的；
        # 「是否真的检查了」由 executed 表达，避免两个语义挤在一个字段里。
        return _ok(
            {
                "passed": False,
                "executed": False,
                "issues": [],
                "facts_used": 0,
                "rules_run": [],
                "notice": "作品库尚未建立，没有可对照的设定事实，一致性检查无法执行",
                "scope": meta,
                "catalog": list(CONFLICT_RULES),
            }
        )
    try:
        facts = extract_facts(chest, entities=entities)
        result = check_text(text, facts, rules=rules)
    finally:
        _close(chest)

    out = result.to_dict()
    # CheckResult.to_dict() 自带 ok，会覆盖外层 ok —— 一旦查出冲突，
    # 外层 ok 变成 False，调用方就会把它当成"调用失败"，看不到冲突详情。
    # 所以这里剥掉 ok，改用 passed 表达「没发现问题」。
    out.pop("ok", None)
    out["passed"] = bool(result.ok)
    out["scope"] = meta
    out["catalog"] = list(CONFLICT_RULES)
    return _ok(out)


def op_guard(args: dict[str, Any]) -> dict[str, Any]:
    """技术护栏：拿一段代码/命令/配置，看命中了哪些断言。

    参数：
      text          要检查的代码或文本（可与 task 二选一）
      task          要做的任务描述，用于匹配「流程性」断言
                    （如 "回写 Assembly-CSharp 到 DLL"）
      min_severity  可选，high|medium|low，默认 low（全部）
      detailed      可选，是否输出修法与后果，默认 True

    返回：
      {hits[], total, rendered}
      命中数就是要留意的条数；它不判断"代码有没有错"，
      只提示"这里踩过坑"。
    """
    from .guard import guard as run_guard
    from .guard import list_assertions, render

    text = str(args.get("text") or "")
    task = str(args.get("task") or "")
    if not text.strip() and not task.strip():
        return _err("text 或 task 至少给一个", code="bad_request")

    min_sev = str(args.get("min_severity") or "low").strip()
    detailed = args.get("detailed")
    detailed = True if detailed is None else bool(detailed)

    chest, meta = _open_scoped(args, layer="assertion")
    if chest is None:
        return _ok(
            {
                "hits": [],
                "total": 0,
                "rendered": "【技术护栏】技术库尚未建立，没有可用的断言",
                "scope": meta,
            }
        )
    try:
        all_assertions = list_assertions(chest, min_severity=min_sev)
        hits = run_guard(chest, text, task=task, min_severity=min_sev)
        # 误报反馈：把历史上被判过"误报"的断言标出来，
        # 提醒使用者「这条可能不准」，而不是当成硬规则照做。
        fb = chest.feedback_stats("assertion")
    finally:
        _close(chest)

    hit_dicts = []
    for hh in hits:
        d = hh.to_dict()
        st = fb.get(f"assertion:{hh.assertion.id}") or {}
        if st.get("total"):
            d["fp"] = st.get("fp", 0)
            d["tp"] = st.get("tp", 0)
            d["fp_rate"] = st.get("fp_rate", 0.0)
            d["unreliable"] = st.get("total", 0) >= 2 and st.get("fp_rate", 0) >= 0.5
        hit_dicts.append(d)

    rendered = render(hits, total=len(all_assertions), detailed=detailed)
    bad = [d for d in hit_dicts if d.get("unreliable")]
    if bad:
        rendered += (
            "\n\n⚠ 其中 "
            + "、".join(d["id"] for d in bad)
            + " 历史上多次被判为误报，命中仅供参考。"
        )

    return _ok(
        {
            "hits": hit_dicts,
            "total": len(all_assertions),
            "rendered": rendered,
            "scope": meta,
        }
    )


def op_assert(args: dict[str, Any]) -> dict[str, Any]:
    """写入/更新一条技术断言。

    参数：
      id            必填，断言标识（短横线命名，如 unity-operator-eq-null）
      rule          必填，一句话规则（祈使句）
      detect_all    必填，匹配条件数组（至少 2 个，且不能太通用）
      detect_not    可选，出现这些说明已正确，不报警
      fix           可选，具体修法
      consequence   可选，违反后果
      severity      可选，high|medium|low
      category      可选，性能/兼容/构建/反编译/数据/其他
      cases         可选，关联的案例 ID

    不合法会直接拒绝——会持续误报的断言比没有断言更糟。
    """
    from .guard import AssertionError_, save

    aid = str(args.get("id") or "").strip()
    rule = str(args.get("rule") or "").strip()
    if not aid:
        return _err("id 必填：断言标识", code="bad_request")
    if not rule:
        return _err("rule 必填：一句话规则", code="bad_request")

    detect_all = args.get("detect_all") or []
    if isinstance(detect_all, str):
        detect_all = [detect_all]
    detect_not = args.get("detect_not") or []
    if isinstance(detect_not, str):
        detect_not = [detect_not]
    when = args.get("when") or []
    if isinstance(when, str):
        when = [when]

    chest, meta = _open_scoped(args, layer="assertion")
    if chest is None:
        return _err("技术库不可用", code="not_found")
    try:
        a = save(
            chest,
            aid,
            rule,
            detect_all=detect_all,
            detect_not=detect_not,
            when=when,
            fix=str(args.get("fix") or ""),
            consequence=str(args.get("consequence") or ""),
            severity=str(args.get("severity") or "medium"),
            category=str(args.get("category") or "其他"),
            cases=args.get("cases") or (),
            allow_single=bool(args.get("allow_single")),
        )
    except AssertionError_ as exc:
        return _err(f"断言被拒绝：{exc}", code="invalid_assertion")
    finally:
        _close(chest)

    return _ok({"assertion": a.to_dict(), "scope": meta})


def op_seed_assertions(args: dict[str, Any]) -> dict[str, Any]:
    """把内置断言写入技术库（幂等）。

    参数：overwrite（可选，true 时覆盖已存在的断言）

    内置断言来自真实事故复盘，见 :mod:`enderwriter.assertions_seed`。
    """
    from .assertions_seed import BUILTIN_ASSERTIONS, seed_builtin

    chest, meta = _open_scoped(args, layer="assertion")
    if chest is None:
        return _err("技术库不可用", code="not_found")
    try:
        result = seed_builtin(chest, overwrite=bool(args.get("overwrite")))
        total = len(chest.all_entities(["assertion"]))
    finally:
        _close(chest)

    return _ok(
        {
            **result,
            "defined": len(BUILTIN_ASSERTIONS),
            "total_in_library": total,
            "scope": meta,
        }
    )


def op_mark_superseded(args: dict[str, Any]) -> dict[str, Any]:
    """把一条记录标记为「已废止」或取消该标记。

    参数：
      layer       图层
      entity      实体名
      superseded  可选，默认 True；传 false 取消标记
      note        可选，废止说明（如「已被数值牌级制取代」）

    废止后该记录**不再参与检索**（避免新旧口径同时出现），
    但仍在库里可查、可恢复。
    """
    layer = str(args.get("layer") or "").strip()
    entity = str(args.get("entity") or "").strip()
    if not layer or not entity:
        return _err("layer 与 entity 都必填", code="bad_request")

    flag = args.get("superseded")
    flag = True if flag is None else bool(flag)
    note = str(args.get("note") or "").strip()

    from .memory import SCOPE_TECH, route

    scope = route(layer)
    mem = _memory(args)
    if scope not in (SCOPE_TECH, "global") and not mem.work_id:
        return _err("作品库未指定", code="bad_request")

    chest = mem.open(scope)
    try:
        rec = chest.get(layer, entity)
        if rec is None:
            return _ok({"changed": False, "reason": "not_found", "layer": layer, "entity": entity})
        data = dict(rec.data or {})
        if flag:
            data["superseded"] = True
            if note:
                data["superseded_note"] = note
        else:
            data.pop("superseded", None)
            data.pop("superseded_note", None)
        chest.upsert(layer, entity, rec.content, data=data, chapter_id=rec.chapter_id)
    finally:
        _close(chest)

    return _ok(
        {
            "changed": True,
            "layer": layer,
            "entity": entity,
            "superseded": flag,
            "note": note,
        }
    )


def op_usage(args: dict[str, Any]) -> dict[str, Any]:
    """使用概况：多少条已回收归档、多少条从未被检索过。

    「归档」= 曾被检索进写作上下文。**不等于删除**——
    末影箱在文学使用时兼当设定集，任何设定都要能随时查回来。

    参数：
      layer  可选，只看某一格
      work   可选，作品名
    """
    chest, meta = _open_scoped(args, layer=str(args.get("layer") or "") or None)
    if chest is None:
        return _ok(
            {
                "summary": {"total": 0, "archived": 0, "used": 0, "unused": 0},
                "unused": [],
                "scope": meta,
            }
        )
    try:
        summary = chest.usage_summary()
        usage = chest.usage_map()
        layer_filter = str(args.get("layer") or "").strip()
        unused: list[dict[str, Any]] = []
        for layer in (["character", "world", "timeline", "foreshadowing", "item", "style", "assertion"]):
            if layer_filter and layer != layer_filter:
                continue
            for entity in chest.all_entities([layer]):
                if (layer, entity) in usage:
                    continue
                unused.append({"layer": layer, "entity": entity})
    finally:
        _close(chest)

    return _ok({"summary": summary, "unused": unused[:200], "scope": meta})


def op_archive(args: dict[str, Any]) -> dict[str, Any]:
    """手动标记/取消一条记忆的「已回收归档」。

    归档只影响面板归类，**不影响检索**，也不删除任何内容。

    参数：layer、entity、archived（默认 true）、note
    """
    layer = str(args.get("layer") or "").strip()
    entity = str(args.get("entity") or "").strip()
    if not layer or not entity:
        return _err("layer 与 entity 都必填", code="bad_request")

    flag = args.get("archived")
    flag = True if flag is None else bool(flag)

    chest, meta = _open_scoped(args, layer=layer)
    if chest is None:
        return _err("库不存在", code="not_found")
    try:
        rec = chest.get(layer, entity)
        if rec is None:
            return _ok({"changed": False, "reason": "not_found", "layer": layer, "entity": entity})
        ok = chest.set_archived(layer, entity, flag, note=str(args.get("note") or ""))
        usage = chest.usage_of(layer, entity) or {}
    finally:
        _close(chest)

    return _ok(
        {
            "changed": bool(ok),
            "layer": layer,
            "entity": entity,
            "archived": flag,
            "usage": usage,
            "scope": meta,
        }
    )


def op_history(args: dict[str, Any]) -> dict[str, Any]:
    """列出某条记忆的所有历史版本。

    每次覆写都会追加一个版本，旧内容永不丢失——
    这样 AI 可以放心地覆写设定，人随时能回看改了什么。

    参数：layer、entity、work（可选）
    """
    layer = str(args.get("layer") or "").strip()
    entity = str(args.get("entity") or "").strip()
    if not layer or not entity:
        return _err("layer 与 entity 都必填", code="bad_request")

    chest, meta = _open_scoped(args, layer=layer)
    if chest is None:
        return _err("库不存在", code="not_found")
    try:
        rec = chest.get(layer, entity)
        resolved = rec.entity if rec else entity
        rows = chest.conn.execute(
            "SELECT version, content, chapter_id, updated_at, change "
            "FROM history WHERE layer=? AND entity=? ORDER BY version DESC",
            (layer, resolved),
        ).fetchall()
        current = int(rec.version) if rec else 0
    finally:
        _close(chest)

    versions = [
        {
            "version": int(r["version"]),
            "chars": len(str(r["content"] or "")),
            "content": str(r["content"] or ""),
            "chapter_id": r["chapter_id"],
            "updated_at": r["updated_at"],
            "change": r["change"] or "",
            "current": int(r["version"]) == current,
        }
        for r in rows
    ]
    return _ok(
        {
            "layer": layer,
            "entity": resolved,
            "current_version": current,
            "count": len(versions),
            "versions": versions,
            "scope": meta,
        }
    )


def op_revert(args: dict[str, Any]) -> dict[str, Any]:
    """把某条记忆回滚到指定历史版本。

    实现方式：把旧内容**作为新版本写回**（version + 1），
    而不是删掉后面的版本。所以「回滚」本身也可再回滚，
    完整历史链始终保留——这符合「文字本身不改」的约束。

    参数：layer、entity、version（目标版本号）、work（可选）
    """
    layer = str(args.get("layer") or "").strip()
    entity = str(args.get("entity") or "").strip()
    if not layer or not entity:
        return _err("layer 与 entity 都必填", code="bad_request")
    try:
        want = int(args.get("version"))
    except (TypeError, ValueError):
        return _err("version 必填，且必须是整数", code="bad_request")

    chest, meta = _open_scoped(args, layer=layer)
    if chest is None:
        return _err("库不存在", code="not_found")
    try:
        rec = chest.get(layer, entity)
        if rec is None:
            return _err(f"没有这条记忆：{entity}", code="not_found")
        row = chest.conn.execute(
            "SELECT content, data, chapter_id FROM history WHERE layer=? AND entity=? AND version=?",
            (layer, rec.entity, want),
        ).fetchone()
        if row is None:
            return _err(f"没有 v{want} 这个版本", code="bad_request")

        from .common import loads as _loads

        data = _loads(row["data"], {}) or {}
        new_rec = chest.upsert(
            layer,
            rec.entity,
            str(row["content"] or ""),
            data=data,
            chapter_id=row["chapter_id"],
            change=f"revert to v{want}",
        )
        new_version = int(new_rec.version)
    finally:
        _close(chest)

    return _ok(
        {
            "layer": layer,
            "entity": rec.entity,
            "reverted_to": want,
            "new_version": new_version,
            "note": f"已把 v{want} 的内容写回为 v{new_version}（旧版本全部保留）",
            "scope": meta,
        }
    )


def op_build_edges(args: dict[str, Any]) -> dict[str, Any]:
    """从正文自动抽取实体关系（edges）。

    只写**强边**：被提到 ≥min_weight 次，或出现在正文开头（主题级）。
    弱边一律丢弃——多跳检索会被噪声稀释，比不建边更差。

    参数：
      min_weight     可选，默认 2
      max_per_src    可选，每个实体最多保留多少条出边，默认 12
      work           可选，作品名
    """
    from .relations import build_edges, extract_candidates, render

    chest, meta = _open_scoped(args)
    if chest is None:
        return _err("作品库尚未建立", code="not_found")
    try:
        candidates = extract_candidates(chest)
        stats = build_edges(
            chest,
            min_weight=int(args.get("min_weight") or 2),
            max_per_src=int(args.get("max_per_src") or 12),
        )
    finally:
        _close(chest)

    return _ok(
        {
            **stats,
            "rendered": render(stats, sample=[c for c in candidates if c.weight >= 2][:8]),
            "sample": [c.to_dict() for c in candidates[:20]],
            "scope": meta,
        }
    )


def op_edges(args: dict[str, Any]) -> dict[str, Any]:
    """查看某条记忆的关系（出边 / 入边）。"""
    from .relations import REL_CN, edge_stats

    entity = str(args.get("entity") or "").strip()

    chest, meta = _open_scoped(args)
    if chest is None:
        return _ok({"stats": {"total": 0, "by_rel": {}}, "out": [], "in": [], "scope": meta})
    try:
        stats = edge_stats(chest)
        out_edges: list[dict[str, Any]] = []
        in_edges: list[dict[str, Any]] = []
        if entity:
            from .common import loads as _loads

            rows = chest.conn.execute(
                "SELECT * FROM edges WHERE src=? OR dst=?", (entity, entity)
            ).fetchall()
            for r in rows:
                d = _loads(r["data"], {}) or {}
                item = {
                    "src": r["src"],
                    "rel": r["rel"],
                    "rel_cn": REL_CN.get(str(r["rel"]), str(r["rel"])),
                    "dst": r["dst"],
                    "weight": d.get("weight"),
                }
                (out_edges if r["src"] == entity else in_edges).append(item)
            out_edges.sort(key=lambda x: -(x.get("weight") or 0))
            in_edges.sort(key=lambda x: -(x.get("weight") or 0))
    finally:
        _close(chest)

    return _ok(
        {
            "stats": stats,
            "entity": entity,
            "out": out_edges,
            "in": in_edges,
            "scope": meta,
        }
    )


def op_organize(args: dict[str, Any]) -> dict[str, Any]:
    """整理分析：指出重复候选、孤立条目、已废止、从未使用。

    **不改任何内容。** 合并必须由人确认；即便合并也走版本号，
    旧版全部保留可回看。

    参数：work（可选）
    """
    from .export import organize, render_organize

    chest, meta = _open_scoped(args)
    if chest is None:
        return _err("作品库尚未建立", code="not_found")
    try:
        rep = organize(chest)
    finally:
        _close(chest)

    return _ok({**rep, "rendered": render_organize(rep), "scope": meta})


def op_export(args: dict[str, Any]) -> dict[str, Any]:
    """导出为结构化 Markdown 设定集。

    组织方式：按图层分卷，卷内按 ``part_of`` 关系做两级嵌套；
    原文一字不改，已废止的条目保留并加标记。

    参数：
      title               可选，文档标题
      include_superseded  可选，是否收录已废止条目，默认 true
      include_history     可选，是否附历史版本，默认 false
      include_relations   可选，是否附关联，默认 true
      include_unused      可选，是否附「尚未被引用」，默认 true
      save                可选，写到这个路径（不给则只返回文本）
    """
    from .export import export_markdown

    chest, meta = _open_scoped(args)
    if chest is None:
        return _err("作品库尚未建立", code="not_found")
    try:
        md = export_markdown(
            chest,
            title=str(args.get("title") or "设定集"),
            include_superseded=args.get("include_superseded") is not False,
            include_history=bool(args.get("include_history")),
            include_relations=args.get("include_relations") is not False,
            include_unused=args.get("include_unused") is not False,
        )
    finally:
        _close(chest)

    saved = ""
    path = str(args.get("save") or "").strip()
    if path:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(md)
            saved = os.path.abspath(path)
        except OSError as exc:
            return _err(f"写入失败：{exc}", code="io_error")

    return _ok(
        {
            "markdown": md,
            "chars": len(md),
            "saved": saved,
            "scope": meta,
        }
    )


def op_chapter(args: dict[str, Any]) -> dict[str, Any]:
    """记录一章正文，并把它设为「当前章节」。

    这是让末影箱知道「你写到哪了」的入口。记下来之后：

      * ``recall`` 传 ``at_chapter`` 就只看这一章之前成立的设定
      * 伏笔巡检能算出超期
      * 写回能挂到具体章节上

    参数：
      chapter_id  必填，如 ch_042
      text        可选，正文
      title       可选，章节标题
      summary     可选，本章摘要
      work        作品名
    """
    from .common import chapter_index

    cid = str(args.get("chapter_id") or "").strip()
    if not cid:
        return _err("chapter_id 必填，如 ch_042", code="bad_request")
    if chapter_index(cid) < 0:
        return _err(f"无法解析章节号：{cid!r}（应形如 ch_042）", code="bad_request")

    # 记录章节通常是这个作品库的第一个动作（还没写过任何设定），
    # 所以这里不能用 _open_scoped —— 它在库不存在时会拒绝打开。
    mem = _memory(args)
    if not mem.work_id:
        return _err("需要指定 work 或 session_id", code="bad_request")
    chest = mem.open("work")
    try:
        chest.put_chapter(
            cid,
            str(args.get("text") or ""),
            title=str(args.get("title") or ""),
            summary=str(args.get("summary") or ""),
            commit=False,
        )
        # 只在章节号更大时推进"当前章节"，避免回看旧章把进度退回去
        prev = chest.current_chapter
        advanced = False
        if not prev or chapter_index(cid) >= chapter_index(prev):
            chest.set_meta("current_chapter", cid, commit=False)
            advanced = True
        chest.conn.commit()
        total = len(list(chest.iter_chapters()))
        cur = chest.current_chapter
    finally:
        _close(chest)

    return _ok(
        {
            "chapter_id": cid,
            "current_chapter": cur,
            "advanced": advanced,
            "chapters_total": total,
            "scope": {
                "mode": "scoped",
                "scope": "work",
                "path": mem.work_path,
                "work": mem.work_name,
                "exists": True,
            },
        }
    )


def op_timeline(args: dict[str, Any]) -> dict[str, Any]:
    """给一条记忆标注生效章节（``valid_from`` / ``valid_until``）。

    长篇里同一条设定会随剧情变化：
      「白杨不会变身」在第 1 章成立，第 50 章之后就不是了。
    标注了区间，检索时按 ``at_chapter`` 就能拿到**当时正确**的版本。

    参数：layer、entity、valid_from、valid_until（都可留空表示不限）
    """
    layer = str(args.get("layer") or "").strip()
    entity = str(args.get("entity") or "").strip()
    if not layer or not entity:
        return _err("layer 与 entity 都必填", code="bad_request")

    chest, meta = _open_scoped(args, layer=layer)
    if chest is None:
        return _err("库不存在", code="not_found")
    try:
        rec = chest.get(layer, entity)
        if rec is None:
            return _err(f"没有这条记忆：{entity}", code="not_found")
        data = dict(rec.data or {})
        for key, arg in (("valid_from", "valid_from"), ("valid_until", "valid_until")):
            val = str(args.get(arg) or "").strip()
            if val:
                data[key] = val
            else:
                data.pop(key, None)
        chest.upsert(layer, rec.entity, rec.content, data=data, chapter_id=rec.chapter_id)
        version = 0
        got = chest.get(layer, rec.entity)
        version = got.version if got else 0
    finally:
        _close(chest)

    return _ok(
        {
            "layer": layer,
            "entity": entity,
            "valid_from": data.get("valid_from"),
            "valid_until": data.get("valid_until"),
            "version": version,
            "scope": meta,
        }
    )


def op_feedback(args: dict[str, Any]) -> dict[str, Any]:
    """记录一次命中判定的对错，用于让规则/断言变准。

    参数：
      kind     assertion（技术断言）| rule（R1..R5 一致性规则）
      ref      断言 id 或规则 id
      verdict  fp=误报，tp=确实有问题
      note     可选，说明为什么算误报

    反馈会累积成误报率：fp_rate 高的断言在输出里会被标出来，
    提醒「这条可能不准，别当硬规则用」。
    """
    kind = str(args.get("kind") or "").strip()
    ref = str(args.get("ref") or "").strip()
    verdict = str(args.get("verdict") or "").strip().lower()
    if kind not in ("assertion", "rule"):
        return _err("kind 只能是 assertion / rule", code="bad_request")
    if not ref:
        return _err("ref 必填：断言 id 或规则 id", code="bad_request")
    if verdict not in ("fp", "tp"):
        return _err("verdict 只能是 fp（误报）/ tp（真问题）", code="bad_request")

    # assertion 在技术库，rule 与作品无关，统一用技术库承载反馈
    from .memory import SCOPE_TECH

    mem = _memory(args)
    chest = mem.open(SCOPE_TECH)
    try:
        chest.add_feedback(kind, ref, verdict, note=str(args.get("note") or ""))
        stats = chest.feedback_stats(kind, ref)
    finally:
        _close(chest)

    key = f"{kind}:{ref}"
    return _ok({"recorded": True, "kind": kind, "ref": ref, "verdict": verdict, "stats": stats.get(key, {})})


def op_feedback_stats(args: dict[str, Any]) -> dict[str, Any]:
    """查看所有规则/断言的误报率。"""
    from .memory import SCOPE_TECH

    mem = _memory(args)
    chest = mem.open(SCOPE_TECH)
    try:
        stats = chest.feedback_stats(
            str(args.get("kind") or ""), str(args.get("ref") or "")
        )
    finally:
        _close(chest)

    flagged = {k: v for k, v in stats.items() if v.get("fp_rate", 0) >= 0.5 and v.get("total", 0) >= 2}
    return _ok({"stats": stats, "unreliable": flagged, "count": len(stats)})


def op_update(args: dict[str, Any]) -> dict[str, Any]:
    """直接修改一条记忆的正文（生成新版本，旧版全部保留）。

    与 ``remember`` 的区别：这个接口用于「编辑已有条目」，
    会保留原有 data（如 valid_from / superseded 等标记）。

    参数：layer、entity、content（新正文）、note（可选，版本说明）
    """
    layer = str(args.get("layer") or "").strip()
    entity = str(args.get("entity") or "").strip()
    content = args.get("content")
    if not layer or not entity:
        return _err("layer 与 entity 都必填", code="bad_request")
    if content is None:
        return _err("content 必填", code="bad_request")
    content = str(content)

    chest, meta = _open_scoped(args, layer=layer)
    if chest is None:
        return _err("库不存在", code="not_found")
    try:
        rec = chest.get(layer, entity)
        if rec is None:
            return _err(f"没有这条记忆：{entity}", code="not_found")
        note = str(args.get("note") or "edit")
        new_rec = chest.upsert(
            layer,
            rec.entity,
            content,
            data=dict(rec.data or {}),
            chapter_id=rec.chapter_id,
            change=note,
        )
        version = int(new_rec.version)
    finally:
        _close(chest)

    return _ok(
        {
            "layer": layer,
            "entity": entity,
            "version": version,
            "chars": len(content),
            "note": "已保存为新版本，旧版本可在历史里回看/回滚",
            "scope": meta,
        }
    )


def op_import(args: dict[str, Any]) -> dict[str, Any]:
    """从既有文档导入设定（反向建库）。

    **默认只预演**（``dry_run=true``），返回完整清单供人确认；
    必须显式传 ``dry_run=false`` 才真正写入。

    参数：
      path        必填，文档路径（.txt / .md）
      dry_run     可选，默认 true
      layer       可选，兜底图层，默认 world
      skip        可选，标题包含这些子串的分节跳过
      overrides   可选，{标题子串: 图层} 强制指定
      chapter_id  可选，导入的条目挂到哪一章
    """
    from .importer import plan_import, render_plan, run_import

    path = str(args.get("path") or "").strip()
    if not path:
        return _err("path 必填：文档路径", code="bad_request")
    if not os.path.isfile(path):
        return _err(f"文件不存在：{path}", code="not_found")
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError as exc:
        return _err(f"读取失败：{exc}", code="io_error")

    skip = args.get("skip") or []
    if isinstance(skip, str):
        skip = [s.strip() for s in skip.replace("，", ",").split(",") if s.strip()]
    overrides = args.get("overrides") if isinstance(args.get("overrides"), dict) else {}

    plan = plan_import(
        text,
        default_layer=str(args.get("layer") or "world"),
        overrides=overrides,
        skip=skip,
    )
    rendered = render_plan(plan)

    dry = args.get("dry_run")
    dry = True if dry is None else bool(dry)
    if dry:
        return _ok(
            {
                "dry_run": True,
                "sections": plan["sections"],
                "to_import": plan["to_import"],
                "by_layer": plan["by_layer"],
                "items": [
                    {"entity": i["entity"], "layer": i["layer"], "chars": i["chars"]}
                    for i in plan["items"]
                ],
                "skipped": plan["skipped"],
                "rendered": rendered,
            }
        )

    mem = _memory(args)
    if not mem.work_id:
        return _err("需要指定 work 或 session_id", code="bad_request")
    chest = mem.open("work")
    try:
        result = run_import(
            chest,
            plan,
            chapter_id=str(args.get("chapter_id") or "") or None,
            keyword=os.path.basename(path),
        )
        total = chest.stats()["total_records"]
    finally:
        _close(chest)

    return _ok(
        {
            "dry_run": False,
            "imported": len(result["written"]),
            "failed": result["failed"],
            "total_in_scope": total,
            "rendered": f"【导入完成】写入 {len(result['written'])} 条，库内共 {total} 条。\n\n" + rendered,
        }
    )


_OPS = {
    "info": op_info,
    "search": op_search,
    "patrol": op_patrol,
    "stats": op_stats,
    "writeback": op_writeback,
    "demo": op_demo,
    "install": op_install,
    "entries": op_entries,
    # 分层记忆
    "remember": op_remember,
    "recall": op_recall,
    "works": op_works,
    "overview": op_overview,
    "clear": op_clear,
    "forgetEntry": op_forget_entry,
    # 一致性检查
    "check": op_check,
    # 技术护栏
    "guard": op_guard,
    "assert": op_assert,
    "seedAssertions": op_seed_assertions,
    # 设定集：废止标记
    "markSuperseded": op_mark_superseded,
    # 使用记录：回收归档（只标记，不删除）
    "usage": op_usage,
    "archive": op_archive,
    # 版本：AI 可覆写，人可回看/回滚
    "history": op_history,
    "revert": op_revert,
    # 关系：自动抽取强边
    "buildEdges": op_build_edges,
    "edges": op_edges,
    # 整理与导出
    "organize": op_organize,
    "export": op_export,
    # 时间轴：记录章节 + 设定生效区间
    "chapter": op_chapter,
    "timeline": op_timeline,
    # 反馈：让规则/断言变准
    "feedback": op_feedback,
    "feedbackStats": op_feedback_stats,
    # 编辑：改已有条目（生成新版本）
    "update": op_update,
    # 导入：从既有文档反向建库
    "import": op_import,
    # 面板开关
    "setEnabled": op_set_enabled,
}


def handle(args: dict[str, Any]) -> dict[str, Any]:
    """分发一次请求。永不抛异常：错误一律转成 {ok:false,...}。"""
    raw = str(args.get("op") or "").strip()
    # op 名大小写不敏感：forgetEntry / forgetentry 都能命中
    fn = _OPS.get(raw) or _OPS.get(raw.lower())
    if fn is None:
        # 再按小写比较一遍所有注册项，兼容 camelCase 注册名
        lowered = {k.lower(): v for k, v in _OPS.items()}
        fn = lowered.get(raw.lower())
    if fn is None:
        return _err(f"未知 op：{raw!r}；可选 {sorted(_OPS)}", code="unknown_op")
    try:
        return fn(args)
    except FileNotFoundError as exc:
        return _err(f"路径不存在：{exc}", code="not_found")
    except KeyError as exc:
        return _err(f"找不到对象：{exc}", code="not_found")
    except ValueError as exc:
        return _err(f"参数不合法：{exc}", code="bad_request")
    except Exception as exc:  # noqa: BLE001 - 桥必须兜底
        return _err(
            f"{type(exc).__name__}: {exc}",
            code="internal_error",
            detail=traceback.format_exc(limit=6),
        )


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    raw = ""
    if "--stdin" in argv:
        raw = sys.stdin.read()
    elif argv and not argv[0].startswith("-"):
        raw = argv[0]
    else:
        # --op xxx --key value 形式
        args: dict[str, Any] = {}
        it = iter(argv)
        for tok in it:
            if tok == "--db":
                args["db"] = next(it, "")
            elif tok.startswith("--"):
                key = tok[2:].replace("-", "_")
                val = next(it, "")
                if val in ("true", "1"):
                    args[key] = True
                elif val in ("false", "0"):
                    args[key] = False
                else:
                    args[key] = val
        raw = json.dumps(args, ensure_ascii=False)

    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as exc:
        result = _err(f"请求不是合法 JSON：{exc}")
        _emit(result)
        return 2

    result = handle(payload if isinstance(payload, dict) else {})
    _emit(result)
    return 0 if result.get("ok") else 1


def _emit(obj: dict[str, Any]) -> None:
    """只往 stdout 写一个 JSON，其余全部走 stderr。"""
    buf = io.StringIO()
    json.dump(obj, buf, ensure_ascii=False, default=str)
    sys.stdout.write(buf.getvalue())
    sys.stdout.flush()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
