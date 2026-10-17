"""示例数据：一部中文玄幻小说的「开局末影箱」。

用途：
  * ``enderwriter demo`` 的起步数据；
  * 单元测试的固定夹具（确定性：同样的调用永远得到同样的末影箱内容）。

覆盖七个格子（规范 §2）::

    character     4 人（2 core + 2 secondary）
    world         3 条设定（境界体系 / 地理 / 规则）
    timeline      3 个事件
    foreshadowing 3 条伏笔（planted / reinforced / resolved 各态齐全）
    item          3 件物品
    style         2 条风格样本
    alias         5 个称呼 -> 标准名映射
    图索引        6 条边
    状态索引      13 项硬事实（alive / realm / location / ...）
    章节正文      ch_001 / ch_002

自洽性约定（demo 的伏笔巡检与矛盾检测要依赖这些约定）：
  * 每条伏笔都满足 ``planted < planned_resolve``；已回收的 ``resolved_chapter`` 不早于 planted；
  * 角色状态与正文一致：王五 alive=False，正文里他只以「遗蜕／残念」形式出现；
  * 状态项都带 ``chapter_id``，便于 writeback 做时间倒错检测。
"""

from __future__ import annotations

from typing import Any

from . import common as C

__all__ = ["seed_demo", "SEED_FORESHAODWINGS", "SEED_ENTITIES"]

#: 供 LLM 后端做关键词/词表使用的实体清单
SEED_ENTITIES: tuple[str, ...] = (
    "张三",
    "李四",
    "赵六",
    "王五",
    "归墟",
    "示例卷",
    "青霄剑宗",
    "照骨灯",
    "断魂钉",
    "沉水镜",
    "月蚀",
    "噬灵",
)

# --------------------------------------------------------------------------
# 伏笔定义（单一数据源：seed 写入 + 文档引用都从这里来）
# --------------------------------------------------------------------------
SEED_FORESHAODWINGS: tuple[dict[str, Any], ...] = (
    {
        "id": "fs_001",
        "content": "照骨灯只在有人以真名起誓时才会亮起——张三不知自己已立下何等誓言。",
        "status": "reinforced",
        "planted_chapter": "ch_002",
        "planned_resolve": "ch_012",
        "reinforced_chapters": ["ch_003"],
    },
    {
        "id": "fs_002",
        "content": "青霄剑宗封山百年，山门石阶上却每年多出一道新刻痕；谢无咎称那是“记名”，记的是一笔尚未抵清的旧债。",
        "status": "planted",
        "planted_chapter": "ch_002",
        "planned_resolve": "ch_020",
    },
    {
        "id": "fs_003",
        "content": "王五坐化前在示例卷留下半盏残灯，灯芯以自身神魂为引，足以在月蚀之夜照见回溯。",
        "status": "resolved",
        "planted_chapter": "ch_001",
        "planned_resolve": "ch_006",
        "resolved_chapter": "ch_006",
    },
)

# --------------------------------------------------------------------------
# 章节正文
# --------------------------------------------------------------------------
_CH001_TITLE = "第一章 归墟旧灯"
_CH001_SUMMARY = "张三在归墟古战场的示例卷下捡到一盏会说话的残灯，照骨灯第一次为他亮起。"
_CH001_TEXT = (
    "归墟的夜比别处更黑。\n"
    "张三伏在示例卷的乱石间，指尖被冷铁割开一道口子，血珠还没落地就被风卷走了。"
    "他已经在青霄剑宗的山门外守了七日，等一个叫李四的女子，也等一句能让他活下去的话。\n"
    "崖底深处有光。不是月光，那光极细，像一根被人从喉咙里抽出来的线。\n"
    "他顺着光摸下去，摸到一盏残灯。灯身斑驳，灯壁上刻着两个古字：照骨。\n"
    "“别碰。”一个声音在灯里说，“碰了，你就欠我一笔债。”\n"
    "张三笑了一声。他欠的债已经够多了，再多一笔也无妨。\n"
    "他伸手握住灯座的那一刻，灯芯忽然烧了起来，火是青白色的，不烫手。"
    "光落在他的骨相上，照出一段他自己从未见过的影子——那影子在替他起誓。\n"
    "崖顶传来李四的声音：“张三，你在下面做什么？”\n"
    "他抬起头，看见月光被撕开了一道缝。那不是月蚀，却有东西在月蚀的位置上睁开了眼。"
    "他收紧指节，把残灯按进怀里，低声说：“我找到答案了。”\n"
    "答案没有回答他。灯里的声音只是一遍遍地重复：“王五，你欠我的，该还了。”"
)

_CH002_TITLE = "第二章 月蚀之约"
_CH002_SUMMARY = "李四与张三立下月蚀之约；青霄剑宗封山真相初露，断魂钉与沉水镜是进出剑宗的凭物。"
_CH002_TEXT = (
    "李四是踏着霜来的。\n"
    "她没有问张三为何一身血，只把一枚断魂钉按在他掌心："
    "“青霄剑宗封山百年，山门石阶上每年多出的一道刻痕，是谢无咎的记账。”\n"
    "张三盯着那枚断魂钉。钉身上有一圈焦痕，和三年前那场大火留在别处的一模一样。\n"
    "“你要我替你进山。”他说。\n"
    "“我要你活着回来。”李四说，“月蚀之夜，沉水镜会照出百年前那场死的真相。"
    "在那之前，谁都不许死。”\n"
    "他们在示例卷边立约。约定很简单：以真名起誓，一人不归，另一人代其履约。\n"
    "风停了。怀里的照骨灯忽然亮起，青白色的火舌舔过两人的影子，"
    "光里的影子各自多了一笔，像有人替他们补上了名字。\n"
    "李四神色微变：“照骨灯只在有人以真名起誓时才会亮。你刚才说的是哪三个字？”\n"
    "张三沉默良久。他说出口的名字，他自己都不确定是不是自己的。\n"
    "远处青霄剑宗封山的石阶亮起一线微光。谢无咎立在阶上，望向归墟方向，"
    "淡淡说了一句：“记账的人来了。”\n"
    "崖风重新卷起来的时候，张三听见灯里的声音在笑。那笑声很轻，"
    "像有人把一枚钉子，慢慢按进另一个人的掌心里。\n"
    "这一夜，月还没有蚀，山门外的石阶上却先刻下了一道新痕。"
)

# --------------------------------------------------------------------------
# 各格子数据
# --------------------------------------------------------------------------
_CHARACTERS: tuple[dict[str, Any], ...] = (
    {
        "name": "张三",
        "content": "张三，青霄剑宗第八代记名弟子，身负噬灵之体：所修之力来路不明，越强越易被吞噬。"
        "外表沉静寡言，实则极度偏执，认定一切都能被交换。"
        "在本作开局时，他尚未知晓自己早已与一位死去之人互换过名字。",
        "data": {"role": "protagonist", "importance": "core", "origin": "归墟"},
    },
    {
        "name": "李四",
        "content": "李四，示例卷下守灯人，先天能以骨相辨真伪，因此从不说谎，也不听别人说谎。"
        "她与青霄剑宗有旧怨：宗门封山百年，封的正是她师父的“罪”。"
        "行事冷静、狠得下心，唯独对张三留了一线余地。",
        "data": {"role": "deuteragonist", "importance": "core", "origin": "示例卷"},
    },
    {
        "name": "赵六",
        "content": "赵六，青霄剑宗现任首徒，李四的师兄，剑术冠绝同辈，却极少在宴席上说话。"
        "他每年亲手在山门石阶上刻下一道痕，谁也不问为什么。",
        "data": {"role": "supporting", "importance": "secondary", "origin": "青霄剑宗"},
    },
    {
        "name": "王五",
        "content": "王五，张三的师尊，百年前死于归墟一场大火，尸骨无存，只余一具虚幻遗蜕守在示例卷。"
        "他曾在临终前把半盏残灯留给后来者，灯芯以自身神魂为引。",
        "data": {"role": "mentor", "importance": "secondary", "origin": "归墟", "deceased": True},
    },
)

_WORLD: tuple[dict[str, Any], ...] = (
    {
        "name": "境界体系",
        "content": "此界修行分九境：凝气、通灵、照骨、化脉、炼神、归墟、窥天、执灯、无咎。"
        "每一境都有对应的“骨相”，照骨灯可照人骨相，判断真伪境界；"
        "越过照骨境，需以真名起誓，否则会在月蚀之夜反噬。",
        "data": {"kind": "rule", "tags": ["修行", "境界"]},
    },
    {
        "name": "归墟古战场",
        "content": "归墟是百年前那场宗门大战的旧战场，地脉被烧断，终年无星。"
        "示例卷在归墟北侧，崖底埋着无数无名者的遗蜕与残灯；城中人忌讳夜里点灯，"
        "因灯一亮，便会照亮不该被照亮的东西。",
        "data": {"kind": "place", "tags": ["地理", "归墟"]},
    },
    {
        "name": "青霄剑宗",
        "content": "青霄剑宗立宗千年，百年前封山，至今不再收徒，只在每年月蚀之夜开一线山门。"
        "进出宗门需凭断魂钉与沉水镜；石阶共九百九十九级，阶上每一道刻痕都对应一笔旧债。",
        "data": {"kind": "faction", "tags": ["宗门", "青霄剑宗"]},
    },
)

_TIMELINE: tuple[dict[str, Any], ...] = (
    {
        "name": "百年前·归墟大火",
        "content": "百年前，青霄剑宗在归墟围剿一脉禁忌传承，一场大火烧了七日，王五死于其中，尸骨无存。",
        "data": {"at": "百年前", "chapter": "ch_001", "kind": "backstory"},
    },
    {
        "name": "三年前·宗门封山",
        "content": "三年前，青霄剑宗宣布封山百年，山门不再收徒；同年张三被记名入宗，成为第八代记名弟子。",
        "data": {"at": "三年前", "chapter": "ch_002", "kind": "backstory"},
    },
    {
        "name": "当下·月蚀之约",
        "content": "当下，李四与张三在示例卷以真名起誓，约定月蚀之夜借沉水镜照见百年前的真相。",
        "data": {"at": "当下", "chapter": "ch_002", "kind": "event"},
    },
)

_ITEMS: tuple[dict[str, Any], ...] = (
    {
        "name": "照骨灯",
        "content": "照骨灯，旧灯一盏，灯身刻古字“照骨”，灯芯以王五残魂为引。"
        "能照人骨相、辨真伪境界；只在有人以真名起誓时自动亮起。现由张三随身携带。",
        "data": {"owner": "张三", "importance": "core"},
    },
    {
        "name": "断魂钉",
        "content": "断魂钉，进出青霄剑宗的凭物之一。钉身一圈焦痕源自百年前归墟那场大火，"
        "持钉者可越过石阶而不被记名。现由李四持有后转交张三。",
        "data": {"owner": "李四", "importance": "core"},
    },
    {
        "name": "沉水镜",
        "content": "沉水镜，青霄剑宗的封山信物之一，需交由二人共持方能在月蚀之夜开启，"
        "映出百年前那场死的真相。镜子至今仍在宗门深处，由赵六保管。",
        "data": {"owner": "赵六", "importance": "secondary"},
    },
)

_STYLE: tuple[dict[str, Any], ...] = (
    {
        "name": "短句留白",
        "content": "节奏样本：多用四至十字短句，句尾留白，不写心理活动，只写动作与物件。"
        "示例：他把残灯按进怀里。风停了。远处的钟声敲到第三下。",
        "data": {"kind": "rhythm"},
    },
    {
        "name": "器物叙事",
        "content": "意象样本：以“灯/钉/镜”这类器物承载信息与情绪，每章至少让一件器物发生变化，"
        "并让变化可被回读。",
        "data": {"kind": "imagery"},
    },
)

#: 称呼格：别名 -> 标准名
_ALIASES: tuple[tuple[str, str], ...] = (
    ("沈师弟", "张三"),
    ("小砚", "张三"),
    ("守灯人", "李四"),
    ("苏姑娘", "李四"),
    ("谢无咎", "赵六"),
)

#: 图索引：(src, rel, dst, chapter_id)
_EDGES: tuple[tuple[str, str, str, str], ...] = (
    ("张三", "师尊", "王五", "ch_001"),
    ("李四", "师妹", "赵六", "ch_002"),
    ("张三", "同盟", "李四", "ch_002"),
    ("赵六", "掌管", "沉水镜", "ch_002"),
    ("张三", "持有", "照骨灯", "ch_001"),
    ("王五", "出身", "青霄剑宗", "ch_001"),
)

#: 状态索引：(entity, key, value, chapter_id)
_STATES: tuple[tuple[str, str, Any, str], ...] = (
    ("张三", "alive", True, "ch_002"),
    ("张三", "realm", "照骨境", "ch_002"),
    ("张三", "location", "示例卷", "ch_002"),
    ("张三", "holding", "照骨灯", "ch_002"),
    ("李四", "alive", True, "ch_002"),
    ("李四", "realm", "通灵境", "ch_002"),
    ("李四", "location", "示例卷", "ch_002"),
    ("赵六", "alive", True, "ch_002"),
    ("赵六", "realm", "炼神境", "ch_002"),
    ("赵六", "location", "青霄剑宗", "ch_002"),
    ("王五", "alive", False, "ch_001"),
    ("王五", "realm", "归墟境", "ch_001"),
    ("王五", "location", "示例卷", "ch_001"),
)


# ==========================================================================
# 对外入口
# ==========================================================================
def seed_demo(chest: Any) -> dict:
    """往末影箱灌入示例开局数据，返回写入统计 dict。

    统计结构::

        {
          "records": {layer: n, ...},   # 每个格子写入条数（与 chest.stats() 口径一致）
          "aliases": n, "edges": n, "states": n, "chapters": [id, ...],
          "foreshadowings": {status: n}, "entities": [...],
        }

    :param chest: ``enderwriter.chest.store.EnderChest`` 实例
    :raises TypeError: chest 为 None
    """
    if chest is None:
        raise TypeError("seed_demo(chest) 需要一个 EnderChest 实例")

    stats: dict[str, Any] = {
        "records": {layer: 0 for layer in C.LAYERS},
        "aliases": 0,
        "edges": 0,
        "states": 0,
        "chapters": [],
        "foreshadowings": {},
        "entities": [],
    }

    # --- 0) 称呼格必须先建：后续 upsert/state/edge 都走 resolve_entity ----
    for alias, canonical in _ALIASES:
        chest.link_alias(alias, canonical)
        stats["aliases"] += 1

    # --- 1) 六个记忆格 ----------------------------------------------------
    for layer, rows in (
        ("character", _CHARACTERS),
        ("world", _WORLD),
        ("timeline", _TIMELINE),
        ("item", _ITEMS),
        ("style", _STYLE),
    ):
        for row in rows:
            chest.upsert(
                layer,
                row["name"],
                row["content"],
                data=dict(row.get("data") or {}),
                chapter_id=str((row.get("data") or {}).get("chapter") or "ch_001"),
            )
            stats["records"][layer] += 1

    for fs in SEED_FORESHAODWINGS:
        chest.set_foreshadow(
            fs["id"],
            fs["content"],
            status=fs["status"],
            planted_chapter=fs["planted_chapter"],
            planned_resolve=fs["planned_resolve"],
            reinforced_chapters=fs.get("reinforced_chapters"),
            resolved_chapter=fs.get("resolved_chapter"),
        )
        stats["records"]["foreshadowing"] += 1
        stats["foreshadowings"][fs["status"]] = stats["foreshadowings"].get(fs["status"], 0) + 1

    # --- 2) 图索引 --------------------------------------------------------
    for src, rel, dst, chapter_id in _EDGES:
        chest.add_edge(src, rel, dst, chapter_id=chapter_id)
        stats["edges"] += 1

    # --- 3) 状态索引 ------------------------------------------------------
    for entity, key, value, chapter_id in _STATES:
        chest.set_state(entity, key, value, chapter_id=chapter_id)
        stats["states"] += 1

    # --- 4) 章节正文 ------------------------------------------------------
    for chapter_id, title, text, summary in (
        ("ch_001", _CH001_TITLE, _CH001_TEXT, _CH001_SUMMARY),
        ("ch_002", _CH002_TITLE, _CH002_TEXT, _CH002_SUMMARY),
    ):
        chest.put_chapter(chapter_id, text, title=title, summary=summary)
        stats["chapters"].append(chapter_id)

    chest.commit()

    stats["entities"] = [str(name) for name in chest.all_entities()]
    stats["total_records"] = sum(stats["records"].values())
    return stats


def seed_entities() -> list[str]:
    """示例故事里全部标准实体名（供 ``StubBackend(vocabulary=...)`` 使用）。"""
    return list(C.dedupe(SEED_ENTITIES))
