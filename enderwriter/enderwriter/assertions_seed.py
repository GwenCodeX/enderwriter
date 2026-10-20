"""内置断言：从真实事故复盘里提炼的可执行护栏。

这些不是凭空写的编程建议，全部来自实际踩过的坑，正文里的数字是实测值。

## 两类触发方式

**代码特征**（``detect_all``）—— 写代码时抓：

    unity-operator-eq-null-recursion   重载 operator== 里用 == null 判空
    unity-linear-scan-in-loop          循环里 IndexOf/Exists 线性扫描
    unity-string-concat-in-loop        循环里字符串 += 累加
    dnspy-residual-compiler-idents     反编译残留的编译器生成标识符

**任务意图**（``when``）—— 动手之前抓：

    unity-writeback-field-order        回写 DLL 前的字段顺序检查
    unity-writeback-type-diff          回写 DLL 前的类型集合比对
    dnspy-ldc-r4-audit                 浮点常量必须按 IL 字节核对
    unity-perf-time-window-probe       帧率问题必须实测
    unity-build-noconfig               csc 参数放置位置

## 为什么有些教训没有变成断言

「用时间窗口探针排查」这类是**方法**，源码里没有对应特征；
「字段顺序必须一致」是**流程**，只能靠"你正要回写"来触发。

后者用 ``when`` 覆盖；纯粹的方法论留在案例正文里——
硬做成代码断言只会误报，而误报会让人关掉整个功能。
"""

from __future__ import annotations

from typing import Any, Iterable

from .guard import AssertionError_, save

__all__ = ["BUILTIN_ASSERTIONS", "seed_builtin"]

#: 每条都是 dict，字段与 :func:`guard.save` 一致
BUILTIN_ASSERTIONS: tuple[dict[str, Any], ...] = (
    # ------------------------------------------------------------------
    # 代码特征类
    # ------------------------------------------------------------------
    {
        "id": "unity-operator-eq-null-recursion",
        "rule": "重载了 operator== 的类，运算符体内不得用 a == null 判空——会递归调用自身",
        "detect_all": ["operator ==", "== null"],
        "detect_not": ["ReferenceEquals"],
        "fix": "判空改用 ReferenceEquals(a, null) 或 (object)a == null；并去掉包裹比较的 try/catch",
        "consequence": (
            "无限递归 → 栈溢出 → 被同函数的 catch (Exception) 吞掉，无任何报错；"
            "每个调用点约 71ms（抛异常+栈展开），30~80 个实例在 Update 里即掉到 0.3 FPS"
        ),
        "severity": "high",
        "category": "性能",
        "cases": ["unity性能杀手:operator==自递归被catch吞掉"],
    },
    {
        "id": "unity-linear-scan-in-loop",
        "rule": "循环内不得用 List.IndexOf / List.Exists 做成员判断，应改用 HashSet / Dictionary",
        "detect_all": ["IndexOf(", "foreach"],
        "detect_not": ["HashSet", "Dictionary"],
        "fix": "先构建一次 HashSet<T> / Dictionary<K,V>，循环内改用 Contains / TryGetValue 做 O(1) 判断",
        "consequence": (
            "成员判断退化成 O(n²)~O(n³)；在自动保存这类周期性操作里表现为规律性卡顿，"
            "随数据量增长而恶化"
        ),
        "severity": "medium",
        "category": "性能",
        "cases": ["unity性能杀手:operator==自递归被catch吞掉"],
    },
    {
        "id": "unity-string-concat-in-loop",
        "rule": "循环内不得用 += 累加字符串，应改用 StringBuilder",
        "detect_all": ['+= "'],
        "detect_not": ["StringBuilder"],
        "allow_single": True,
        "fix": "循环前建 StringBuilder，循环内 Append，结束后 ToString()",
        "consequence": "每次 += 都新建并整体拷贝字符串，在长循环里造成 O(n²) 分配与 GC 压力",
        "severity": "low",
        "category": "性能",
        "cases": ["unity性能杀手:operator==自递归被catch吞掉"],
    },
    {
        "id": "dnspy-residual-compiler-idents",
        "rule": "出现编译器生成的标识符（>g__ / <>9__ / >d__）时，必须按原 DLL 的 IL 还原，不得改名成「合法」标识符",
        "detect_all": [">g__"],
        "detect_not": [],
        "allow_single": True,
        "fix": (
            "用 Mono.Cecil 导出原 DLL 的类型/方法/字段集合，逐个对照；"
            "编译器生成的闭包类、状态机、局部函数必须保持原名与结构"
        ),
        "consequence": (
            "改名后语义可能变化（闭包缓存、async 状态机字段被 Unity 按位置序列化）；"
            "也可能因方法签名不匹配导致 prefab 报 Missing Script"
        ),
        "severity": "medium",
        "category": "反编译",
        "cases": ["dnspy反编译回写工程经验"],
    },
    # ------------------------------------------------------------------
    # 任务意图类（动手之前）
    # ------------------------------------------------------------------
    {
        "id": "unity-writeback-field-order",
        "rule": "回写 DLL 前必须逐类型比对「序列化字段声明顺序」，必须 0 差异",
        "when": ["回写 DLL", "回写DLL", "Assembly-CSharp", "dnSpy 回写"],
        "detect_all": [],
        "fix": (
            "用 Mono.Cecil 导出每个类型的实例字段（public 或 [SerializeField]）声明顺序，"
            "与原 DLL 对比；新增字段一律追加到列表末尾，不要按逻辑分组插到中间"
        ),
        "consequence": (
            "Unity 发布版按位置解析 level0 里的字段值；顺序变了会读到错误长度/类型，"
            "报 `The file 'level0' is corrupted!` 并崩溃——报的是资源损坏，极易误判"
        ),
        "severity": "high",
        "category": "反编译",
        "cases": ["unity回写:序列化字段顺序导致level0崩溃"],
    },
    {
        "id": "unity-writeback-type-diff",
        "rule": "回写 DLL 前必须做「类型集合 diff」，确认新 DLL 不缺原 DLL 里的类型",
        "when": ["回写 DLL", "回写DLL", "Assembly-CSharp", "反编译回写"],
        "detect_all": [],
        "fix": (
            "用 Mono.Cecil 导出两边的 (命名空间, 类型名) 集合做差集；"
            "缺的类型必须补回，否则 shipped prefab 引用会变成 Missing Script"
        ),
        "consequence": (
            "反编译源码可能来自旧版本 DLL（实测源 298 文件/363 类型 vs 实际 301/366），"
            "缺类型会让 prefab 引用失效"
        ),
        "severity": "high",
        "category": "反编译",
        "cases": ["dnspy反编译回写工程经验"],
    },
    {
        "id": "dnspy-ldc-r4-audit",
        "rule": "反编译产物里的浮点常量必须按 IL 的原始字节核对，不得直接采信 C# 字面量",
        "when": ["浮点常量", "ldc.r4", "反编译回写", "常量渲染"],
        "detect_all": [],
        "fix": (
            "用 Mono.Cecil 遍历两个 DLL 的每个方法，导出所有 ldc.r4 操作数的 "
            "IEEE-754 原始字节（BitConverter.GetBytes），按方法名归一化成十六进制串做集合对比"
        ),
        "consequence": (
            "dnSpy 会把 2E-12f 渲染成 0.002f（放大 1e10 倍）且编译通过、静默改变行为；"
            "实测同一份谱面存档 10478 字符 vs 8933 字符（丢行）。"
            "一个工具函数常量错会污染全部调用点（实测 30+ 处）"
        ),
        "severity": "high",
        "category": "反编译",
        "cases": ["dnspy浮点常量渲染bug与ldc.r4全库审计"],
    },
    {
        "id": "unity-perf-time-window-probe",
        "rule": "帧率骤降必须用「按时间窗口」的探针对每段实测，不得用帧计数窗口，也不得只凭静态代码推断",
        "when": ["卡顿", "帧率", "掉帧", "性能排查"],
        "detect_all": [],
        "fix": (
            "用 Stopwatch / Time.realtimeSinceStartup 给每个 Update 及其内部分段各自计时；"
            "按经过时间截止采样（帧计数窗口在 1 FPS 时要等一分钟才采满）"
        ),
        "consequence": (
            "静态读代码会把真凶误判成「普通比较」；实测曾直接定位出 "
            "sn.eq=3202ms 而做更多工作的 UpdateShape 仅 0.07ms，"
            "相差千倍才暴露「这是异常代价」而非计算量"
        ),
        "severity": "medium",
        "category": "性能",
        "cases": ["卡顿排查:按时间窗口探针逐段定位(三层递进)"],
    },
    {
        "id": "dnspy-repair-must-match-il",
        "rule": "修复反编译产物必须逐条对照原版 IL 语义，不得凭「看起来对」就改",
        "when": ["反编译修复", "修复反编译", "回写 DLL"],
        "detect_all": [],
        "fix": (
            "每改一处先用 ILSpy/Cecil 看原版 IL：例如运算符重载里原版用 brtrue 判空，"
            "而不是调用重载；等值比较原版可能是逐字段比较而非 ReferenceEquals"
        ),
        "consequence": (
            "实测第一次「修复」operator== 时又引入两个新错：漏比字段、"
            "用 ReferenceEquals 短路而原版逐字段比较（同实例不同字段时原版返回 false）"
        ),
        "severity": "high",
        "category": "反编译",
        "cases": ["dnspy浮点常量渲染bug与ldc.r4全库审计"],
    },
)


def seed_builtin(chest: Any, *, overwrite: bool = False) -> dict[str, Any]:
    """把内置断言写进库。

    ``overwrite=False`` 时跳过已存在的（不覆盖用户改过的版本）。

    返回 ``{"added": [...], "skipped": [...], "rejected": [...]}``。
    """
    added: list[str] = []
    skipped: list[str] = []
    rejected: list[dict[str, str]] = []

    existing: set[str] = set()
    try:
        existing = set(chest.all_entities(["assertion"]))
    except Exception:  # noqa: BLE001
        existing = set()

    for spec in BUILTIN_ASSERTIONS:
        aid = str(spec.get("id") or "").strip()
        if not aid:
            continue
        if aid in existing and not overwrite:
            skipped.append(aid)
            continue
        fields = {k: v for k, v in spec.items() if k not in ("id", "rule")}
        try:
            save(chest, aid, str(spec.get("rule") or ""), **fields)
            added.append(aid)
        except AssertionError_ as exc:
            rejected.append({"id": aid, "reason": str(exc)})

    return {"added": added, "skipped": skipped, "rejected": rejected}
