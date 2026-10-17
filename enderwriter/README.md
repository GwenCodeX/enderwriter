# EnderWriter 末影写作框架 · 最小原型

> 大模型负责"想写什么、什么时候查"，本地小模型负责"查得快、查得准"，末影箱负责"记得住、管得好"。

本仓库按《末影写作框架规范 v0.1》**第三节 Schema** 定义数据结构、**第四节工作流**搭建可运行的最小原型。
目标：让 AI 写出百万字级、伏笔可回收、设定不崩坏的长篇小说。

**非目标**（明确不做）：不做纯上下文压缩；不把整本书塞进窗口；不靠模型"硬记"。

一句话总结：

> EnderWriter = 大模型规划与写作 + 细纲派生关键词 + 本地 Qwen 3B 分层检索末影箱 + 事件触发动态追加 + 写完回写与伏笔巡检。

## 快速开始

```powershell
$py = 'C:\Users\d2132\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe'
cd <本目录>

# 一、零依赖先跑通（离线 Stub，不需要任何模型）
& $py -m enderwriter demo

# 二、装本地 Qwen 3B（一条命令，自动下载，约 2GB）
& $py -m enderwriter install

# 三、用真实模型写作
$env:EW_LLM = 'llamacpp'
& $py -m enderwriter demo

# 其他
& $py -m enderwriter install --check      # 只检查环境，不下载
& $py -m enderwriter query -e 萧炎,药老 -q 黑戒来历 --budget 800
& $py -m enderwriter patrol --chapter ch_042
& $py -m enderwriter stats
& $py -m enderwriter export-schemas --out schemas
& $py -m unittest discover -s tests       # 263 项测试
```

### 本地模型安装器

`enderwriter install` 会自动把"本地 Qwen 3B"这条链路装好，两条路线任选（默认 `auto` 自动挑）：

| 引擎 | 组成 | 体积 | 适合 |
| --- | --- | --- | --- |
| `llamacpp`（默认） | llama.cpp 预编译二进制 + ModelScope 的 GGUF 模型 | ~2.1GB | 境外 CDN 被限速的网络。模型走 ModelScope（国内实测 12MB/s） |
| `ollama` | Ollama 官方安装包 + `qwen2.5:3b-instruct` | ~3.5GB | 网络畅通，或已装 Ollama |

```powershell
& $py -m enderwriter install                        # 自动选引擎并安装
& $py -m enderwriter install --engine llamacpp      # 强制 llama.cpp
& $py -m enderwriter install --engine ollama        # 强制 Ollama
& $py -m enderwriter install --gpu vulkan           # 指定加速后端：vulkan|cuda12|cpu
& $py -m enderwriter install --no-pull              # 只装运行时，不下载模型
```

安装器会自动：检测本机 GPU → 挑选 Vulkan/CUDA/CPU 版二进制 → 下载并解压到 `vendor/` →
下载模型到 `vendor/models/` → 启动服务并**实跑一次推理自检**。
全程幂等，已下好的会跳过；任何一步失败都给出可操作的中文提示。

**实测数据**（AMD Radeon RX 6800 / Qwen2.5-3B-Instruct Q4_K_M）：

| 后端 | 64 token 生成耗时 |
| --- | --- |
| Vulkan（GPU） | 0.23 s |
| CPU | 0.88 s |

**完全离线**：没有任何运行时，`StubBackend` 也能跑通全部六阶段，测试不受影响。

## 架构

| 组件 | 职责 | 模块 |
| --- | --- | --- |
| 作者 Author | 大模型：细纲、关键词、正文生成、工具调用、回写触发 | `llm.py` |
| 编排器 Orchestrator | 上下文组装、工具调用路由、预检索+动态检索、token 预算分层裁剪 | `orchestrator.py` |
| 记忆守护者 Memory Keeper | 本地 Qwen 3B 分层检索（`retrieval.py`）+ 后端可选（`llamacpp.py`） | `retrieval.py` |
| 末影箱 Ender Chest | 分格存储、版本历史、四类索引 | `chest/store.py` |
| 回写代理 Writer-Back | 写后状态更新、矛盾检测、巡检告警 | `writeback.py` |
| 安装器 Installer | 自动装运行时、下模型、环境自检 | `install.py` |

### 末影箱：七个格子 + 四类索引

格子（`LAYERS`）：人物格 `character`、世界格 `world`、时间格 `timeline`、伏笔格 `foreshadowing`、
物品格 `item`、称呼格 `alias`、风格格 `style`。

每条记录带 `version` 与 `last_updated`；**变更只追加历史，永不覆盖**。
伏笔是状态机：`planted → reinforced → resolved / abandoned`。

索引：
- 关键词倒排索引（`terms` 表）：中文双字组 + 拉丁词，实体加权 3.0 / 标题 2.0 / 正文 1.0
- 向量索引（`embedding` BLOB）：numpy 暴力余弦，256 维确定性哈希向量，可注入真嵌入
- 图索引（`edges` 表）：关系边，支持 1~N 跳扩展
- 状态索引（`state` 表）：实体 × 键 → 当前值 + 所属章节

### 检索流程（第四节）

1. **实体归一化**：称呼格把别名映射为标准名（`炎帝` / `岩枭` → `萧炎`）
2. **多通道召回**：关键词 / 向量 / 图，三路并行
3. **三档分层**：high（多通道命中，给全文）/ medium（单通道，给摘要）/ low（弱相关，抽样 1~2 条并打标）
4. **重排**：用 `query_context` 复核排序
5. **预算裁剪**：单次 `search_memory` ≤ 800 token，档位预算 50% / 35% / 15%
6. **结构化返回**：`{high, medium, low, total_tokens}`，每条带 `layer/entity/content/version/last_updated/confidence`

已在上下文中的实体返回"已在上下文"，不重复占预算。

### 上下文优先级预算

| 优先级 | 内容 | 占比 |
| --- | --- | --- |
| P0 | 全局大纲、当前细纲、核心人物卡、待处理伏笔 | 30% |
| P1 | 高相关预检索结果 | 30% |
| P2 | 近期正文（最近 1~2 章，否则摘要+关键句） | 20% |
| P3 | 中相关结果、次要角色与地点 | 15% |
| P4 | 探索性样本、风格样本 | 5% |

章节上下文总量 = 模型窗口的 **70%**。

### 动态检索

- **同步工具调用**：显式、低延迟、关键实体时使用
- **异步预取**：监听生成流，识别实体名后后台检索（规范推荐默认）
- 同步调用作为兜底

### 写后处理

- **回写代理**：把本章变化写回末影箱，并做矛盾检测——死而复生、时间倒错、能力规则违规、称呼漂移
- **异步伏笔巡检**：`current_chapter > planned_resolve + grace` 时告警并把回收建议注入下一章细纲；
  `planted` 后超过 `stale_threshold` 章且从未强化，标记为 `abandoned` 候选

## Schema

`schemas/` 下四份 JSON 与 `enderwriter/common.py` 的 `SCHEMAS` 内容一致：

- `chapter_outline.json` — §3.1 大模型输出的章节细纲
- `search_memory_tool.json` — §3.2 `search_memory` 工具参数
- `memory_result.json` — §3.3 记忆守护者返回
- `writeback_report.json` — §3.5 回写报告

校验器是纯 stdlib 实现的最小 JSON-Schema（支持 `$ref`/`oneOf`/`enum`/`pattern` 等），无第三方依赖。

## 依赖

核心只依赖 **numpy**（向量检索）；其余全是标准库（`sqlite3`、`json`、`argparse`、`unittest`、`urllib`）。
SQLite FTS5 无法切分中文且 `jieba` 不可用，因此分词用确定性 CJK 双字组方案，索引侧与查询侧共用 `analyze()`。

## 目录

```
enderwriter/
  enderwriter/
    common.py        冻结契约：Schema、预算、ID、序列化
    lexicon.py       中文双字组分词与实体抽取
    embed.py         确定性哈希向量
    chest/store.py   末影箱 EnderChest
    retrieval.py     记忆守护者 MemoryKeeper + search_memory
    orchestrator.py  P0-P4 组装 + 动态预取
    writeback.py     回写代理 + 矛盾检测 + patrol
    llm.py           LLMBackend / OllamaBackend / StubBackend / get_backend
    llamacpp.py      llama-server 子进程托管 + GGUF 推理
    llamacc_meta.py  模型与二进制的下载元数据
    install.py       自动安装器（两条引擎路线）
    seed.py          示例故事种子数据
    cli.py           命令行入口
  schemas/           §3 四份 JSON Schema
  tests/             unittest 测试（263 项）
  vendor/            自动下载的运行时与模型（已 gitignore）
```

## 后端选择

| `EW_LLM` | 说明 |
| --- | --- |
| `stub`（默认） | 确定性离线实现，无需任何模型，测试与 CI 用 |
| `llamacpp` | llama.cpp + 本地 GGUF，GPU 加速，`enderwriter install` 自动装好 |
| `ollama` | Ollama 官方路线，`qwen2.5:3b-instruct` |

相关环境变量：

| 变量 | 作用 |
| --- | --- |
| `EW_LLM` | 选择后端 |
| `EW_GGUF` | 指定 GGUF 模型路径（覆盖 `vendor/models/` 自动发现） |
| `EW_LLAMA_SERVER` | 指定 llama-server 可执行文件 |
| `EW_OLLAMA_URL` / `EW_OLLAMA_MODEL` | Ollama 地址与模型 |
| `EW_GPU` | 强制加速后端：`vulkan` / `cuda12` / `cpu` |
| `EW_LOG` | 日志级别，`DEBUG` 可看检索 trace |

## 扩展点（§8）

多卷规划、作者干预（人工锁定伏笔）、情绪/风格切换、读者模拟代理、按章回滚。
