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


def _close(chest) -> None:
    try:
        chest.close()
    except Exception:  # noqa: BLE001 - 关闭失败不影响结果
        pass


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

    chest = _open(args.get("db"), seed=bool(args.get("seed", True)))
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
        }
    )


def op_patrol(args: dict[str, Any]) -> dict[str, Any]:
    from .writeback import patrol

    chapter = str(args.get("chapter") or "").strip()
    if not chapter:
        return _err("chapter 必填，形如 ch_042", code="bad_request")
    chest = _open(args.get("db"), seed=bool(args.get("seed", True)))
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
    chest = _open(args.get("db"), seed=bool(args.get("seed", True)))
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
    """列出某个格子的全部条目（供 Web 面板浏览）。"""
    from .common import LAYERS, clip_tokens

    layer = str(args.get("layer") or "character").strip()
    if layer not in LAYERS:
        return _err(f"未知格子：{layer!r}；可选 {list(LAYERS)}", code="bad_request")
    limit = int(args.get("limit") or 200)

    chest = _open(args.get("db"), seed=bool(args.get("seed", True)))
    try:
        names = sorted(chest.all_entities([layer]))
        items: list[dict[str, Any]] = []
        for entity in names[:limit]:
            rec = chest.get(layer, entity)
            if rec is None:
                continue
            items.append(
                {
                    "layer": rec.layer,
                    "entity": rec.entity,
                    "content": clip_tokens(str(rec.content or ""), 220),
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


_OPS = {
    "info": op_info,
    "search": op_search,
    "patrol": op_patrol,
    "stats": op_stats,
    "writeback": op_writeback,
    "demo": op_demo,
    "install": op_install,
    "entries": op_entries,
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
