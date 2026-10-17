"""JSON 桥：给外部进程（如 DSH 插件）提供一条稳定的命令行 RPC 入口。

设计目标：**一次调用一个 JSON，绝不往 stdout 混非 JSON**。
所有日志走 stderr，stdout 只输出一个 JSON 对象，方便 Node/Python 任何一侧解析。

用法::

    python -m enderwriter.bridge '{"op":"search","entities":["沈砚"],"query":"月蚀"}'
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
        rec = chest.upsert(
            layer or "style",
            entity,
            content,
            data=args.get("data") if isinstance(args.get("data"), dict) else None,
            chapter_id=args.get("chapter_id") or None,
            keywords=keywords or None,
        )
        total = chest.stats()["total_records"]
    finally:
        _close(chest)

    return _ok(
        {
            "scope": scope,
            "work": mem.work_name,
            "path": mem.path_for(scope),
            "record": {
                "layer": rec.layer,
                "entity": rec.entity,
                "version": rec.version,
                "content": rec.content,
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
            results[scope] = keeper.search(req)
        except SchemaError as exc:
            errors.append(f"{scope}: {exc}")
        finally:
            _close(chest)

    merged_tokens = sum(int(r.get("total_tokens") or 0) for r in results.values() if isinstance(r, dict))
    return _ok(
        {
            "work": mem.work_name,
            "scopes": results,
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
                        for layer in ("style", "world", "character", "item"):
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
    return _ok(info)


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
        return _err("entities 不能为空：请至少给一个实体名（如 沈砚）", code="bad_request")

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
        items: list[dict[str, Any]] = []
        for entity in names[:limit]:
            rec = chest.get(layer, entity)
            if rec is None:
                continue
            full = str(rec.content or "")
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
        return _ok(
            {
                "ok": True,
                "issues": [],
                "facts_used": 0,
                "rules_run": [],
                "note": "作品库尚未建立，无可对照的设定",
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
    out["scope"] = meta
    out["catalog"] = list(CONFLICT_RULES)
    return _ok(out)


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
    # 一致性检查
    "check": op_check,
}


def handle(args: dict[str, Any]) -> dict[str, Any]:
    """分发一次请求。永不抛异常：错误一律转成 {ok:false,...}。"""
    op = str(args.get("op") or "").strip().lower()
    fn = _OPS.get(op)
    if fn is None:
        return _err(f"未知 op：{op!r}；可选 {sorted(_OPS)}", code="unknown_op")
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
