# EnderWriter 冻结接口契约（v1，组件的唯一依赖）

所有组件只依赖以下已落盘模块。**不要修改** `common.py` / `lexicon.py` / `embed.py` / `chest/store.py`。
如需新增能力，在自己的模块里实现。

运行环境：
- Python：`C:\Users\d2132\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe`
- 包根：`C:\Users\d2132\Documents\deepseek-harness\default-workspace\enderwriter`
- 运行测试：`cd <包根>; & $py -m unittest discover -s tests`
- 无 GPU / 无 Ollama / 无网络。numpy 是唯一第三方依赖。

## 1. `enderwriter/common.py`
- `LAYERS = ("character","world","timeline","foreshadowing","item","style")`
- `LAYER_CN: dict[str,str]`（含 `"alias": "称呼格"`）
- `est_tokens(text) -> int`（CJK≈1/字，其余非空白 +0.25，向上取整）
- `clip_tokens(text, budget, suffix="…") -> str`
- `make_id(prefix, n, width=3) -> str`（`fs_007`）
- `chapter_index(chapter_id) -> int`（失败 -1）、`chapter_sort_key(id)`
- `dumps(obj) -> str` / `loads(s, default=None)`
- `get_logger(name)`（读环境变量 `EW_LOG`，默认 WARNING）
- `SchemaError(ValueError)`、`validate(value, schema_dict)`、`validate_or_raise(json, schema_dict)`
- `SCHEMAS: dict` 含 4 项、`schema(name) -> dict`
- `export_schemas(dest_dir)`、`normalize_name(name)`、`dedupe(seq)`
- `keyword_table(outline, extra=()) -> list[str]`（从 §3.1 细纲派生关键词，不额外推断）

## 2. `enderwriter/lexicon.py`
- `analyze(text) -> list[str]`（NFKC；CJK 双字组；拉丁词 ≥2 字符小写；数字串；保留重复以计 tf）
- `term_weights(text, *, entity="", title="") -> dict[str,float]`（实体 3.0 / 标题 2.0 / 正文 1.0）
- `extract_entities(text, vocab) -> list[str]`（最长优先，按首次出现排序——供异步预取）
- `ZH_STOP`、`bigrams(run)`

## 3. `enderwriter/embed.py`
- `DIM = 256`、`EMBED_VERSION = 1`
- `embed(text) -> np.ndarray[float32, DIM]`（L2 归一化，空文本零向量）
- `embed_many(texts) -> np.ndarray[N, DIM]`
- `to_blob(vec) -> bytes` / `from_blob(blob|None, dim=DIM) -> vec`
- `cosine(a, b) -> float` / `cosine_matrix(query[1,D], mat[N,D]) -> np.ndarray[N]`

## 4. `enderwriter/chest/store.py` → `EnderChest`
写入：
- `upsert(layer, entity, content, *, data=None, chapter_id=None, embedding=None) -> Record`
  - 未知 layer 抛 `ValueError`；非 `alias` 层先 `resolve_entity`（别名→标准名）；version +1；追加历史
- `set_foreshadow(fs_id, content, *, status="planted", planted_chapter=None, planned_resolve=None, data=None) -> Foreshadow`
- `reinforce_foreshadow(fs_id, chapter_id) -> Foreshadow`、`resolve_foreshadow(fs_id, chapter_id)`、`abandon_foreshadow(fs_id, reason="")`；不存在抛 `KeyError`
- `next_foreshadow_id() -> str`
- `set_state(entity, key, value, *, chapter_id=None)`、`link_alias(alias, canonical)`、`add_edge(src, rel, dst, *, chapter_id=None, data=None)`
- `put_chapter(chapter_id, text, *, title="", summary="")`

读取：
- `get(layer, entity) -> Record|None`、`get_any(entity, layers=None) -> list[Record]`
- `history(layer, entity) -> list[Record]`（旧→新）
- `resolve_entity(name) -> str`、`display_name(name) -> str`、`synonyms(canonical) -> list[str]`
- `get_state(entity, key) -> Any|None`、`states_of(entity) -> dict`、`state_chapter(entity, key) -> str|None`
- `all_foreshadowings(*, only_open=False) -> list[Foreshadow]`
- `get_chapter(id) -> dict|None`、`recent_chapters(n, *, before=None) -> list[dict]`、`iter_chapters()`
- `neighbors(entity, hops=1) -> list[tuple[str,str,str]]`（src, rel, dst）
- `all_entities(layers=None) -> list[tuple[str,str]]`、`entity_vocab() -> set[str]`、`detect_entities(text) -> list[str]`
- `next_chapter_id() -> str`、`stats() -> dict`
检索（均返回 `list[tuple[Record, float]]`，分降序）：
- `search_terms(terms: Iterable[str], *, layers=None, limit=50)`
- `search_vector(query: str|np.ndarray, *, layers=None, limit=50)`
- `search_graph(entities, *, hops=1, layers=None, limit=50)`
- `search(query, entities=(), *, layers=None, limit=50)`（三路合并，取最大分，多通道 +15%）

`Record`：`layer, entity, content, data, version, last_updated, chapter_id`，属性 `chapter_no`，
方法 `to_hit(*, band="high", confidence=None) -> Hit`。
`Hit`：`layer, entity, content, version, last_updated, confidence, band, in_context, data, channels`，
方法 `to_dict()`（`in_context=True` 时 content 返回 `"已在上下文"`）、`render()`。
`Foreshadow`：`id, content, status, planted_chapter, planned_resolve, reinforced_chapters, resolved_chapter, data`，
属性 `planted_no`、`planned_no`、`is_open`。

## 5. 需实现的模块与签名（严格照此）
- `enderwriter/retrieval.py`：`MemoryKeeper`、`search_memory(chest, req, *, context_entities=()) -> dict`
- `enderwriter/orchestrator.py`：`Orchestrator`、`assemble_context(...) -> dict`
- `enderwriter/writeback.py`：`WriteBackAgent`、`patrol(chest, current_chapter, *, grace=3, stale_threshold=30) -> list[dict]`
- `enderwriter/llm.py`：`LLMBackend`（协议）、`OllamaBackend`、`StubBackend`、`get_backend()`
- `enderwriter/cli.py`：`main(argv=None)`

预算常量（严格照规范，来自 §4）：
- P0 全局大纲/当前细纲/核心人物卡/待处理伏笔 = 30%
- P1 高相关预检索 = 30%；P2 近期正文 = 20%；P3 中相关/次要角色地点 = 15%；P4 探索+风格样本 = 5%
- 章节上下文总量 = 模型窗口 70%
- 单次 `search_memory` ≤ 800 token；层级 high 50% / medium 35% / low 15%
