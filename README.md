# EnderWriter 末影写作框架

> 大模型负责"想写什么、什么时候查"，本地小模型负责"查得快、查得准"，末影箱负责"记得住、管得好"。

一个面向**百万字级长篇小说**的 AI 写作框架原型。解决的核心问题是：**写到第 400 章时，设定还在不在、伏笔还收不收得回。**

按《末影写作框架规范 v0.1》第三节 Schema 定义数据结构、第四节工作流实现。

**非目标**：不做纯上下文压缩；不把整本书塞进窗口；不靠模型"硬记"。

---

## 它解决什么问题

长篇小说 AI 写作的真正瓶颈不是单章质量，而是**跨章一致性**：

- 角色在第 30 章晋升斗帝，第 42 章有人还说他斗气三段
- 第 12 章埋的伏笔，第 200 章还没回收，也没人记得
- 同一个角色的称呼从"师姐"漂移成"阿姐"，前后矛盾

这些靠"把前文塞进窗口"解决不了——百万字塞不进任何模型。所以需要一套**外置的结构化长期记忆**，加上**按需检索**。

---

## 核心设计

```
作者(大模型) ──细纲──> 编排器 ──检索请求──> 记忆守护者(本地 3B) ──> 末影箱(SQLite)
     │                    │                        │                    │
     │<──分层上下文───────┘                        │                     │
     │                                                                  │
     └────────────写完整章─────────> 回写代理 ──状态更新+矛盾检测───────┘
                                          │
                                          └──> 异步伏笔巡检 ──> 下一章细纲
```

| 组件 | 职责 | 模块 |
| --- | --- | --- |
| **末影箱** Ender Chest | 分格存储、版本历史、四类索引 | `chest/store.py` |
| **记忆守护者** Memory Keeper | 三档分层检索（`search_memory`） | `retrieval.py` |
| **编排器** Orchestrator | P0–P4 上下文预算组装 + 动态预取 | `orchestrator.py` |
| **回写代理** Writer-Back | 写后状态更新、矛盾检测、伏笔巡检 | `writeback.py` |
| **作者** Author | 细纲、正文、工具调用 | `llm.py` |

### 末影箱：七个格子 + 四类索引

格子：人物 / 世界 / 时间 / 伏笔 / 物品 / 称呼 / 风格。

每条记录带 `version` 与 `last_updated`，**变更只追加历史，永不覆盖**（支持按章回滚）。
伏笔是状态机：`planted → reinforced → resolved / abandoned`。

四类索引：
- **关键词倒排**：中文双字组（无 jieba 也能切中文），实体权重 3.0 / 标题 2.0 / 正文 1.0 + IDF
- **向量**：256 维，可注入真实嵌入
- **图**：关系边，支持多跳扩展
- **状态**：实体 × 键 → 当前值 + 所属章节

### 检索流程

实体归一化（别名→标准名）→ 多通道召回（关键词/向量/图）→
**三档分层**（high 全文 50% / medium 摘要 35% / low 抽样 15%）→
预算裁剪（单次 ≤ 800 token）→ 结构化返回。

已在上下文中的实体返回"已在上下文"，**不重复占预算**。

### 上下文优先级预算

| 优先级 | 内容 | 占比 |
| --- | --- | --- |
| P0 | 全局大纲、当前细纲、核心人物卡、待处理伏笔 | 30% |
| P1 | 高相关预检索 | 30% |
| P2 | 近期正文 | 20% |
| P3 | 中相关、次要角色与地点 | 15% |
| P4 | 探索样本、风格样本 | 5% |

章节上下文总量 = 模型窗口的 **70%**。

---

## 快速开始

```bash
pip install numpy          # 唯一的第三方依赖
python -m enderwriter demo # 跑完整六阶段流水线
python -m unittest discover -s tests
```

`demo` 会走完 **①规划 → ②检索 → ②b组装 → ③生成 → ④动态追加 → ⑤回写 → ⑥巡检**，
并打印每一步的 token 预算、检索分层、伏笔状态与矛盾检测结果。

其他命令：

```bash
python -m enderwriter query -e 沈砚,断魂钉 -q 月蚀之约 --budget 400
python -m enderwriter patrol --chapter ch_016
python -m enderwriter stats
python -m enderwriter export-schemas --out schemas
```

### 接入本地 Qwen 3B

```bash
python -m enderwriter install            # 自动装运行时 + 下模型 + 自检
python -m enderwriter install --check    # 只检查环境
```

两条路线任选：

| 引擎 | 组成 | 体积 |
| --- | --- | --- |
| `llamacpp`（默认） | llama.cpp 预编译二进制 + ModelScope GGUF | ~2.1GB |
| `ollama` | Ollama + `qwen2.5:3b-instruct` | ~3.5GB |

安装器自动检测 GPU 并选择 Vulkan / CUDA / CPU 构建。

实测（AMD RX 6800 / Qwen2.5-3B Q4_K_M）：**GPU 0.23s、CPU 0.88s 生成 64 token**。

没有本地模型时自动降级为确定性 `StubBackend`，全部测试仍可运行。

---

## 作为 DSH 插件使用

`dsh-plugin/` 是把本框架接入 DeepSeek Harness 的 Host 插件，提供：

- **6 个会话内工具**：`enderwriter_search` / `patrol` / `stats` / `writeback` / `demo` / `info`
- **可视化面板**：左侧边栏「末影箱」图标，点开可浏览七格统计、伏笔状态、逐格条目

通过与 Python 的 JSON 桥（`python -m enderwriter.bridge --stdin`）通信，一次调用一个 JSON。

---

## 规范实现对照

| 规范要求 | 实现位置 |
| --- | --- |
| §3.1 章节细纲 Schema（9 字段） | `common.py` `SCHEMAS["chapter_outline"]` |
| §3.2 `search_memory` 工具参数 | `SCHEMAS["search_memory_tool"]` |
| §3.3 记忆守护者返回 | `SCHEMAS["memory_result"]` |
| §3.5 回写报告 | `SCHEMAS["writeback_report"]` |
| 单次检索 ≤ 800 token | `retrieval.py` |
| 三档 50/35/15 | `retrieval.py` |
| P0–P4 = 30/30/20/15/5 | `orchestrator.py` |
| 上下文 ≤ 窗口 70% | `orchestrator.py`（硬断言） |
| 伏笔超期 `planned_resolve + grace` | `writeback.py patrol()` |
| 四类矛盾检测 | `writeback.py detect_contradictions()` |

四份 Schema 也导出在 `schemas/*.json`。

---

## 测试

```
Ran 263 tests in 8.6s
OK
```

覆盖：Schema 校验器（含 `$ref`）、分词、向量、末影箱 CRUD/版本/索引、
检索三档预算、P0–P4 组装边界、四类矛盾、伏笔巡检边界、安装器（全 mock，不联网）。

---

## 依赖

核心只依赖 **numpy**；其余全是标准库（`sqlite3`、`json`、`argparse`、`unittest`、`urllib`）。

SQLite FTS5 无法切分中文且 `jieba` 属重依赖，因此采用确定性 CJK 双字组方案，
索引侧与查询侧共用 `analyze()`。

---

## 目录

```
enderwriter/        Python 包
  common.py         冻结契约：Schema、token 预算、ID
  lexicon.py        中文双字组分词
  embed.py          确定性哈希向量
  chest/store.py    末影箱
  retrieval.py      记忆守护者 + search_memory
  orchestrator.py   P0–P4 组装 + 动态预取
  writeback.py      回写 + 矛盾检测 + 巡检
  llm.py            LLMBackend / Ollama / llama.cpp / Stub
  llamacpp.py       llama-server 进程托管
  install.py        自动安装器
  bridge.py         JSON 桥（给插件用）
  cli.py            命令行
tests/              unittest
schemas/            §3 四份 JSON Schema
dsh-plugin/         DSH 插件（Host + Web 面板）
```

---

## 路线图（§8 扩展点）

- [ ] 多卷规划
- [ ] 作者干预（人工锁定伏笔）
- [ ] 情绪 / 风格切换
- [ ] 读者模拟代理
- [ ] 按章回滚（`history` 表已备好数据基础）

## License

MIT
