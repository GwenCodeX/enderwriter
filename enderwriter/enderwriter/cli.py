"""EnderWriter 命令行入口（``python -m enderwriter`` 等价于 ``python -m enderwriter.cli``）。

子命令::

    demo            跑完整 ①规划→②检索→③生成→④动态追加→⑤回写→⑥巡检 全流程
    query           直接调一次 ``search_memory`` 并打印结构化结果
    patrol          打印指定章节的伏笔巡检结果
    stats           打印末影箱统计
    export-schemas  导出 §3 四份 Schema 到目录

设计约定：
  * ``retrieval`` / ``orchestrator`` / ``writeback`` 由其它组件提供，且可能与 CLI 并行开发。
    所以本文件对它们一律用**函数内惰性 import**：模块缺失时打印
    「模块尚未就绪：xxx」并返回非零码，**绝不让 cli.py 自身导入失败**；
  * 所有面向用户的输出都是中文，全部走 stdout（警告走 stderr）；
  * 默认 ``--llm stub``，完全离线可跑。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from typing import Any, Callable, Sequence

from . import common as C
from .chest import EnderChest

__all__ = ["main", "build_parser"]

#: 惰性依赖：模块名 -> 中文说明（用于"模块尚未就绪"提示）
_LAZY_MODULES: dict[str, str] = {
    "retrieval": "记忆守护者（search_memory / MemoryKeeper）",
    "orchestrator": "编排层（assemble_context / Orchestrator）",
    "writeback": "回写代理（WriteBackAgent / patrol）",
}

#: 默认模型窗口（token）
DEFAULT_WINDOW = 8000

#: 退出码
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NOT_READY = 3


# ==========================================================================
# 惰性导入与输出工具
# ==========================================================================
class NotReady(Exception):
    """协作模块尚未落盘。"""

    def __init__(self, module: str, detail: str = "") -> None:
        self.module = module
        self.detail = detail
        super().__init__(f"模块尚未就绪：{LAZY_DETAIL(module)}{detail}")


def LAZY_DETAIL(module: str) -> str:  # noqa: N802 - 保持与异常消息一致
    cn = _LAZY_MODULES.get(module, module)
    return f"{cn}（enderwriter/{module}.py）"


def lazy_import(module: str) -> Any:
    """函数内惰性 import 协作模块；缺失/半成品时抛 ``NotReady``。"""
    import importlib

    try:
        return importlib.import_module(f".{module}", package=__package__ or "enderwriter")
    except ImportError as exc:
        raise NotReady(module, f"：{exc}") from None


def lazy_attr(module: str, attr: str) -> Any:
    """惰性取模块内的某个公开符号。"""
    mod = lazy_import(module)
    try:
        return getattr(mod, attr)
    except AttributeError:
        raise NotReady(module, f"：缺少符号 {attr}") from None


def say(text: str = "") -> None:
    print(text, flush=True)


def hr(title: str = "", width: int = 68, char: str = "─") -> None:
    if not title:
        say(char * width)
        return
    pad = max(1, width - len(title) - 3)
    say(f"{char * 2} {title} " + char * pad)


def section(no: str, title: str) -> None:
    say()
    hr(f"{no} {title}")


def kv(label: str, value: Any, *, indent: str = "  ") -> None:
    if str(value) == "":
        say(f"{indent}{label}")
    else:
        say(f"{indent}{label}：{value}")


def err(msg: str) -> None:
    sys.stderr.write(f"{msg}\n")
    sys.stderr.flush()


# ==========================================================================
# 参数
# ==========================================================================
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="enderwriter",
        description="EnderWriter —— 规划驱动的长篇小说写作框架（离线最小原型）",
        epilog="示例：python -m enderwriter demo --window 8000 --llm stub",
    )
    p.add_argument("--db", default=argparse.SUPPRESS,
                   help="sqlite 路径（默认 :memory:，退出即丢）")
    sub = p.add_subparsers(dest="cmd", metavar="子命令")

    # ---- demo ----
    pd = sub.add_parser("demo", help="跑完整六阶段流程")
    pd.add_argument("--db", default=None, help="sqlite 路径（默认 :memory:）")
    pd.add_argument("--llm", default=None, choices=["stub", "ollama", "llamacpp", "auto"],
                    help="大模型后端（默认读 EW_LLM，缺省 stub）")
    pd.add_argument("--model", default=None, help="模型名（ollama）/ GGUF 路径（llamacpp）")
    pd.add_argument("--window", type=int, default=DEFAULT_WINDOW, help=f"模型窗口 token 数（默认 {DEFAULT_WINDOW}）")
    pd.add_argument("--chapter", default=None, help="要写的章节号（默认末影箱下一章）")
    pd.add_argument("--focus", default="", help="本章焦点描述")
    pd.add_argument("--words", type=int, default=480, help="正文目标 token 数（默认 480）")
    pd.add_argument("--no-seed", action="store_true", help="不灌示例数据（库中已有数据时用）")
    pd.add_argument("--json", action="store_true", help="额外输出一份 JSON 摘要")

    # ---- query ----
    pq = sub.add_parser("query", help="检索记忆（search_memory）")
    pq.add_argument("--db", default=None, help="sqlite 路径（默认 :memory:）")
    pq.add_argument("-e", "--entities", required=True, help="实体，逗号分隔，如 张三,示例卷")
    pq.add_argument("-q", "--query", default="", help="检索语境（自然语言）")
    pq.add_argument("--budget", type=int, default=800, help="token 预算（默认 800，上限规范 800）")
    pq.add_argument("--context", default="", help="已在上下文中的实体（逗号分隔），不重复返回正文")
    pq.add_argument("--json", action="store_true", help="输出原始 JSON")

    # ---- patrol ----
    pp = sub.add_parser("patrol", help="伏笔巡检")
    pp.add_argument("--db", default=None, help="sqlite 路径（默认 :memory:）")
    pp.add_argument("--chapter", required=True, help="当前章节号，如 ch_003")
    pp.add_argument("--grace", type=int, default=3, help="超期宽限章数（默认 3）")
    pp.add_argument("--stale", type=int, default=30, help="陈旧阈值章数（默认 30）")
    pp.add_argument("--json", action="store_true", help="输出原始 JSON")

    # ---- stats ----
    ps = sub.add_parser("stats", help="末影箱统计")
    ps.add_argument("--db", default=None, help="sqlite 路径（默认 :memory:）")
    ps.add_argument("--json", action="store_true", help="输出原始 JSON")

    # ---- export-schemas ----
    pe = sub.add_parser("export-schemas", help="导出 §3 四份 Schema")
    pe.add_argument("--out", required=True, help="输出目录")

    # ---- install ----
    pi = sub.add_parser("install", help="自动安装本地 Qwen 模型环境")
    pi.add_argument(
        "--engine",
        choices=["auto", "ollama", "llamacpp"],
        default="auto",
        help="auto=自动挑选（默认）；ollama=官方路线；llamacpp=轻量路线",
    )
    pi.add_argument("--model", default=None, help="模型名（ollama）/ GGUF 文件名（llamacpp）")
    pi.add_argument("--check", action="store_true", help="只检测，不安装任何东西")
    pi.add_argument("--no-install", action="store_true", help="跳过运行时安装")
    pi.add_argument("--no-pull", action="store_true", help="跳过模型下载")
    pi.add_argument("--gpu", default=None, help="llama.cpp 加速后端：vulkan|cuda12|cpu")
    return p


# ==========================================================================
# 共用：打开末影箱
# ==========================================================================
def _open_chest(db: str | None, *, seed: bool = False) -> EnderChest:
    """打开末影箱。``db`` 为 None/``:memory:`` 时用临时库。"""
    path = db or ":memory:"
    chest = EnderChest(path)
    empty = chest.stats()["total_records"] == 0 and chest.stats()["chapters"] == 0
    if seed and empty:
        from .seed import seed_demo

        seed_demo(chest)
    return chest


def _split_csv(raw: str) -> list[str]:
    return [x.strip() for x in str(raw or "").replace("，", ",").split(",") if x.strip()]


# ==========================================================================
# demo：六阶段流水线
# ==========================================================================
def _render_outline(outline: dict, indent: str = "  ") -> None:
    kv("章节", outline.get("chapter_id"), indent=indent)
    kv("目标", outline.get("chapter_goal"), indent=indent)
    chars = ", ".join(
        f"{c.get('name')}({c.get('importance')})" for c in outline.get("active_characters") or []
    )
    kv("出场人物", chars or "（无）", indent=indent)
    kv("地点", ", ".join(outline.get("locations") or []) or "（无）", indent=indent)
    kv("物品", ", ".join(i.get("name", "") for i in outline.get("items") or []) or "（无）", indent=indent)
    res = ", ".join(f.get("id", "") for f in outline.get("foreshadowing_to_resolve") or [])
    kv("待回收伏笔", res or "（无）", indent=indent)
    plant = "; ".join(str(f.get("content", ""))[:24] for f in outline.get("foreshadowing_to_plant") or [])
    kv("待埋新伏笔", plant or "（无）", indent=indent)
    kv("情绪 / 风格", f"{outline.get('mood', '')} / {'、'.join(outline.get('style_keywords') or [])}",
       indent=indent)


def _render_memory(result: dict, indent: str = "  ") -> None:
    if not isinstance(result, dict):
        kv("检索结果", repr(result)[:200], indent=indent)
        return
    kv("耗时 / 已用 token", f"{result.get('elapsed_ms', '?')} ms / {result.get('total_tokens', '?')}",
       indent=indent)
    for band, cn in (("high", "高相关"), ("medium", "中相关"), ("low", "弱相关(抽样)")):
        hits = result.get(band) or []
        kv(cn, f"{len(hits)} 条", indent=indent)
        for h in hits:
            if isinstance(h, dict):
                content = str(h.get("content", ""))
                if len(content) > 46:
                    content = content[:46] + "…"
                kv(f"  [{h.get('layer')}] {h.get('entity')} v{h.get('version')}",
                   f"conf={h.get('confidence')} {content}", indent=indent)
            else:
                kv("  ", str(h)[:80], indent=indent)
    norm = result.get("normalized") or {}
    if norm.get("aliases"):
        kv("别名归一化", ", ".join(f"{a}→{c}" for a, c in norm["aliases"].items()), indent=indent)


def _render_patrol(items: Sequence[Any], indent: str = "  ") -> None:
    if not items:
        kv("巡检", "未发现问题（无超期/陈旧伏笔）", indent=indent)
        return
    for it in items:
        if isinstance(it, dict):
            tp = {"overdue": "超期", "stale": "陈旧", "resolved_pending": "已回收仍挂账",
                  "closed": "已关闭"}.get(str(it.get("type")), str(it.get("type")))
            line = f"[{tp}] {it.get('id', '?')}"
            for k in ("planned_resolve", "planted_chapter", "chapter_id", "current_chapter"):
                if it.get(k):
                    line += f"  {k}={it[k]}"
            kv(line, "", indent=indent)
            if it.get("suggestion"):
                kv("  建议", it["suggestion"], indent=indent)
        else:
            kv("·", str(it), indent=indent)


def _render_warnings(items: Sequence[Any], indent: str = "  ") -> None:
    if not items:
        kv("矛盾告警", "无矛盾", indent=indent)
        return
    kv("矛盾告警", f"{len(items)} 条", indent=indent)
    for w in items:
        say(f"{indent}⚠ {_short(w, 160)}")


def _render_stats(stats: dict, indent: str = "  ") -> None:
    kv("库路径", stats.get("path"), indent=indent)
    recs = stats.get("records") or {}
    for layer in C.LAYERS:
        kv(f"{C.LAYER_CN.get(layer, layer)}（{layer}）", recs.get(layer, 0), indent=indent)
    kv("记录总数", stats.get("total_records"), indent=indent)
    for key, cn in (
        ("history", "版本历史条数"),
        ("terms", "倒排词条数"),
        ("edges", "关系边数"),
        ("aliases", "称呼条数"),
        ("state", "状态项数"),
        ("chapters", "章节正文数"),
        ("open_foreshadowings", "未回收伏笔"),
    ):
        kv(cn, stats.get(key), indent=indent)


def _chapter_text_summary(text: str, limit: int = 160) -> str:
    flat = text.replace("\n", "")
    return flat[:limit] + ("…" if len(flat) > limit else "")


def cmd_demo(args: argparse.Namespace) -> int:
    """①规划 → ②检索 → ③生成 → ④动态追加 → ⑤回写 → ⑥巡检。"""
    from .llm import draft_request, get_backend, next_chapter_id

    db = args.db if args.db is not None else getattr(args, "db", None)
    window = max(1000, int(args.window))
    chest = _open_chest(db, seed=not args.no_seed)
    summary: dict[str, Any] = {"db": chest.path, "window": window}

    name = None if args.llm in (None, "auto") else args.llm
    backend = get_backend(name, model=args.model)
    summary["backend"] = type(backend).__name__

    hr("EnderWriter 演示：六阶段写作闭环")
    kv("末影箱", chest.path)
    kv("模型窗口", f"{window} token（章节上下文上限 = 70% = {int(window * 0.7)} token）")
    kv("大模型后端", f"{type(backend).__name__}（{'联网' if getattr(backend, 'online', False) else '离线'}）")
    st0 = chest.stats()
    kv("起步数据", f"{st0['total_records']} 条记录 / {st0['chapters']} 章正文 / "
                   f"{st0['open_foreshadowings']} 条未回收伏笔")

    try:
        # ---------------------------------------------------------- ① 规划
        section("①", "规划（PLAN）：从末影箱推导需求 → §3.1 章节细纲")
        chapter_id = str(args.chapter or next_chapter_id(chest))
        req = draft_request(chest, chapter_id, focus=args.focus)
        kv("章节需求", C.dumps(req)[:220] + "…")
        outline = backend.plan(req)
        C.validate(outline, C.schema("chapter_outline"))
        kv("Schema 校验", "通过（§3.1 chapter_outline，9 个必填字段齐全）")
        _render_outline(outline)
        summary["outline"] = outline
        chapter_id = str(outline.get("chapter_id") or chapter_id)

        # ---------------------------------------------------------- ② 检索
        section("②", "检索（RETRIEVE）：search_memory 三路召回 + 三档分层")
        search_memory = lazy_attr("retrieval", "search_memory")
        entities = [c["name"] for c in outline.get("active_characters") or [] if c.get("name")]
        entities += [x for x in (outline.get("locations") or []) if x]
        query = str(outline.get("chapter_goal") or "")
        # §3.3 要求 entities 至少 1 个：细纲万一没写人物/地点，就退回到本章 ID 与目标文本
        if not entities:
            entities = [str(outline.get("chapter_id") or "本章"), query[:12] or "本章"]
        mem_req = {"entities": entities[:6], "query_context": query, "budget": 800}
        C.validate(mem_req, C.schema("search_memory_tool"))
        kv("工具入参", C.dumps(mem_req))
        result = search_memory(chest, mem_req)
        C.validate(result, C.schema("memory_result"))
        kv("Schema 校验", "通过（§3.3 memory_result）")
        _render_memory(result)
        summary["retrieval"] = {
            "high": len(result.get("high") or []),
            "medium": len(result.get("medium") or []),
            "low": len(result.get("low") or []),
            "total_tokens": result.get("total_tokens"),
        }

        # ------------------------------------------------------- ②b 组装
        section("②b", "组装（ASSEMBLE）：P0-P4 分层预算，总量 ≤ 窗口 70%")
        Orchestrator = lazy_attr("orchestrator", "Orchestrator")
        orch = Orchestrator(chest, window_tokens=window)
        ctx = orch.assemble_context(outline, current_chapter=chapter_id)
        budget = ctx.get("budget") if isinstance(ctx, dict) else None
        if isinstance(budget, dict):
            parts = "  ".join(f"{k}={v}" for k, v in budget.items() if isinstance(v, (int, float)))
            kv("各段 token", parts)
        kv("合计 token", f"{ctx.get('total_tokens')} / 上限 {int(window * 0.7)}")
        kv("被裁掉", "、".join(str(x) for x in (ctx.get('dropped') or [])) or "（无）")
        ctx_text = str(ctx.get("text") or "")
        summary["context"] = {"total_tokens": ctx.get("total_tokens"),
                              "dropped": ctx.get("dropped") or []}

        # ---------------------------------------------------------- ③ 生成
        section("③", "生成（GENERATE）：把分层上下文拼成 prompt 喂给模型")
        prompt = _build_prompt(ctx_text, outline)
        kv("prompt token", C.est_tokens(prompt))
        text = backend.generate(prompt, max_tokens=int(args.words))
        kv("正文 token / 字数", f"{C.est_tokens(text)} / {len(text)}")
        say()
        for line in _paragraphs(text):
            say("  " + line)
        summary["draft"] = text

        # ------------------------------------------------------ ④ 动态追加
        section("④", "动态追加（PREFETCH）：生成流中识别新实体 → 增量检索")
        if hasattr(orch, "prefetch"):
            known = tuple(entities)
            window_chars = max(1, len(text) // 3)
            step = max(60, window_chars)
            seen_new: list[str] = []
            for i in range(0, len(text), step):
                chunk = text[i: i + step]
                if not chunk:
                    continue
                pf = orch.prefetch(chunk, context_entities=known)
                if isinstance(pf, dict):
                    seen_new.extend(pf.get("prefetched") or [])
            drained = orch.drain_prefetch() if hasattr(orch, "drain_prefetch") else []
            new_ents = C.dedupe([x for x in seen_new if x])
            kv("流式切片", f"{len(text)} 字 / 每片 {step} 字")
            kv("新检出实体", "、".join(new_ents) or "（无新增，全部已在上下文）")
            kv("追加检索批次", len(drained) if isinstance(drained, list) else "?")
            if new_ents:
                call = orch.build_tool_call(new_ents[:4], query, budget=400)
                extra_req = _tool_arguments(call)
                C.validate(extra_req, C.schema("search_memory_tool"))
                extra = orch.render_tool_result(search_memory(chest, extra_req))
                kv("追加检索入参", C.dumps(extra_req))
                kv("追加注入 token", C.est_tokens(extra))
                prompt = prompt + "\n\n【动态追加记忆】\n" + extra
            summary["prefetch"] = new_ents
        else:  # pragma: no cover - 编排层可能未提供预取
            kv("预取", "编排层未提供 prefetch()，本阶段跳过")

        # ---------------------------------------------------------- ⑤ 回写
        section("⑤", "回写（WRITEBACK）：§3.5 报告 → 落地到七个格子")
        report = backend.writeback(text, outline)
        C.validate(report, C.schema("writeback_report"))
        kv("Schema 校验", "通过（§3.5 writeback_report）")
        WriteBackAgent = lazy_attr("writeback", "WriteBackAgent")
        agent = WriteBackAgent(chest)
        report = _attach_text(report, chapter_id, text, outline)
        apply_res = agent.apply(report, chapter_id=chapter_id)
        upd = report.get("updated") or {}
        kv("更新人物", "、".join(upd.get("characters") or []) or "（无）")
        kv("更新物品", "、".join(upd.get("items") or []) or "（无）")
        kv("更新时间线", "、".join(upd.get("timeline") or []) or "（无）")
        fs_upd = "、".join(f"{f.get('id')}={f.get('status')}" for f in upd.get("foreshadowing") or [])
        kv("伏笔状态", fs_upd or "（无）")
        applied = apply_res.get("applied") if isinstance(apply_res, dict) else None
        kv("已落地操作", f"{len(applied or [])} 项")
        for item in (applied or [])[:8]:
            say(f"  · {_short(item)}")
        wb_warn = (apply_res.get("warnings") if isinstance(apply_res, dict) else None) or []
        summary["writeback"] = {"chapter_id": chapter_id, "applied": len(applied or [])}

        # ---------------------------------------------------------- ⑥ 巡检
        section("⑥", "巡检（PATROL）：伏笔超期/陈旧 + 矛盾告警")
        patrol = lazy_attr("writeback", "patrol")
        items = patrol(chest, chapter_id, grace=3, stale_threshold=30) or []
        _render_patrol(items)
        contradictions = _collect_contradictions(agent, report, chapter_id)
        _render_warnings(contradictions)
        # ⑤ 阶段回写器自己报的 warnings 单独列出，避免和"矛盾"混为一谈
        if wb_warn:
            kv("回写提示", f"{len(wb_warn)} 条", indent="  ")
            for w in wb_warn:
                say(f"  · {_short(w)}")
        summary["patrol"] = [it for it in items if isinstance(it, dict)]
        summary["contradictions"] = contradictions

        # ---------------------------------------------------------- 收尾
        say()
        hr("末影箱统计（写入之后）")
        st = chest.stats()
        _render_stats(st)
        summary["stats"] = st
        kv("本章正文", f"{chest.get_chapter(chapter_id) is not None and '已入库' or '未入库'}（{chapter_id}）")

        say()
        hr("伏笔总览")
        for fs in chest.all_foreshadowings():
            kv(f"{fs.id} [{fs.status}]",
               f"埋于 {fs.planted_chapter} · 计划 {fs.planned_resolve}"
               + (f" · 回收 {fs.resolved_chapter}" if fs.resolved_chapter else ""))

        say()
        hr("演示完成")
        say(f"  六阶段全部通过；正文 {len(text)} 字已写入末影箱 {chapter_id}。")
        if args.json:
            say(C.dumps(summary))
        return EXIT_OK

    except NotReady as exc:
        say()
        err(f"[模块尚未就绪] {exc}")
        err("  请等待相应组件落盘后重试，或先运行 `python -m enderwriter stats` 查看已有数据。")
        return EXIT_NOT_READY
    except C.SchemaError as exc:
        say()
        err(f"[Schema 校验失败] {exc}")
        return EXIT_ERROR
    except RuntimeError as exc:
        say()
        err(f"[运行失败] {exc}")
        return EXIT_ERROR
    finally:
        try:
            backend.close()
        except Exception:
            pass
        chest.close()


def _paragraphs(text: str) -> list[str]:
    out = [p.strip() for p in str(text or "").splitlines() if p.strip()]
    return out or ["（空）"]


def _tool_arguments(call: Any) -> dict:
    """把编排层返回的工具调用信封统一成 ``search_memory_tool`` 入参。

    兼容三种形态：
      * 直接就是入参（含 ``entities``）；
      * OpenAI function-calling 信封 ``{"name":..., "arguments": {...}}``；
      * 信封里 ``arguments`` 是 JSON 字符串。
    """
    if not isinstance(call, dict):
        return {"entities": [str(x) for x in (call or [])]}
    if "entities" in call:
        return call
    args = call.get("arguments")
    if args is None and call.get("arguments_json"):
        try:
            args = json.loads(call["arguments_json"])
        except (TypeError, ValueError):
            args = None
    if isinstance(args, dict):
        return args
    ents = [c.get("name") for c in call.get("active_characters") or [] if c.get("name")]
    return {"entities": ents or ["?"]}


def _build_prompt(ctx_text: str, outline: dict) -> str:
    """分层上下文 + 细纲 -> 给模型的生成指令（带可解析的要素标记）。"""
    chars = ", ".join(c.get("name", "") for c in outline.get("active_characters") or [])
    locs = ", ".join(outline.get("locations") or [])
    items = ", ".join(i.get("name", "") for i in outline.get("items") or [])
    return "\n".join(
        [
            f"【第 {C.chapter_index(outline.get('chapter_id'))} 章 · 正文生成】",
            f"目标: {outline.get('chapter_goal', '')}",
            f"角色: {chars}",
            f"地点: {locs}",
            f"物品: {items}",
            f"情绪: {outline.get('mood', '')}",
            f"风格: {', '.join(outline.get('style_keywords') or [])}",
            "【上下文】",
            ctx_text,
            "【要求】承接上文，延续文风，只输出正文。",
        ]
    )


def _attach_text(report: dict, chapter_id: str, text: str, outline: dict) -> dict:
    """给回写报告补上正文与摘要（Schema 允许附加字段）。"""
    out = dict(report or {})
    out["chapter_id"] = chapter_id
    out.setdefault("text", text)
    out.setdefault("summary", str(outline.get("chapter_goal") or ""))
    out.setdefault("title", "")
    return out


def _collect_contradictions(agent: Any, report: dict, chapter_id: str,
                            extra: Sequence[Any] = ()) -> list[str]:
    """调用回写代理的矛盾检测（若提供），返回人类可读的中文告警。"""
    out: list[str] = [str(x) for x in extra]
    fn: Callable[..., Any] | None = getattr(agent, "detect_contradictions", None)
    if callable(fn):
        try:
            found = fn(report, chapter_id=chapter_id)
        except TypeError:
            found = fn(report)
        except Exception as exc:  # pragma: no cover - 检测器异常不阻断 demo
            out.append(f"矛盾检测器异常：{exc}")
            found = []
        for item in found or []:
            text = str(item)
            if text not in out:
                out.append(text)
    return out


def _short(value: Any, limit: int = 100) -> str:
    """把落地操作/告警压成一行（list/dict 用 JSON，字符串原样）。"""
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + "…"
    try:
        text = C.dumps(value)
    except Exception:  # pragma: no cover
        text = str(value)
    return text if len(text) <= limit else text[:limit] + "…"


# ==========================================================================
# query
# ==========================================================================
def cmd_query(args: argparse.Namespace) -> int:
    search_memory = lazy_attr("retrieval", "search_memory")
    chest = _open_chest(args.db, seed=True)
    try:
        entities = _split_csv(args.entities)
        if not entities:
            err("[参数错误] -e/--entities 至少要给一个实体")
            return EXIT_ERROR
        req: dict[str, Any] = {"entities": entities, "budget": int(args.budget)}
        if args.query:
            req["query_context"] = args.query
        C.validate(req, C.schema("search_memory_tool"))
        ctx_entities = tuple(_split_csv(args.context)) if args.context else ()
        result = search_memory(chest, req, context_entities=ctx_entities) if ctx_entities \
            else search_memory(chest, req)
        C.validate(result, C.schema("memory_result"))
    except C.SchemaError as exc:
        err(f"[Schema 校验失败] {exc}")
        return EXIT_ERROR
    finally:
        pass
    if args.json:
        say(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        hr("search_memory 结果")
        kv("入参", C.dumps(req))
        if ctx_entities:
            kv("已在上下文", "、".join(ctx_entities))
        _render_memory(result)
        say()
        kv("合计 token", result.get("total_tokens"))
        trace = result.get("trace") or []
        if trace:
            hr("trace")
            for t in trace:
                say(f"  - {t}")
    chest.close()
    return EXIT_OK


# ==========================================================================
# patrol
# ==========================================================================
def cmd_patrol(args: argparse.Namespace) -> int:
    patrol = lazy_attr("writeback", "patrol")
    chest = _open_chest(args.db, seed=True)
    try:
        chapter = str(args.chapter)
        if C.chapter_index(chapter) < 1:
            err(f"[参数错误] 非法章节号：{chapter}（应形如 ch_003）")
            return EXIT_ERROR
        items = patrol(chest, chapter, grace=int(args.grace), stale_threshold=int(args.stale)) or []
        if args.json:
            say(json.dumps(items, ensure_ascii=False, indent=2))
        else:
            hr(f"伏笔巡检 · 当前章节 {chapter}（grace={args.grace}, stale={args.stale}）")
            kv("未回收伏笔", len(chest.all_foreshadowings(only_open=True)))
            kv("全部伏笔", len(chest.all_foreshadowings()))
            _render_patrol(items)
            say()
            kv("巡检结论", f"{len(items)} 项需要处理" if items else "一切正常")
    finally:
        chest.close()
    return EXIT_OK


# ==========================================================================
# stats
# ==========================================================================
def cmd_stats(args: argparse.Namespace) -> int:
    chest = _open_chest(args.db, seed=True)
    try:
        st = chest.stats()
        if args.json:
            say(json.dumps(st, ensure_ascii=False, indent=2))
        else:
            hr("末影箱统计")
            _render_stats(st)
    finally:
        chest.close()
    return EXIT_OK


# ==========================================================================
# export-schemas
# ==========================================================================
def cmd_export_schemas(args: argparse.Namespace) -> int:
    out = os.path.abspath(os.path.expanduser(str(args.out)))
    try:
        written = C.export_schemas(out)
    except OSError as exc:
        err(f"[导出失败] 无法写入目录 {out}：{exc}")
        return EXIT_ERROR
    hr("导出 §3 Schema")
    kv("目录", out)
    for p in written:
        kv("·", os.path.basename(p))
    kv("数量", len(written))
    return EXIT_OK


def cmd_install(args: argparse.Namespace) -> int:
    """自动安装本地 Qwen 环境（Ollama 或 llama.cpp）。"""
    try:
        from .install import run_install
    except ImportError as exc:  # pragma: no cover
        err(f"模块尚未就绪：enderwriter/install.py（{exc}）")
        return EXIT_DEPS
    return run_install(
        engine=getattr(args, "engine", "auto") or "auto",
        model=getattr(args, "model", None),
        check_only=bool(getattr(args, "check", False)),
        install=not bool(getattr(args, "no_install", False)),
        pull=not bool(getattr(args, "no_pull", False)),
        asset_key=getattr(args, "gpu", None),
    )


_HANDLERS: dict[str, Callable[[argparse.Namespace], int]] = {
    "demo": cmd_demo,
    "query": cmd_query,
    "patrol": cmd_patrol,
    "stats": cmd_stats,
    "export-schemas": cmd_export_schemas,
    "install": cmd_install,
}


# ==========================================================================
# main
# ==========================================================================
def main(argv: Sequence[str] | None = None) -> int:
    """命令行主入口。返回退出码（0 成功；1 失败；3 依赖模块尚未就绪）。"""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # pragma: no cover - 某些环境不支持 reconfigure
        pass
    parser = build_parser()
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit as exc:  # --help / 参数错误：argparse 已经打印过信息
        code = exc.code if isinstance(exc.code, int) else EXIT_ERROR
        if code == 0:
            return EXIT_OK
        err(f"[参数错误] 子命令参数不合法，运行 `python -m enderwriter <子命令> --help` 查看用法。")
        return EXIT_ERROR
    if not getattr(args, "cmd", None):
        parser.print_help()
        return EXIT_OK
    handler = _HANDLERS.get(args.cmd)
    if handler is None:  # pragma: no cover - argparse 已挡住
        parser.print_help()
        return EXIT_ERROR
    try:
        return int(handler(args))
    except NotReady as exc:
        err(f"[模块尚未就绪] {exc}")
        return EXIT_NOT_READY
    except KeyboardInterrupt:  # pragma: no cover
        err("[已中断]")
        return 130
    except Exception as exc:  # pragma: no cover - 兜底，保证不吐 traceback 给用户
        if os.environ.get("EW_DEBUG"):
            traceback.print_exc()
        else:
            err(f"[未预期错误] {type(exc).__name__}: {exc}")
            err("  设置环境变量 EW_DEBUG=1 可查看完整堆栈。")
        return EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
