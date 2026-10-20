"""一致性检查：用确定性规则核实正文与已确立设定的冲突。

## 为什么不用本地小模型

最初的设计是让 Qwen 当审查员（同模型 + 差异化输入）。
实测结果（Qwen2.5-3B）：

    输入：事实「主角甲左手刀伤缠绷带」+ 正文「主角甲右手握剑」
    输出：{"violated": true, "reason": "右手握剑与左手刀伤冲突"}   ← 误报

    输入：事实「主角甲境界=白牌」+ 正文「主角甲双手握剑」
    输出：{"violated": true, "reason": "正文没有提及境界"}         ← 把"未提及"当"违反"

误报率超过 80%，逐条二分类也治不了——3B 分不清「没提到」和「提到了但冲突」。

所以改成**确定性规则**：可解释、可测试、零误报、零延迟。
代价是没有语义理解能力，只能覆盖能用规则表达的冲突类型。

## 覆盖哪些冲突

    R1 死而复生      设定 alive=False / 已死，正文却让该角色行动
    R2 伤情违反      设定某部位受伤，正文让该部位做出需要完好的动作
    R3 持有者违反    设定物品在 A 手上，正文让 B 直接使用该物品
    R4 境界回退      设定境界为甲，正文声称其境界为更低的乙
    R5 位置瞬移      设定角色在某地，正文突然出现在另一个地名（启发式，low）

R1-R4 是高置信规则；R5 依赖"看起来像地名"的启发式判断，只报 low，
用来提示复核，不该据此阻断写作。

## 抽事实

事实来自两处：
  * state 表（显式结构化状态，最可靠）
  * 设定文本里的模式匹配（"左手受刀伤""已经死了""佩剑由主角乙保管"）

后者是为了适配"只写设定、不写 state"的实际用法。

## 没有事实时

规则全部依赖事实，事实为空必然零命中。这时**不能**返回"通过"——
那会被读成"查过了没问题"。此时 ``ok=False`` 且 ``notice`` 说明缺什么。
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Sequence

from .common import get_logger

__all__ = [
    "Fact",
    "Issue",
    "CheckResult",
    "extract_facts",
    "check_text",
    "BODY_PARTS",
    "CONFLICT_RULES",
]

log = get_logger("review")

#: 身体部位词——用于伤情冲突检测
BODY_PARTS: tuple[str, ...] = (
    "左手", "右手", "左臂", "右臂", "左腿", "右腿",
    "左眼", "右眼", "左脚", "右脚", "左肩", "右肩",
    "左手腕", "右手腕", "背部", "胸口", "脖颈", "头部",
)

#: 规则清单（供文档与面板展示）
CONFLICT_RULES: tuple[dict[str, str], ...] = (
    {"id": "R1", "name": "死而复生", "desc": "已死亡的角色出现在正文中并行动"},
    {"id": "R2", "name": "伤情违反", "desc": "受伤部位做出需要完好的动作"},
    {"id": "R3", "name": "持有者违反", "desc": "物品在他人手中却由本角色直接使用"},
    {"id": "R4", "name": "境界回退", "desc": "正文声称的境界低于已确立境界"},
    {"id": "R5", "name": "位置瞬移", "desc": "角色出现在与记录不符的地名，且无位移描述（启发式）"},
)


# ==========================================================================
# 数据结构
# ==========================================================================
class Fact:
    """一条可被正文违反的硬事实。"""

    __slots__ = ("id", "kind", "subject", "value", "chapter", "source")

    def __init__(
        self,
        fid: str,
        kind: str,
        subject: str,
        value: Any,
        *,
        chapter: str = "",
        source: str = "",
    ) -> None:
        self.id = fid
        self.kind = kind          # alive / injury / owner / realm / location
        self.subject = subject
        self.value = value
        self.chapter = chapter
        self.source = source

    def __repr__(self) -> str:  # pragma: no cover
        return f"Fact({self.kind}:{self.subject}={self.value!r})"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "subject": self.subject,
            "value": self.value,
            "chapter": self.chapter,
            "source": self.source,
        }


class Issue:
    """一条被检出的冲突。"""

    __slots__ = ("rule", "rule_name", "subject", "quote", "fact_id", "detail", "severity")

    def __init__(
        self,
        rule: str,
        rule_name: str,
        subject: str,
        quote: str,
        *,
        fact_id: str = "",
        detail: str = "",
        severity: str = "high",
    ) -> None:
        self.rule = rule
        self.rule_name = rule_name
        self.subject = subject
        self.quote = quote
        self.fact_id = fact_id
        self.detail = detail
        self.severity = severity

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "rule_name": self.rule_name,
            "subject": self.subject,
            "quote": self.quote,
            "fact_id": self.fact_id,
            "detail": self.detail,
            "severity": self.severity,
        }


class CheckResult:
    """检查结论。

    ``notice`` 是**检查没真正执行**时的说明。

    为什么需要它：规则要靠事实才能判定，事实为空时所有规则必然零命中。
    如果这时仍返回 ``ok=True, issues=[]``，调用方会读成"检查过、没问题"——
    这是比漏报更危险的情况：虚假的安全感。

    所以事实为空（或规则一条都没跑）时 ``ok`` 必须是 False，
    并在 ``notice`` 里说清缺什么、怎么补。
    """

    __slots__ = ("ok", "issues", "facts_used", "rules_run", "notice")

    def __init__(
        self,
        ok: bool = True,
        issues: Sequence[Issue] | None = None,
        *,
        facts_used: int = 0,
        rules_run: Sequence[str] = (),
        notice: str = "",
    ) -> None:
        self.ok = ok
        self.issues = list(issues or [])
        self.facts_used = facts_used
        self.rules_run = list(rules_run)
        self.notice = notice

    @property
    def executed(self) -> bool:
        """检查是否真的执行了（有事实 + 跑了规则）。"""
        return bool(self.facts_used) and bool(self.rules_run) and not self.notice

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "issues": [i.to_dict() for i in self.issues],
            "facts_used": self.facts_used,
            "rules_run": self.rules_run,
            "executed": self.executed,
            "notice": self.notice,
        }

    def render(self) -> str:
        if self.notice:
            return f"【一致性检查】未执行 —— {self.notice}"
        if not self.issues:
            return f"【一致性检查】通过（{self.facts_used} 条事实 / {len(self.rules_run)} 条规则）"
        lines = [f"【一致性检查】发现 {len(self.issues)} 处冲突"]
        for i in self.issues:
            lines.append(f"- [{i.rule} {i.rule_name}] {i.subject}：{i.detail}")
            lines.append(f"    正文：{i.quote}")
        return "\n".join(lines)


# ==========================================================================
# 从设定文本抽事实
# ==========================================================================
#: 伤情模式：「左手受刀伤」「左臂骨折」「右腿被咬伤」
_INJURY_PATTERNS = (
    re.compile(r"(?P<part>" + "|".join(BODY_PARTS) + r")(?:受|有|中了|被)?(?:了)?(?P<what>[^。；，\n]{0,12}?)(?:伤|断|裂|折|废|残|失|瞎|麻)"),
    re.compile(r"(?P<part>" + "|".join(BODY_PARTS) + r")(?:缠|裹|扎)(?:着|了)?(?:[^。；，\n]{0,8})?(?:绷带|布条|纱布)"),
)

#: 死亡模式：「已经死了」「于第X章死亡」「不幸身亡」
_DEATH_PATTERNS = (
    re.compile(r"(?:已经|已|最终|最后)?(?:死了|身亡|殒命|毙命|阵亡|殉职|战死)"),
    re.compile(r"(?:死亡|离世|去世)"),
)

#: 存活反例（排除「没死」「不会死」）
_ALIVE_NEGATORS = ("没", "未", "不会", "没有", "差点", "险些", "以为")

#: 持有模式：「由主角乙保管」「主角乙持有」「在李四手上」
_OWNER_PATTERNS = (
    re.compile(r"(?:由|归)(?P<who>[\u4e00-\u9fffA-Za-z]{2,6})(?:保管|持有|保存|执掌|携带)"),
    re.compile(r"在(?P<who>[\u4e00-\u9fffA-Za-z]{2,6})(?:手上|手中|掌中)"),
)


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"[。！？\n；]", str(text or "")) if s.strip()]


def extract_facts(
    chest: Any,
    *,
    entities: Sequence[str] = (),
    max_entities: int = 200,
) -> list[Fact]:
    """从末影箱抽取可检验的事实。

    两个来源：
      1. state 表 —— 显式结构化状态，直接转成 Fact
      2. 设定正文 —— 用模式匹配抽取伤情/死亡/持有者
    """
    facts: list[Fact] = []
    seen: set[str] = set()

    names = list(entities)
    if not names:
        try:
            names = chest.all_entities()[:max_entities]
        except Exception as exc:  # noqa: BLE001
            log.warning("读取实体失败：%s", exc)
            return facts

    for name in names:
        # --- 1) state 表 ---
        try:
            states = chest.states_of(name) or {}
        except Exception:  # noqa: BLE001
            states = {}

        for key, kind in (
            ("alive", "alive"),
            ("dead", "alive_dead"),
            ("injury", "injury"),
            ("owner", "owner"),
            ("realm", "realm"),
            ("location", "location"),
        ):
            if key not in states:
                continue
            val = states[key]
            if val is None or val == "" or isinstance(val, (dict, list)):
                continue
            if kind == "alive_dead":
                kind, val = "alive", not bool(val)
            fid = f"state:{name}:{key}"
            if fid in seen:
                continue
            seen.add(fid)
            chapter = ""
            try:
                chapter = chest.state_chapter(name, key) or ""
            except Exception:  # noqa: BLE001
                chapter = ""
            facts.append(Fact(fid, kind, name, val, chapter=chapter, source="state"))

        # --- 2) 设定正文模式匹配 ---
        try:
            recs = chest.get_any(name)
        except Exception:  # noqa: BLE001
            recs = []

        for rec in recs:
            body = str(rec.content or "")
            if not body:
                continue
            chapter = str(rec.chapter_id or "")

            # 伤情
            for pat in _INJURY_PATTERNS:
                for m in pat.finditer(body):
                    part = m.group("part")
                    fid = f"injury:{name}:{part}"
                    if fid in seen:
                        continue
                    seen.add(fid)
                    facts.append(
                        Fact(fid, "injury", name, part, chapter=chapter, source=rec.layer)
                    )

            # 死亡
            if rec.layer in ("character", "world"):
                for pat in _DEATH_PATTERNS:
                    m = pat.search(body)
                    if not m:
                        continue
                    # 检查否定前缀
                    ctx = body[max(0, m.start() - 4) : m.start()]
                    if any(neg in ctx for neg in _ALIVE_NEGATORS):
                        continue
                    fid = f"dead:{name}"
                    if fid in seen:
                        continue
                    seen.add(fid)
                    facts.append(Fact(fid, "dead", name, True, chapter=chapter, source=rec.layer))
                    break

            # 持有者（物品层）
            if rec.layer == "item":
                for pat in _OWNER_PATTERNS:
                    m = pat.search(body)
                    if not m:
                        continue
                    who = m.group("who")
                    fid = f"owner:{name}"
                    if fid in seen:
                        continue
                    seen.add(fid)
                    facts.append(
                        Fact(fid, "owner", name, who, chapter=chapter, source=rec.layer)
                    )
                    break

    return facts


# ==========================================================================
# 规则
# ==========================================================================
def _find_quotes(text: str, subject: str) -> list[str]:
    """找出包含主体的句子。"""
    if not subject:
        return []
    return [s for s in _sentences(text) if subject in s]


#: 需要双手/双臂协同的动作
_TWO_HAND_ACTIONS = ("双手", "两手", "双臂", "两腕")
#: 单手拔剑/挥砍类动作的模式
_HAND_ACTION = re.compile(r"(?P<part>" + "|".join(BODY_PARTS) + r")(?:握|持|抓|抬|举|拔|挥|握紧|按|拍)")


def _rule_alive(text: str, facts: Iterable[Fact]) -> list[Issue]:
    """R1 死而复生：已死角色出现在正文里并行动。"""
    out: list[Issue] = []
    for f in facts:
        if f.kind != "dead":
            continue
        if f.value is not True:
            continue
        for s in _find_quotes(text, f.subject):
            # 排除"回忆/尸体/遗蜕/墓/曾经"这类表述
            if any(w in s for w in ("遗蜕", "尸体", "遗体", "坟", "墓", "回忆", "想起", "曾经", "生前", "梦")):
                continue
            out.append(
                Issue(
                    "R1",
                    "死而复生",
                    f.subject,
                    s,
                    fact_id=f.id,
                    detail=f"{f.subject} 在设定中已死亡（{f.chapter or '未标注章节'}），正文中却有行动",
                )
            )
    return out


def _rule_injury(text: str, facts: Iterable[Fact]) -> list[Issue]:
    """R2 伤情违反：受伤部位做出需要完好的动作。"""
    out: list[Issue] = []
    injured_parts: dict[str, str] = {}
    for f in facts:
        if f.kind == "injury":
            injured_parts[str(f.value)] = f.id

    if not injured_parts:
        return out

    for s in _sentences(text):
        # 同一句里说"伤"是正常的（比如"忍着左手的痛"）
        if any(w in s for w in ("伤", "痛", "血", "绷带", "纱布", "废", "抖")):
            continue

        # 检查受伤部位是否在做动作
        for part, fid in injured_parts.items():
            if part not in s:
                continue
            m = re.search(re.escape(part) + r"(?:握|持|抓|抬|举|拔|挥|按|拍|推|拉|拖|拎|托)", s)
            if m:
                out.append(
                    Issue(
                        "R2",
                        "伤情违反",
                        "",
                        s,
                        fact_id=fid,
                        detail=f"{part} 有伤在身，正文却让它做「{m.group(0)}」这种需要完好的动作",
                    )
                )

        # 双手协同动作 vs 单手受伤
        if any(w in s for w in _TWO_HAND_ACTIONS):
            for part, fid in injured_parts.items():
                if part in ("左手", "右手", "左臂", "右臂", "左肩", "右肩"):
                    out.append(
                        Issue(
                            "R2",
                            "伤情违反",
                            "",
                            s,
                            fact_id=fid,
                            detail=f"{part} 有伤，正文却写「双手协同」动作",
                        )
                    )

    return out


def _rule_owner(text: str, facts: Iterable[Fact]) -> list[Issue]:
    """R3 持有者违反：物品在他人手上，正文让本角色直接使用。"""
    out: list[Issue] = []
    for f in facts:
        if f.kind != "owner":
            continue
        item = f.subject
        holder = str(f.value or "")
        if not item or not holder:
            continue
        for s in _sentences(text):
            if item not in s:
                continue
            # 句子里出现了持有者 → 正常（可能是转移动作，也可能就是他在用）
            if holder in s:
                continue
            # 任何转移/获得动作都不算违反
            if any(
                w in s
                for w in (
                    "交给", "递给", "转交", "还回", "归还", "交还", "递给",
                    "夺", "抢", "捡", "借", "收到", "接过", "取得", "得到",
                    "拿了", "拿来", "取走", "带走", "掉落", "收起",
                )
            ):
                continue
            out.append(
                Issue(
                    "R3",
                    "持有者违反",
                    item,
                    s,
                    fact_id=f.id,
                    detail=f"{item} 在设定中由 {holder} 持有，正文却没提到转移就使用了它",
                    severity="low",
                )
            )
    return out


_REALM_ORDER = (
    ("白牌", 1), ("绿牌", 2), ("蓝牌", 3), ("紫牌", 4), ("橙牌", 5), ("红牌", 6), ("黑牌", 7),
    ("斗之气", 1), ("斗者", 2), ("斗师", 3), ("大斗师", 4), ("斗灵", 5), ("斗王", 6),
    ("斗皇", 7), ("斗宗", 8), ("斗尊", 9), ("斗圣", 10), ("斗帝", 11),
)


def _realm_rank(text: str) -> tuple[int, str]:
    """在文本里找境界词并返回 (等级, 词)。找不到返回 (0, '')。"""
    best = (0, "")
    for word, rank in _REALM_ORDER:
        if word in text and rank > best[0]:
            best = (rank, word)
    return best


def _rule_realm(text: str, facts: Iterable[Fact]) -> list[Issue]:
    """R4 境界回退：正文把主体的境界说成比已确立更低的档次。

    关键约束：境界词必须与主体名**邻近**才能算作「说主体的境界」。
    否则「斗圣之威压下来，李四也退了半步」会被误判成
    「李四是斗圣」——句中斗圣是别人。
    """
    out: list[Issue] = []
    for f in facts:
        if f.kind != "realm":
            continue
        subject = f.subject
        established = _realm_rank(str(f.value or ""))
        if established[0] == 0:
            continue
        for s in _find_quotes(text, subject):
            if established[1] in s:
                continue  # 同一句里已出现正确境界，跳过
            got = _realm_rank(s)
            if not got[0]:
                continue
            if got[0] >= established[0]:
                continue
            # 境界词必须与主体名**紧邻**才算「说主体的境界」。
            # 「斗圣之威压下来，李四也退了半步」里斗圣是别人，
            # 主体与境界词隔着"之威压下来，"——这种就要排除。
            near = False
            for m in re.finditer(re.escape(got[1]), s):
                for sub in re.finditer(re.escape(subject), s):
                    gap = abs(m.start() - sub.start())
                    if gap <= len(subject) + 4:
                        near = True
                        break
                if near:
                    break
            if not near:
                continue
            out.append(
                Issue(
                    "R4",
                    "境界回退",
                    subject,
                    s,
                    fact_id=f.id,
                    detail=f"{subject} 已确立为「{established[1]}」，正文却说成更低的「{got[1]}」",
                )
            )
    return out


#: 地点后缀词——用来判断一个词像不像地名。
#: 这是启发式：只有看起来像地名时才做位置比对，
#: 否则「张三在家里」「张三在心里想」都会误报。
_PLACE_SUFFIX = (
    "城", "市", "县", "镇", "村", "区", "街", "路", "巷", "大道",
    "山", "峰", "岭", "崖", "谷", "林", "原", "野", "漠",
    "河", "江", "湖", "海", "湾", "溪", "泉", "潭",
    "岛", "州", "港", "关", "门", "桥", "塔", "楼", "阁", "殿", "宫", "寺", "庙",
    "院", "府", "宅", "营", "寨", "堡", "仓", "厂", "园", "场", "站", "厅", "室",
    "国", "界", "境", "域", "州",
)

#: 非地点的「在 X」惯用搭配，命中就跳过
_PLACE_STOP = (
    "心里", "心中", "眼里", "眼中", "手里", "手中", "口中", "嘴中",
    "面前", "身后", "身上", "旁边", "附近", "周围", "之后", "之前",
    "这时", "那时", "此刻", "当时", "现在", "现场", "本质上", "意义上",
)

#: 位移动词——出现这些说明角色在移动，位置变化合理，不报警
_MOVE_VERBS = (
    "来到", "走进", "走入", "进入", "进了", "抵达", "到达", "赶到", "赶往",
    "前往", "去了", "去了", "回到", "回到", "返回", "离开", "撤离", "逃往",
    "飞往", "传送", "瞬移", "冲进", "闯入", "踏入", "步入", "登上", "下了",
)


def _looks_like_place(word: str) -> bool:
    """粗判一个词像不像地名。"""
    if not word or len(word) < 2:
        return False
    if word in _PLACE_STOP:
        return False
    return any(word.endswith(s) for s in _PLACE_SUFFIX)


#: 方位后缀——「青云山上」要还原成「青云山」再判地名
_LOC_SUFFIX_RE = re.compile(
    r"(?:的上面|的下面|的里面|的外面|之中|之上|之下|附近|一带|周边|里头|外边|[上中里内外边处间面])$"
)


def _extract_places(text: str, subject: str) -> list[tuple[str, str]]:
    """抽取「某主体 + 在/位于 + 地点」的静态位置陈述。

    返回 [(地点, 原句片段)]。

    只认**静态**位置（在/位于/身处），不认位移动词——
    「张三走进大殿」是移动，不是"身处别处"。
    """
    out: list[tuple[str, str]] = []
    if not subject:
        return out

    # 位移句先剔除，避免把「来到X」当成「在X」
    for m in re.finditer(re.escape(subject) + r".{0,6}?在(?P<place>[^，。；！？、\n]{1,10})", text):
        head = m.group(0)
        if any(v in head for v in _MOVE_VERBS):
            continue
        place = m.group("place").strip()
        # 地点后面可能跟着助词/动词，截掉
        place = re.split(r"[的地得着了过]", place)[0].strip()
        # 「青云山上」→「青云山」，方位词不参与地名判定
        place = _LOC_SUFFIX_RE.sub("", place).strip()
        if not _looks_like_place(place):
            continue
        out.append((place, head))
    return out


def _rule_location(text: str, facts: Iterable[Fact]) -> list[Issue]:
    """R5 位置瞬移：正文把角色放在与已记录不同的地点，且没有位移过程。

    注意这是**启发式**规则，置信度低于 R1-R4：
      * 只在两个地点完全不像时报警（无共同字）
      * 只认「看起来像地名」的词
      * 只报 low，不阻断写作，供人复核
    """
    out: list[Issue] = []
    for f in facts:
        if f.kind != "location":
            continue
        subject = f.subject
        known = str(f.value or "").strip()
        if not _looks_like_place(known):
            continue
        for place, quote in _extract_places(text, subject):
            # 两个地名有共同字，视为同一地点或上下级关系，不报
            if set(place) & set(known):
                continue
            if place in known or known in place:
                continue
            out.append(
                Issue(
                    "R5",
                    "位置瞬移",
                    subject,
                    quote,
                    fact_id=f.id,
                    detail=(
                        f"{subject} 上次记录在「{known}」，"
                        f"正文这里出现在「{place}」，中间没有位移描述"
                    ),
                    severity="low",
                )
            )
    return out


def check_text(text: str, facts: Sequence[Fact], *, rules: Sequence[str] | None = None) -> CheckResult:
    """按规则检查正文。

    rules 为空则跑全部规则。

    两个「未执行」的情况会返回 ``ok=False`` + ``notice``，而不是伪装成通过：
      * 没有任何事实 —— 规则无从判定
      * 正文为空
    """
    body = str(text or "")
    if not body.strip():
        return CheckResult(
            False,
            facts_used=len(facts),
            rules_run=[],
            notice="正文为空，没有可检查的内容",
        )

    if not facts:
        return CheckResult(
            False,
            facts_used=0,
            rules_run=[],
            notice=(
                "这个作品库里没有可用于核对的设定事实，规则无法判定。\n"
                "补法二选一：\n"
                "  1) 用 enderwriter_writeback 写入 state（alive / injury / owner / realm / location）\n"
                "  2) 在设定正文里写明「左手受刀伤」「已死」「佩剑由某某保管」等可识别表述"
            ),
        )

    runners = {
        "R1": _rule_alive,
        "R2": _rule_injury,
        "R3": _rule_owner,
        "R4": _rule_realm,
        "R5": _rule_location,
    }
    wanted = list(rules) if rules else list(runners)

    issues: list[Issue] = []
    ran: list[str] = []
    for rid in wanted:
        fn = runners.get(rid)
        if fn is None:
            continue
        ran.append(rid)
        try:
            issues.extend(fn(body, facts))
        except Exception as exc:  # noqa: BLE001 - 单条规则出错不影响其他规则
            log.warning("规则 %s 执行失败：%s", rid, exc)

    # 去重：同一句同一规则只报一次
    seen: set[tuple[str, str, str]] = set()
    unique: list[Issue] = []
    for i in issues:
        key = (i.rule, i.quote, i.detail)
        if key in seen:
            continue
        seen.add(key)
        unique.append(i)

    return CheckResult(not unique, unique, facts_used=len(facts), rules_run=ran)
