# EnderWriter 代码说明

> 配套文档：[STRUCTURE.md](STRUCTURE.md)（结构与原理）
> 本文聚焦**代码怎么写的**：关键算法、数据结构、调用链、扩展点
> 生成时间：2026-10-17

---

## 目录

1. [模块总览与依赖关系](#一模块总览与依赖关系)
2. [冻结契约层 common.py](#二冻结契约层-commonpy)
3. [中文处理 lexicon.py](#三中文处理-lexiconpy)
4. [向量 embed.py](#四向量-embedpy)
5. [末影箱 chest/store.py](#五末影箱-cheststorepy)
6. [分层记忆 memory.py](#六分层记忆-memorypy)
7. [检索 retrieval.py](#七检索-retrievalpy)
8. [编排 orchestrator.py](#八编排-orchestratorpy)
9. [回写 writeback.py](#九回写-writebackpy)
10. [模型层 llm.py / llamacpp.py](#十模型层-llmpy--llamacpppy)
11. [JSON 桥 bridge.py](#十一json-桥-bridgepy)
12. [DSH 插件 index.js / client.js](#十二dsh-插件-indexjs--clientjs)
13. [测试策略](#十三测试策略)
14. [扩展指引](#十四扩展指引)

---

## 一、模块总览与依赖关系

```
                    ┌──────────┐
                    │ common   │  冻结契约：Schema / token / ID
                    └────┬─────┘
          ┌──────────────┼──────────────┐
          ▼              ▼              ▼
     ┌────────┐    ┌─────────┐    ┌────────┐
     │lexicon │    │  embed  │    │memory  │
     └───┬────┘    └────┬────┘    └───┬────┘
         │              │             │
         └──────┬───────┴─────────────┘
                ▼
         ┌─────────────┐
         │chest/store  │  末影箱：SQLite 9 表
         └──────┬──────┘
                ▼
         ┌─────────────┐
         │ retrieval   │  三档分层检索
         └──────┬──────┘
                ▼
         ┌─────────────┐
         │orchestrator │  P0-P4 组装
         └──────┬──────┘
                ▼
         ┌─────────────┐      ┌────────┐
         │  writeback  │      │  llm   │
         └──────┬──────┘      └───┬────┘
                └────────┬────────┘
                         ▼
                  ┌─────────────┐
                  │   bridge    │  给插件用的 JSON 入口
                  └─────────────┘
```

**依赖规则**：只能向下依赖。`common` 不依赖任何业务模块；`chest` 依赖 `lexicon`/`embed`；`retrieval` 依赖 `chest`；`orchestrator` 依赖 `retrieval`。

**唯一第三方依赖**：`numpy`（向量运算）。其余全是标准库。

---

## 二、冻结契约层 common.py

515 行。**所有模块只依赖它，不允许它依赖别的模块。**

### 2.1 token 估算

```python
TOKEN_RATIO_ASCII = 0.25
_CJK_RANGES = (...)          # 7 个 CJK 区段

def _is_wide(ch: str) -> bool:
    """是否宽字符（CJK），宽字符按 1 token，其余非空白按 0.25"""

def est_tokens(text: str) -> int:
    """估算 token 数：CJK ≈ 1/字，其他非空白 +0.25，向上取整"""

def clip_tokens(text: str, budget: int, suffix: str = "…") -> str:
    """按 token 预算截断，超出加省略号"""
```

不引入 `tiktoken`（体积大且需下载词表），用确定性估算。对中文场景误差可接受。

### 2.2 ID 与章节号

```python
def make_id(prefix: str, n: int, width: int = 3) -> str:
    """fs_007 / ch_042"""

_CH_RE = re.compile(r"(\d+)")

def chapter_index(chapter_id) -> int:
    """ch_042 -> 42；无法解析返回 -1"""
```

章节号是零填充的，所以字符串比较等价于数值比较（`ch_007 < ch_042`）。**代价**：超过 `ch_999` 会失效。

### 2.3 JSON 工具

```python
def dumps(obj) -> str:  # ensure_ascii=False, sort_keys=True, 紧凑分隔符
def loads(s, default=None):
```

`sort_keys=True` 让同样的数据总是产生同样的 JSON 字节，便于比对和缓存。

### 2.4 最小 JSON-Schema 校验器

不引入 `jsonschema` 库，手写一个够用的：

```python
class SchemaError(ValueError): ...

def _type_ok(value, t) -> bool          # object/array/string/integer/number/boolean/null
def _resolve_ref(ref: str, root: dict) -> dict   # 支持 #/definitions/xxx 与 ~0 ~1 转义
def _validate(value, sch, path, errors, root=None) -> None
def validate(value, schema) -> Any      # 失败抛 SchemaError（含全部错误）
```

支持的关键字：`type` `const` `enum` `oneOf` `anyOf` `minLength` `pattern`
`minimum` `maximum` `minItems` `maxItems` `items` `required` `properties`
`additionalProperties` `$ref`

**`root` 参数**贯穿每次递归——因为 `memory_result` 用了 `#/definitions/hit` 引用，没有 root 就解析不了 `$ref`。这是最初实现遗漏后补上的。

### 2.5 四份 Schema

```python
SCHEMAS = {
  "chapter_outline":     {...},   # §3.1 九个必填字段
  "search_memory_tool":  {...},   # §3.2 entities(minItems 1) / budget
  "memory_result":       {...},   # §3.3 high/medium/low + $ref hit
  "writeback_report":    {...},   # §3.5 chapter_id/updated/warnings
}
def schema(name) -> dict
def export_schemas(dest_dir) -> list[str]   # 导出到 schemas/*.json
```

### 2.6 名称归一化

```python
def normalize_name(name: str) -> str:
    """NFKC + strip + lower + 去空白。用于实体比对"""
```

---

## 三、中文处理 lexicon.py

87 行。**为什么不用 jieba**：SQLite FTS5 无法切中文，jieba 是重依赖（~20MB 词典）。

### 3.1 正则切分

```python
_LATIN   = re.compile(r"[A-Za-z][A-Za-z0-9_'\-]*")
_CJK_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")
_DIGITS  = re.compile(r"\d+")
```

### 3.2 双字组方案

```python
def bigrams(run: str) -> list[str]:
    """CJK 连续串 -> 词项列表。
    长度 1：去虚词后保留单字
    长度 2~4：同时保留整串 + 所有 bigram
    长度 >4：只保留所有 bigram
    """
    n = len(run)
    if n == 1:
        return [] if run in ZH_STOP else [run]
    out = []
    if n <= 4:
        out.append(run)          # 整串（提高长词区分度）
    out.extend(run[i : i + 2] for i in range(n - 1))
    return out
```

示例：

```
"萧炎走进了宗门"
  → 萧炎、炎走、走进、进宗、宗门
```

### 3.3 分析入口

```python
def analyze(text: str) -> list[str]:
    """NFKC 归一 → CJK 双字组 + 拉丁词 + 数字串。保留重复用于计频"""
```

**关键约束：索引侧与查询侧必须共用同一个 `analyze()`**，否则索引和查询的词项对不上。

### 3.4 权重

```python
def term_weights(text, *, entity="", title="") -> dict[str, float]:
    """实体名 3.0 / 标题 2.0 / 正文 1.0
    三处都出现则累加，所以主角名在正文里出现很多次会权重很高
    """
```

### 3.5 实体抽取

```python
def extract_entities(text: str, vocab) -> list[str]:
    """从文本里找出已知实体。最长优先（避免"沈"先匹配掉"张三"），
    命中后把该片段替换成空格防止重叠，最后按首次出现位置排序
    """
```

按出现位置排序这点很重要——**动态预取时要先处理先出现的实体**。

---

## 四、向量 embed.py

95 行。**为什么用哈希向量**：离线可复现，无需下载模型；`upsert(embedding=vec)` 可注入真实嵌入。

```python
DIM = 256
EMBED_VERSION = 1

def _bucket(term: str) -> int:
    """blake2b(term, digest_size=8) -> struct.unpack('<Q') -> % DIM"""

def embed(text: str) -> np.ndarray:
    """文本 -> float32[DIM] 单位向量，空文本返回零向量"""
    vec = np.zeros(DIM, dtype=np.float32)
    terms = analyze(text)
    counts = {}                      # 词频
    for t in terms:
        counts[t] = counts.get(t, 0) + 1
    for term, tf in counts.items():
        vec[_bucket(term)] += 1.0 + float(np.log(tf))   # 1+log(tf) 抑制高频词
    norm = np.linalg.norm(vec)
    if norm > 0:
        vec /= norm                  # L2 归一，后续用点积当余弦
    return vec
```

### 为什么是哈希而不是训练模型

| | 哈希向量 | 真实嵌入 |
|---|---|---|
| 语义相似 | 弱（只有词形重合） | 强 |
| 离线可复现 | ✅ | 需模型文件 |
| 测试确定性 | ✅ | 需固定随机种 |
| 依赖 | numpy | numpy + 模型 |

对"末影箱检索"这个场景，**关键词通道已经覆盖大部分需求**，向量通道主要做补充。真实使用时可以用 `upsert(embedding=真实向量)` 替换。

### Blob 存储

```python
def to_blob(vec) -> bytes:              # float32 原始字节
def from_blob(blob, dim=DIM) -> np.ndarray   # 长度不符时裁剪/补零（兼容维度升级）
def cosine(a, b) -> float               # 任一为零向量返回 0.0
def cosine_matrix(query[1,D], mat[N,D]) -> np.ndarray[N]   # 批量
```

---

## 五、末影箱 chest/store.py

904 行，最大的单文件。

### 5.1 表结构（9 张）

```sql
meta(key PK, value)

records(layer, entity, content, data DEFAULT '{}', version DEFAULT 1,
        last_updated, chapter_id, embedding BLOB,
        PRIMARY KEY(layer, entity))

history(id AUTOINCREMENT, layer, entity, version, content, data,
        chapter_id, updated_at, change DEFAULT 'update')
  INDEX idx_history(layer, entity, version)

terms(term, layer, entity, weight REAL DEFAULT 1.0,
      PRIMARY KEY(term, layer, entity))
  INDEX idx_terms_term(term)

aliases(alias PK, canonical)
  INDEX idx_aliases_canon(canonical)

edges(src, rel, dst, chapter_id, data, PRIMARY KEY(src, rel, dst))
  INDEX idx_edges_src(src) / idx_edges_dst(dst)

state(entity, key, value, chapter_id, version DEFAULT 1, updated_at,
      PRIMARY KEY(entity, key))

chapters(chapter_id PK, title '', text '', summary '', word_count 0, created_at)
```

### 5.2 写入：upsert 与版本

```python
def upsert(self, layer, entity, content, *, data=None, chapter_id=None,
           keywords=None, embedding=None, change="update", commit=True) -> Record:
    """已存在则 version+1，旧值留在 history"""
    if layer not in LAYERS:
        raise ValueError(f"未知格子 {layer!r}")

    # 非 alias 层先做别名归一化：写"炎帝"实际落到"萧炎"
    entity = str(entity) if layer == "alias" else self.resolve_entity(entity)

    # 向量：实体名 + 正文
    vec = embedding if embedding is not None else _embed.embed(f"{entity} {content}")

    row = self.conn.execute(
        "SELECT version FROM records WHERE layer=? AND entity=?", (layer, entity)
    ).fetchone()
    version = (int(row["version"]) + 1) if row else 1

    self.conn.execute("""
        INSERT INTO records(...) VALUES(...)
        ON CONFLICT(layer, entity) DO UPDATE SET
            content=excluded.content, data=excluded.data, version=excluded.version,
            last_updated=excluded.last_updated, chapter_id=excluded.chapter_id,
            embedding=excluded.embedding
    """, (...))

    self.conn.execute("INSERT INTO history(...) VALUES(...)", (...))   # 追加历史
    self._index_terms(layer, entity, content, keywords, data)           # 重建倒排
```

**只追加不覆盖**：旧版本永远留在 `history` 表，这是"按章回滚"的数据基础。

### 5.3 关键词索引构建

```python
def _index_terms(self, layer, entity, content, keywords, data):
    """删掉旧词项，按 term_weights 重建
    权重：正文 1.0 / 实体名 3.0 / 标题 2.0 / 额外关键词 ×2
    """
```

### 5.4 关键词检索

```python
def search_terms(self, terms, *, layers=None, limit=50) -> list[tuple[Record, float]]:
    """打分 = 词权重和 × IDF / 长度归一"""
    rows = conn.execute("""
        SELECT layer, entity, SUM(weight) AS score
        FROM terms WHERE term IN (...) AND layer IN (...)
        GROUP BY layer, entity
    """).fetchall()

    total = COUNT(*) FROM records
    for r in rows:
        raw = float(r["score"])
        idf = 1.0 + log(total / (1.0 + raw))
        score = raw * idf / (1.0 + 0.01 * len(content))    # 长度归一，避免长文占优
```

### 5.5 向量检索

```python
def search_vector(self, query, *, layers=None, limit=50):
    """取所有 embedding，numpy 批量余弦，取 top-k，跳过 ≤0 的"""
    cv = embed(query)                      # 或直接传向量
    mat = np.vstack([from_blob(b) for b in embeddings])
    sims = cosine_matrix(cv.reshape(1, -1), mat)
    for idx in np.argsort(-sims)[:limit]:
        if sims[idx] <= 0: break
```

没有 `sqlite_vec`，用 numpy 暴力算。数据量在万级以内没问题。

### 5.6 图检索（BFS）

```python
def search_graph(self, entities, *, hops=1, layers=None, limit=50):
    """从种子实体 BFS 扩展，距离越远分越低"""
    scores = {}
    for seed in seeds:
        root = self.resolve_entity(seed)
        for r in self.get_any(root, ls):
            scores[(r.layer, r.entity)] = 1.0        # 种子本身 1.0
        frontier, seen = {root}, {root}
        for depth in range(1, hops + 1):
            nxt = set()
            for node in frontier:
                for s, _rel, d in self.neighbors(node, hops=1):
                    for other in (s, d):
                        if other in seen: continue
                        seen.add(other); nxt.add(other)
                        for r in self.get_any(other, ls):
                            scores[(r.layer, r.entity)] = max(..., 1.0 / (depth + 1))
            frontier = nxt
```

深度衰减：1 跳 0.5，2 跳 0.33，3 跳 0.25。

### 5.7 三路融合

```python
def search(self, query, entities=(), *, layers=None, limit=50):
    """同一记录取最大分，并记录命中通道数，多通道加分"""
    merged = {}

    def add(pairs):
        for rec, sc in pairs:
            key = (rec.layer, rec.entity)
            if key not in merged:
                merged[key] = (rec, sc, 1)
            else:
                old = merged[key]
                merged[key] = (old[0], max(old[1], sc), old[2] + 1)   # 通道数 +1

    add(self.search_terms(analyze(query), ...))
    add(self.search_vector(query, ...))
    if entities:
        add(self.search_graph(entities, ...))

    # 多通道命中 → 加分 15%/(n-1)
    out = [(rec, sc * (1.0 + 0.15 * (n - 1))) for rec, sc, n in merged.values()]
```

**为什么多通道加分**：同时被关键词、向量、图命中的记录，比只被一个通道命中的更可信。

### 5.8 伏笔状态机

```python
def set_foreshadow(self, fs_id, content, *, status="planted", planted_chapter=None,
                   planned_resolve=None, reinforced_chapters=None,
                   resolved_chapter=None, extra=None):
    if status not in ("planted", "reinforced", "resolved", "abandoned"):
        raise ValueError(f"非法伏笔状态：{status!r}")

    data = {}
    if extra:
        data.update(extra)          # 先铺旧快照
    data.update({                    # 再让显式参数覆盖
        "status": status,
        "planted_chapter": planted_chapter,
        "planned_resolve": planned_resolve,
        "reinforced_chapters": chapters,
        "resolved_chapter": resolved_chapter,
    })
```

**这里的顺序曾经写反过**，导致 `reinforce_foreshadow()` 返回值正确但落盘丢失 `reinforced_chapters`——因为 `extra` 带着旧值把新值覆盖了。顺序必须是"先 extra，后显式参数"。

### 5.9 称呼格

```python
def link_alias(self, alias, canonical): ...
def resolve_entity(self, name) -> str:
    """别名 → 标准名；未登记则返回原名"""
def synonyms(self, entity) -> list[str]:
    """反查某标准名的所有别名"""
```

`upsert` 时自动调用 `resolve_entity`，所以写"炎帝"会落到"萧炎"。

---

## 六、分层记忆 memory.py

227 行。

### 6.1 两层常量

```python
SCOPE_WORK = "work"
SCOPE_GLOBAL = "global"

GLOBAL_LAYERS = frozenset({"style"})                          # 进全局库
WORK_LAYERS   = frozenset(set(LAYERS) - GLOBAL_LAYERS)         # 进作品库
```

### 6.2 路由

```python
def route(layer: str, *, scope: str | None = None) -> str:
    """显式 scope 优先；否则 style→global，其余→work"""
    if scope in (SCOPE_WORK, SCOPE_GLOBAL):
        return scope
    return SCOPE_GLOBAL if layer == "style" else SCOPE_WORK
```

### 6.3 会话命名

```python
SESSION_PREFIX = "对话-"

def session_work(session_id: str) -> str:
    """取会话 ID 里末 8 位数字字母"""
    short = re.sub(r"[^0-9a-zA-Z]", "", session_id)[-8:] or "default"
    return f"{SESSION_PREFIX}{short}"
```

```
session-f9e25f1f-2111-4142-a784-5b9199017a6c
  → 去掉非字母数字: f9e25f1f21114142a7845b9199017a6c
  → 末 8 位: 99017a6c
  → "对话-99017a6c"
```

### 6.4 作品解析

```python
def resolve_work(*, work=None, session_id=None) -> str:
    """优先级：显式 work > 会话专属 > 空（只用全局库）"""
    explicit = (work or "").strip()
    if explicit:
        return explicit
    return session_work(session_id or "")
```

### 6.5 文件命名

```python
def slugify(name: str, *, max_len=40) -> str:
    """中文保留，只替换路径非法字符；含中文时追加 3 字节哈希防撞名"""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    if _CJK_RE.search(cleaned):
        digest = blake2b(name.encode()).hexdigest()[:6]
        return f"{cleaned}-{digest}"
    return cleaned
```

结果：`对话-99017a6c` → `对话-99017a6c-cc9767.db`

### 6.6 WorkMemory

```python
class WorkMemory:
    def __init__(self, work=None, *, home=None):
        self.home = home or default_home()      # EW_HOME 或 <仓库>/memory

    @property
    def work_path(self):    return home/works/{slug(work)}.db
    @property
    def global_path(self):  return home/global.db

    def open(self, scope=SCOPE_WORK):   # 惰性建目录 + EnderChest
    def work_chest(self) / global_chest(self)
    def exists(self, scope) -> bool
```

---

## 七、检索 retrieval.py

639 行。

### 7.1 常量

```python
BAND_RATIOS = {"high": 0.50, "medium": 0.35, "low": 0.15}
DEFAULT_BUDGET = 800
_MEDIUM_FLOOR = 0.25      # 进 medium 的最低分
_WEAK_FLOOR = 0.28        # 进 low 的最低分
```

### 7.2 主流程

```python
def search(self, req: dict, *, context_entities=(), seen_low=None) -> dict:
    """1. 校验入参（§3.2 Schema）
       2. 实体归一化（别名 → 标准名）
       3. 多通道召回
       4. 分档 classify
       5. 预算分配 allocate
       6. 渲染 render
       7. 校验出参（§3.3 Schema）
    """
```

### 7.3 分档规则

```python
def _classify(self, candidates, ...):
    """high   = 多通道命中（channels >= 2）或实体精确命中
       medium = 单通道且分数 >= _MEDIUM_FLOOR
       low    = 弱相关，分数 >= _WEAK_FLOOR，最多抽 1-2 条
    """
```

### 7.4 预算分配

```python
def _allocate(self, bands, budget):
    """按 50/35/15 分配，高优先级优先占位
       某档用不满的额度顺延给下一档
    """
```

### 7.5 两个优化

**in_context 去重**

```python
def rendered_cost(hit: Hit) -> int:
    """已遮掉的正文按 4 token（"已在上下文"）计费，
    否则被隐藏的正文会把预算吃掉一遍，优化反而更贵"""
```

**low 档轮转**

```python
def _apply_seen_low(self, seen_low, picks):
    """同一写作周期内不重复抽同一条 low。
    候选充足时抽全新条目；耗尽时按位次轮转"""
```

### 7.6 便捷函数

```python
def search_memory(chest, req, *, context_entities=()) -> dict:
    """建临时 MemoryKeeper 调用 search()"""
```

---

## 八、编排 orchestrator.py

1291 行。

### 8.1 预算常量

```python
PRIORITIES = ("P0", "P1", "P2", "P3", "P4")
BUDGET_RATIOS = {"P0": 0.30, "P1": 0.30, "P2": 0.20, "P3": 0.15, "P4": 0.05}
CONTEXT_RATIO = 0.70           # 章节上下文 = 窗口 70%
WINDOW_TOKENS_DEFAULT = 8000
```

### 8.2 预算切分（P4 吸收浮点残差）

```python
def _budgets(self, total):
    budgets = {}
    for pri in PRIORITIES[:-1]:
        budgets[pri] = int(total * BUDGET_RATIOS[pri])
    budgets["P4"] = total - sum(budgets.values())   # 残差兜底，保证加总严格等于 total
    return budgets
```

### 8.3 细纲读取兼容

```python
@staticmethod
def _chapter_outline(outline: dict) -> dict:
    """兼容两种形态：
       §3.1 平铺：{"chapter_id": ..., "active_characters": ...}
       嵌套：    {"current_chapter": {...}}
       平铺时用平铺字段覆盖嵌套字段
    """
```

**这里曾经只认嵌套形态**，导致平铺细纲时 P1/P3 全空。

### 8.4 组装

```python
def assemble_context(self, outline, *, current_chapter=None, extra=None) -> dict:
    retrieval = self.search(req, context_entities=ctx_entities)

    p0_units = self._build_p0(outline, current_chapter)          # 大纲/细纲/人物卡/伏笔
    p1_units = self._build_p1({"high": p1_hits})                  # 高相关检索
    p2_units = self._build_p2(current_chapter)                    # 近期正文
    p3_units = self._build_p3(retrieval, outline, set())          # medium + 次要角色
    p4_units = self._build_p4(retrieval)                          # low 抽样 + 风格样本

    for priority, units in (...):
        sections[priority] = self._fit(units, self.budget[priority], dropped)

    text = "\n\n".join(sections[p] for p in PRIORITIES if sections[p])
    # 硬断言：总量 ≤ 窗口 70%
```

### 8.5 两阶段装填

```python
def _fit(self, units, budget, dropped):
    """第一阶段：顺位装，装不下的压缩补白（保留 label + 首字段）
       第二阶段：仍不行就记入 dropped
       最后 clip_tokens 二次兜底
    """
```

极小窗口（300 token）和超长正文下都守住上限，有测试覆盖。

### 8.6 软依赖

```python
def retrieval_available() -> bool: ...

class _FallbackKeeper:
    """retrieval.py 缺失时的极简兜底：
       直接 chest.search() + 简单分档
    """
```

配合注入优先级：`显式 keeper` > `真 MemoryKeeper` > `_FallbackKeeper`。

实测**隐藏 retrieval.py 后 55 个测试仍全绿**（仅 1 个真实现专测 skip）。

### 8.7 动态预取

```python
def prefetch(self, stream_text, *, window=60, context_entities=()):
    """在生成流尾部窗口内识别实体 → 排除已在上下文的 → 检索 → 缓存

    缓存键 = (entities, window)，重复调用直接命中缓存（幂等）
    异常一律吞掉：预取绝不能打断生成
    """
```

---

## 九、回写 writeback.py

904 行。

### 9.1 应用报告

```python
class WriteBackAgent:
    def apply(self, report: dict, *, chapter_id=None) -> dict:
        """先 validate(report, §3.5 Schema) 再落盘
           非法报告在任何写入前抛 SchemaError
        """
        # characters/items/timeline → upsert + set_state
        # foreshadowing → 按 status 调状态机
        # 有 text/summary → put_chapter
```

### 9.2 四类矛盾

```python
def detect_contradictions(self, report, *, chapter_id=None) -> list[str]:
    """死而复生：get_state(角色,"alive") is False 却在 active_characters
       时间倒错：新事件章号 < 该角色 state 最后更新章
       能力违规：声称状态与已记录冲突（如境界回退）
       称呼漂移：正文别称与称呼格登记不一致
    """
```

### 9.3 巡检

```python
def patrol(chest, current_chapter, *, grace=3, stale_threshold=30) -> list[dict]:
    """超期：current > planned_resolve + grace
                → {"type":"overdue", "suggestion":"下一章请安排回收…"}
       陈旧：status=="planted" 且 current - planted > stale_threshold 且从未强化
                → {"type":"stale"} 并标记 data["abandon_candidate"]=True
    """
```

`patrol` 用 `history` 表兜底判断"是否强化过"，防止直连 `chest.reinforce_foreshadow()` 时漏判。

---

## 十、模型层 llm.py / llamacpp.py

### 10.1 接口

```python
class LLMBackend:
    def generate(self, prompt, *, max_tokens=512) -> str
    def plan(self, outline_req) -> dict          # §3.1
    def extract_keywords(self, text) -> list[str]
    def writeback(self, text, outline) -> dict   # §3.5
    def close(self)
```

### 10.2 三个实现

| 类 | 说明 |
|---|---|
| `StubBackend` | 确定性离线实现，blake2b 做种子，同输入同输出 |
| `OllamaBackend` | urllib 调 `/api/generate` |
| `LLamaCppBackend` | 托管 llama-server 子进程，走 OpenAI 兼容接口 |

### 10.3 后端选择

```python
BACKENDS = ("stub", "ollama", "llamacpp")

def get_backend(name=None, *, offline_ok=True):
    """优先级：显式 name > EW_LLM > "stub"
       不可用时降级 Stub 并打一行警告
    """
```

### 10.4 llamacpp 的取舍

```python
def plan(self, outline_req):
    """3B 模型直接吐 JSON 极易格式跑偏，
    所以：模型写正文，确定性逻辑管结构
    细纲/回写报告用 StubBackend 的确定性推导，保证 100% 过 Schema
    """
```

### 10.5 进程托管

```python
class LLamaCppBackend:
    def start(self, *, wait=180.0) -> bool:
        """已在跑 → 复用；否则 spawn llama-server 并轮询 /health"""
    def close(self):
        """只杀自己启动的进程，不碰用户手动起的服务"""
```

---

## 十一、JSON 桥 bridge.py

777 行。**设计目标：一次调用一个 JSON，stdout 绝无杂质。**

### 11.1 入口

```python
def handle(args: dict) -> dict:
    """分发一次请求。永不抛异常：错误一律转成 {ok:false, error:{code,message}}
    """

def main(argv=None) -> int:
    """支持三种传入方式：
       python -m enderwriter.bridge '{"op":"info"}'
       echo '{...}' | python -m enderwriter.bridge --stdin
       python -m enderwriter.bridge --op stats --db story.db
    """
```

退出码：0 成功 / 1 业务错误 / 2 参数错误。

### 11.2 开库策略（关键）

```python
def _open_scoped(args, *, layer=None):
    """规则：
       * 显式给了 db → 单库模式（只有这种情况才灌示例数据）
       * 否则按 work/session_id 解析作品库或全局库
       * 库不存在 → 返回 (None, meta)，调用方给空结果
    """
```

**这里曾经写成 `if work and not db`**，面板不传 `work` 时会掉进临时库分支并自动 `seed_demo()`——于是新对话"浏览条目"看到的是示例故事的张三/李四/王五/赵六。现在所有读取类 op 都走 `_open_scoped`。

### 11.3 op 清单

| op | 作用 |
|---|---|
| `info` | 环境与模型状态 |
| `recall` | 跨层检索 |
| `remember` | 写入（自动路由） |
| `entries` | 列出某格条目 |
| `works` | 列出作品库 |
| `clear` | 清除（自动备份） |
| `overview` | 面板用：两层概况 |
| `search` / `patrol` / `stats` / `writeback` / `demo` / `install` | 单库操作 |

### 11.4 clear 的备份

```python
def op_clear(args):
    """target: work | global | all | work:<作品名>
       先 shutil.move 成 .bak-<时间戳>，再删原文件
    """
    backup = f"{path}.bak-{stamp}"
    shutil.move(path, backup)
```

---

## 十二、DSH 插件 index.js / client.js

### 12.1 Host 半边（index.js，29KB）

**9 个工具注册**：

```js
function registerTool(def) {
  // 包装 execute，记录"最近活跃会话"给面板用
  const originalExecute = def.execute
  def.execute = async (args, toolCtx) => {
    const sid = toolCtx?.agent?.id ? String(toolCtx.agent.id) : ''
    if (sid) lastActiveSession = sid
    return originalExecute(args, toolCtx)
  }
  return ctx.tools.register(def)
}
```

**会话 ID 传递（关键）**：

```js
async execute(args, ctx) {
  // 只从当次调用的 ctx 取，绝不缓存
  const sid = ctx && ctx.agent && ctx.agent.id ? String(ctx.agent.id) : ''
  return run(args ?? {}, ctx, sid)
}
```

每个工具的 `run: async (args, _ctx, sid)` 把 `sid` 传给 `call()`，`call()` 拼进 JSON 请求的 `session_id` 字段。

**反面教训**：最初用了 `let currentSessionId = process.env.DSH_SESSION_ID`，Host 进程被所有会话共享，`execute` 先赋值再 `await`，并发时后一个覆盖前一个，全部写进同一个库。

**Python 桥**：

```js
function callBridge(root, python, request, timeoutMs) {
  const child = spawn(python, ['-m', 'enderwriter.bridge', '--stdin'], {
    cwd: root,
    env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONPATH: root },
    windowsHide: true,
  })
  // stdin 写一个 JSON，stdout 读一个 JSON，超时 kill
}
```

**面板 RPC**：

```js
const connection = ctx.get('connection')
connection.rpc.handle('enderwriter', async (endpoint, payload) => {
  return await route(endpoint, payload)
})
```

用官方 RPC 而不是自建 `webServer` 路由——后者位于连接鉴权之外会 401，且插件重载时撞 `duplicate prefix route`。

### 12.2 Web 半边（client.js，35KB）

**两个注册点**：

```js
ctx.slots.inject('sidebar.panellist', () => ctx.slots.register({
  name: 'sidebar.panellist',
  id: 'enderwriter',      // 侧边栏图标
  order: 20,
  label: '末影箱',
}, PanelIcon))

ctx.slots.inject('main', () => ctx.slots.register({
  name: 'main',
  key: 'enderwriter',     // 中央面板（key = 上面那个 id）
}, SafePanel))
```

**样式注入**（行内 style 写不了 hover/keyframes）：

```js
const stylesApi = globalThis.styles || window.styles
if (stylesApi && typeof stylesApi.insert === 'function') {
  ctx.effect(() => stylesApi.insert(CSS))
}
```

CSS 里定义的动画：

| 动画 | 用途 |
|---|---|
| `ew-fade-in` | 面板入场 |
| `ew-count-in` | 数字驶入 |
| `ew-shimmer` | 骨架屏微光 |
| `ew-spin` | 加载转圈 |
| `ew-toast-in` | Toast 弹出 |
| `ew-dialog-in` | 弹层入场 |

还有 `prefers-reduced-motion` 降级。

**面板取数的困境**：`main` 和 `sidebar.panellist` 都是 **root 作用域**，插槽 props 里没有 session 绑定——面板不知道"当前是哪个对话"。解决办法是 Host 记录 `lastActiveSession`，面板读它。

---

## 十三、测试策略

```
Ran 314 tests in 11.0s
OK
```

| 文件 | 覆盖 |
|---|---|
| `test_smoke.py` | 冻结层/Schema/词法/向量/末影箱/三重索引 |
| `test_retrieval.py` | 归一化/三档预算/in_context/非法请求 |
| `test_orchestrator.py` | P0-P4 预算/极小窗口/软依赖回退/预取幂等 |
| `test_writeback.py` | 四类矛盾/回写落地/patrol 边界 |
| `test_app_layer.py` | LLM 后端/seed/CLI |
| `test_install.py` | 安装器（全 mock，不联网） |
| `test_memory.py` | 分层路由/会话隔离/清除/防种子泄漏 |

### 三条测试原则

1. **全部用临时目录** —— 不碰真实记忆数据
2. **不联网** —— 安装器测试全 mock
3. **修复即补测试** —— 每个真实缺陷都留了回归测试

### 几个防回归测试的例子

```python
def test_foreshadow_reinforce_persists(self):
    """回归：set_foreshadow 的 extra 不得覆盖显式写入的强化/回收字段"""

def test_entries_never_injects_seed_data(self):
    """回归：没指定 work/db 时绝不能凭空灌示例数据"""

def test_two_sessions_are_isolated(self):
    """两个会话各有一套独立记忆"""

def test_multi_hop_reaches_deep_nodes(self):
    """回归：hops>=2 必须真的取到远端节点"""
```

---

## 十四、扩展指引

### 14.1 加一个新的记忆格子

1. `common.py` 的 `LAYERS` 加一项
2. `memory.py` 的 `GLOBAL_LAYERS` 决定它进全局库还是作品库
3. `bridge.py` 的 `op_remember` 不用改（按 layer 自动路由）
4. `client.js` 的 `LAYERS` 数组加一项（面板按钮）
5. Schemas 若引用 `LAYERS` 会自动同步

### 14.2 换成真实嵌入

```python
# 生成向量
vec = your_model.encode(text)     # float32[DIM] 归一化
# 写入时注入
chest.upsert("character", "萧炎", "...", embedding=vec)
```

注意：已有数据需要重建索引（`EMBED_VERSION` +1 后全量重算）。

### 14.3 接入新的大模型

```python
class MyBackend(LLMBackend):
    def generate(self, prompt, *, max_tokens=512) -> str: ...
    def plan(self, outline_req) -> dict: ...
    def extract_keywords(self, text) -> list[str]: ...
    def writeback(self, text, outline) -> dict: ...
    def close(self): ...

# 在 get_backend 里加一个分支
```

### 14.4 加一个新的 bridge op

```python
def op_myop(args):
    return _ok({"result": ...})

_OPS = {
    ...
    "myop": op_myop,
}
```

再在插件的 `route()` 里加一个 case，面板就能调。

### 14.5 自定义面板

Client 半边是纯 JS，可以直接改 `client.js`。注意：

- 样式改动为 CSS 类名，不要写行内 `style`（否则没有 hover/动画）
- 数据通过 `callHost(method, args)` 走 RPC
- 改完需要**重启 DSH**（Client JS 不热更新）

---

## 附：文件行数

```
enderwriter/orchestrator.py  1291
enderwriter/llm.py           1010
enderwriter/writeback.py      904
enderwriter/chest/store.py    904
enderwriter/bridge.py         777
enderwriter/install.py        770
enderwriter/cli.py            755
enderwriter/retrieval.py      639
enderwriter/common.py         515
enderwriter/llamacpp.py       375
enderwriter/seed.py           362
enderwriter/memory.py         227
enderwriter/embed.py           95
enderwriter/lexicon.py         87
enderwriter/llamacc_meta.py    81
enderwriter/chest/__init__.py   6
enderwriter/__main__.py        20
enderwriter/__init__.py        18
                        ───────
                          8,836 行（不含测试）
```

插件：

```
dsh-enderwriter-plugin/index.js    29 KB
dsh-enderwriter-plugin/client.js   35 KB
```
