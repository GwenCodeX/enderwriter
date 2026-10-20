"""技术断言：把「事故复盘」转成「可机械判定的护栏」。

## 为什么需要这一层

技术库原本只存**案例叙述**（"当时发生了什么"）。它有三个问题：

1. 只在人主动去查时有效，而事故恰恰发生在"不知道自己不知道"的时候
2. 叙述要读完 800 字才拿到要点，实战中不会读
3. 通用教训被项目细节淹没（"dcs逆向 v1.1.2"这类），换个项目就以为无关

**断言**回答的是另一个问题："下次别再犯什么"。

    rule        重载 operator== 的类中，不许用 a == null 判空
    detect_all  ["operator ==", "== null"]
    detect_not  ["ReferenceEquals"]
    fix         改用 ReferenceEquals(a, b)
    consequence 每次比较 ~71ms；30 实例 → 0.3 FPS

`detect_all` 全部出现、且 `detect_not` 一个都没出现 → 命中。

## 为什么校验比模型更重要

用模型抽断言时，它会生成两类废品：

    ["operator =="]              条件太宽，任何重载都报
    ["Update", "snInfo =="]      绑死变量名，换项目就失效

这两类都会让断言**持续误报，最后被无视**——比没有断言更糟。
所以 :func:`validate` 用确定性规则把它们挡在入库之前。

## 置信度

匹配是字面的，不理解语义。命中只代表"这里可能有坑"，不代表"一定有错"。
所以输出一律是**提示**，不是阻断。
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Sequence

from .common import get_logger

__all__ = [
    "Assertion",
    "GuardHit",
    "AssertionError_",
    "validate",
    "to_record_data",
    "from_record",
    "save",
    "list_assertions",
    "match",
    "guard",
    "render",
    "SEVERITIES",
]

log = get_logger("guard")

#: 严重度，越小越严重
SEVERITIES = ("high", "medium", "low")
_SEV_ORDER = {s: i for i, s in enumerate(SEVERITIES)}

# --------------------------------------------------------------------------
# 校验：挡住会持续误报的断言
# --------------------------------------------------------------------------
#: 这些词在普通项目里到处都有，作为匹配条件等于"永远命中"
_GENERIC_TOKENS: frozenset[str] = frozenset(
    {
        "void update", "void start", "void awake", "void onenable",
        "public class", "private void", "public void", "static void",
        "using system", "using unityengine", "namespace",
        "for (", "while (", "if (", "return ", "var ", "int ", "string ",
        "catch (", "try {", "else", "{", "}", "()", ";",
    }
)

#: 匹配条件的最短长度——比这更短的条件无法区分具体情境
_MIN_TOKEN_LEN = 3


class AssertionError_(ValueError):
    """断言内容不合法。"""


def _norm_tokens(raw: Any) -> list[str]:
    """把 detect_all / detect_not 规整成去重后的字符串列表。"""
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [raw]
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        s = str(item or "").strip()
        if not s:
            continue
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


def validate(
    rule: str,
    detect_all: Sequence[str],
    *,
    fix: str = "",
    allow_single: bool = False,
    when: Sequence[str] | None = None,
) -> list[str]:
    """校验一条断言能否入库，返回问题列表（空列表 = 通过）。

    这里**故意严格**：宁可入库少，也不能让会误报的断言混进去。
    一条永远误报的断言，会让人把整个防护功能关掉。

    两种触发方式，至少要有一个：
      * ``detect_all`` —— 代码特征（写的时候抓）
      * ``when``       —— 任务意图（动手之前抓）

    ``when`` 解决的是另一类教训：「回写 DLL 前必须比对字段顺序」这种
    流程性规则，源码里没有对应特征，只能靠"你正要干这件事"来触发。

    :param allow_single: 允许只要一个代码条件（仅用于极其特定的信号）
    """
    problems: list[str] = []

    if not str(rule or "").strip():
        problems.append("rule 不能为空")

    tokens = _norm_tokens(detect_all)
    whens = _norm_tokens(when)

    if not tokens and not whens:
        problems.append(
            "detect_all 与 when 至少要有一个："
            "前者按代码特征触发，后者按任务意图触发"
        )
        return problems

    # 代码特征的严格校验只在"靠代码特征触发"时生效
    if tokens:
        need = 1 if (allow_single or whens) else 2
        if len(tokens) < need:
            problems.append(
                f"detect_all 至少需要 {need} 个条件（当前 {len(tokens)} 个）。"
                f"单个泛化条件会让断言对几乎所有代码报错，最终被无视。"
            )

    for t in tokens:
        low = t.lower()
        if low in _GENERIC_TOKENS:
            problems.append(
                f"条件 {t!r} 过于通用：普通项目里到处都有，会让断言永远误报。"
            )
        if len(t) < _MIN_TOKEN_LEN:
            problems.append(f"条件 {t!r} 太短（< {_MIN_TOKEN_LEN} 字符），无法定位具体问题。")

    for w in whens:
        if len(w) < 2:
            problems.append(f"任务关键词 {w!r} 太短，无法判断意图。")

    return problems


# --------------------------------------------------------------------------
# 数据结构
# --------------------------------------------------------------------------
class Assertion:
    """一条可机械判定的技术断言。

    两种触发方式：
      * ``detect_all`` / ``detect_not`` —— 按**代码特征**触发（写的时候抓）
      * ``when``                        —— 按**任务意图**触发（动手前抓）
    """

    __slots__ = (
        "id",
        "rule",
        "detect_all",
        "detect_not",
        "when",
        "fix",
        "consequence",
        "severity",
        "category",
        "cases",
        "version",
    )

    def __init__(
        self,
        aid: str,
        rule: str,
        detect_all: Sequence[str],
        *,
        detect_not: Sequence[str] = (),
        when: Sequence[str] = (),
        fix: str = "",
        consequence: str = "",
        severity: str = "medium",
        category: str = "其他",
        cases: Sequence[str] = (),
        version: int = 1,
    ) -> None:
        self.id = aid
        self.rule = rule
        self.detect_all = list(detect_all)
        self.detect_not = list(detect_not)
        self.when = list(when)
        self.fix = fix
        self.consequence = consequence
        self.severity = severity if severity in _SEV_ORDER else "medium"
        self.category = category
        self.cases = list(cases)
        self.version = version

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "rule": self.rule,
            "detect_all": list(self.detect_all),
            "detect_not": list(self.detect_not),
            "when": list(self.when),
            "fix": self.fix,
            "consequence": self.consequence,
            "severity": self.severity,
            "category": self.category,
            "cases": list(self.cases),
            "version": self.version,
        }

    def __repr__(self) -> str:  # pragma: no cover
        return f"Assertion({self.severity}:{self.id})"


class GuardHit:
    """一条命中的断言，附带命中的依据。"""

    __slots__ = ("assertion", "matched", "line", "via")

    def __init__(
        self,
        assertion: Assertion,
        matched: Sequence[str],
        *,
        line: int = 0,
        via: str = "code",
    ) -> None:
        self.assertion = assertion
        self.matched = list(matched)
        self.line = line
        #: code = 命中代码特征；task = 命中任务意图
        self.via = via

    def to_dict(self) -> dict[str, Any]:
        d = self.assertion.to_dict()
        d["matched"] = list(self.matched)
        d["via"] = self.via
        if self.line:
            d["line"] = self.line
        return d


# --------------------------------------------------------------------------
# 存取
# --------------------------------------------------------------------------
#: 断言在末影箱里的图层名
LAYER = "assertion"


def to_record_data(
    *,
    detect_all: Sequence[str],
    detect_not: Sequence[str] = (),
    when: Sequence[str] = (),
    fix: str = "",
    consequence: str = "",
    severity: str = "medium",
    category: str = "其他",
    cases: Sequence[str] = (),
) -> dict[str, Any]:
    """打包成 records.data 的结构。"""
    return {
        "detect_all": _norm_tokens(detect_all),
        "detect_not": _norm_tokens(detect_not),
        "when": _norm_tokens(when),
        "fix": str(fix or ""),
        "consequence": str(consequence or ""),
        "severity": severity if severity in _SEV_ORDER else "medium",
        "category": str(category or "其他"),
        "cases": [str(c) for c in (cases or [])],
    }


def from_record(rec: Any) -> Assertion:
    """把一条 records 行转成 Assertion。"""
    data = dict(getattr(rec, "data", None) or {})
    return Assertion(
        getattr(rec, "entity", ""),
        getattr(rec, "content", ""),
        _norm_tokens(data.get("detect_all")),
        detect_not=_norm_tokens(data.get("detect_not")),
        when=_norm_tokens(data.get("when")),
        fix=str(data.get("fix") or ""),
        consequence=str(data.get("consequence") or ""),
        severity=str(data.get("severity") or "medium"),
        category=str(data.get("category") or "其他"),
        cases=list(data.get("cases") or []),
        version=int(getattr(rec, "version", 1) or 1),
    )


def save(chest: Any, assertion_id: str, rule: str, **fields: Any) -> Assertion:
    """写入/更新一条断言。

    先校验，不合法直接抛 :class:`AssertionError_`——不允许把会误报的断言写进库。

    ``allow_single=True`` 用于**本身就是唯一信号**的条件
    （如 ``>g__`` 只出现在 C# 编译器生成的局部函数里）。
    """
    detect_all = fields.get("detect_all") or []
    when = fields.get("when") or ()
    problems = validate(
        rule,
        detect_all,
        fix=fields.get("fix", ""),
        allow_single=bool(fields.get("allow_single")),
        when=when,
    )
    if problems:
        raise AssertionError_("；".join(problems))

    data = to_record_data(
        detect_all=detect_all,
        detect_not=fields.get("detect_not") or (),
        when=when,
        fix=fields.get("fix", ""),
        consequence=fields.get("consequence", ""),
        severity=fields.get("severity", "medium"),
        category=fields.get("category", "其他"),
        cases=fields.get("cases") or (),
    )
    rec = chest.upsert(LAYER, assertion_id, rule, data=data, keywords=[assertion_id])
    return from_record(rec)


def list_assertions(chest: Any, *, min_severity: str = "low") -> list[Assertion]:
    """列出所有断言，按严重度排序。"""
    limit = _SEV_ORDER.get(min_severity, 2)
    out: list[Assertion] = []
    try:
        ids = chest.all_entities([LAYER])
    except Exception as exc:  # noqa: BLE001
        log.warning("读取断言失败：%s", exc)
        return out
    for aid in ids:
        try:
            rec = chest.get(LAYER, aid)
        except Exception:  # noqa: BLE001
            continue
        if rec is None:
            continue
        a = from_record(rec)
        if _SEV_ORDER.get(a.severity, 2) <= limit:
            out.append(a)
    out.sort(key=lambda a: (_SEV_ORDER.get(a.severity, 2), a.id))
    return out


# --------------------------------------------------------------------------
# 匹配
# --------------------------------------------------------------------------
def _find_line(text: str, needle: str) -> int:
    """needle 首次出现的行号（1 基）。找不到返回 0。"""
    low = text.lower()
    idx = low.find(needle.lower())
    if idx < 0:
        return 0
    return text.count("\n", 0, idx) + 1


def match(assertions: Iterable[Assertion], text: str, *, task: str = "") -> list[GuardHit]:
    """在文本里匹配断言，返回命中列表。

    代码判定：``detect_all`` 全部出现 **且** ``detect_not`` 一个都没出现。

    ``detect_not`` 的意义是"已经写对了就不吵"——
    例如 detect_not 含 ``ReferenceEquals`` 时，代码里已经用了它就跳过。

    任务判定：给了 ``task`` 时，``when`` 里的关键词命中任务描述也算命中。
    这覆盖"流程性教训"——源码里没有特征，但你知道自己正要干这件事。
    """
    body = str(text or "")
    task_low = str(task or "").strip().lower()
    if not body.strip() and not task_low:
        return []

    low = body.lower()
    hits: list[GuardHit] = []

    for a in assertions:
        # ---- 任务意图 ----
        if task_low and a.when:
            got = [w for w in a.when if w.lower() in task_low]
            if got:
                hits.append(GuardHit(a, got, via="task"))
                continue

        # ---- 代码特征 ----
        if not a.detect_all:
            continue
        if not low:
            continue
        if any(t.lower() in low for t in a.detect_not):
            continue
        if not all(t.lower() in low for t in a.detect_all):
            continue
        line = _find_line(body, a.detect_all[0])
        hits.append(GuardHit(a, a.detect_all, line=line, via="code"))

    hits.sort(key=lambda h: (_SEV_ORDER.get(h.assertion.severity, 2), h.assertion.id))
    return hits


def guard(
    chest: Any, text: str, *, task: str = "", min_severity: str = "low"
) -> list[GuardHit]:
    """便捷入口：从库读断言并匹配。"""
    return match(
        list_assertions(chest, min_severity=min_severity), text, task=task
    )


# --------------------------------------------------------------------------
# 渲染
# --------------------------------------------------------------------------
_SEV_CN = {"high": "高", "medium": "中", "low": "低"}


def render(hits: Sequence[GuardHit], *, total: int = 0, detailed: bool = True) -> str:
    """把命中结果渲染成可读文本。"""
    if not hits:
        if total:
            return f"【技术护栏】{total} 条断言全部通过，未发现风险写法。"
        return (
            "【技术护栏】库里还没有断言。\n"
            "断言由事故复盘提炼而来：用 enderwriter_assert 写入，"
            "或让我从技术库的案例里抽取。"
        )

    lines = [f"【技术护栏】命中 {len(hits)} 条（共 {total or len(hits)} 条断言）"]
    for h in hits:
        a = h.assertion
        flag = _SEV_CN.get(a.severity, a.severity)
        head = f"\n[{flag}] {a.id}"
        if h.via == "task":
            head += "（任务相关）"
        elif h.line:
            head += f"（第 {h.line} 行）"
        lines.append(head)
        lines.append(f"  规则：{a.rule}")
        if h.matched:
            tag = "任务" if h.via == "task" else "命中"
            lines.append(f"  {tag}：{' + '.join(h.matched)}")
        if detailed and a.consequence:
            lines.append(f"  后果：{a.consequence}")
        if detailed and a.fix:
            lines.append(f"  修法：{a.fix}")
    return "\n".join(lines)
