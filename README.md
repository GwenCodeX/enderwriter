# EnderWriter · 末影写作

> 让 AI 写出百万字级、伏笔可回收、设定不崩坏的长篇小说。

大模型负责「想写什么、什么时候查」，本地小模型负责「查得快、查得准」，
末影箱负责「记得住、管得好」。

**不做**纯上下文压缩 · **不把**整本书塞进窗口 · **不靠**模型硬记。

---

## 这是什么

一套面向长篇小说的**分层长时记忆系统** + **DSH 插件**。

核心是一个叫「末影箱」的本地记忆库，把小说的事实（人物、世界、时间线、
伏笔、物品）与写作经验（风格、节奏、手法）分开存储，在写作时按需检索，
在写完章节后自动回写变化，并巡检未回收的伏笔。

## 能力

| 能力 | 说明 |
|---|---|
| 分层记忆 | 作品库（本对话/本作品）＋ 全局文学库（跨对话）＋ 技术库（跨项目） |
| 四路检索 | 关键词倒排 / 向量 / 关系图 / 状态表，按 50%·35%·15% 三档预算返回 |
| 伏笔状态机 | `planted → reinforced → resolved / abandoned`，超出计划章节自动告警 |
| 回写与查重 | 写完一章自动更新事实，并检测死而复生、伤情违反、持有者违反、境界回退 |
| 规则引擎审查 | 确定性规则核对正文与设定冲突，不依赖模型判断 |
| 六阶段流水线 | 规划 → 检索 → 组装 → 生成 → 动态追加 → 回写 → 巡检 |
| 本地模型 | 支持 llama.cpp（GGUF）与 Ollama，自动安装、自动选择 Vulkan/CPU |

## 快速开始

### 作为 DSH 插件

```bash
# 安装插件（本地路径）
dsh plugin add /path/to/dsh-enderwriter-plugin
```

安装后在侧边栏出现「末影箱」图标，面板可浏览三层记忆、搜索条目、清除数据。

### 作为命令行工具

```bash
cd enderwriter
pip install -e .            # 或直接用 python -m enderwriter

python -m enderwriter info          # 环境自检
python -m enderwriter recall 沈砚    # 跨层检索
python -m enderwriter remember ...  # 写入记忆
python -m enderwriter patrol ch_042 # 伏笔巡检
python -m enderwriter demo          # 跑通六阶段流水线
```

## 目录结构

```
enderwriter/                 Python 核心包
  enderwriter/
    common.py                契约层：估算、Schema、校验
    lexicon.py               分词、实体抽取
    embed.py                 向量化（256 维哈希）
    chest/store.py           末影箱存储：四路索引 + 伏笔状态机
    memory.py                分层路由：作品库 / 全局库 / 技术库
    retrieval.py             记忆守护者：三档预算检索
    orchestrator.py          编排器：六阶段流水线
    writeback.py             回写代理 + 矛盾检测 + 伏笔巡检
    review.py                规则引擎：正文一致性检查
    llm.py                   模型后端：stub / ollama / llamacpp
    llamacpp.py              llama.cpp 后端
    install.py               自动安装 Qwen 与推理引擎
    bridge.py                JSON 行协议桥（供插件调用）
    cli.py                   命令行入口
  tests/                     340 项测试

dsh-enderwriter-plugin/      DSH 插件
  index.js                   Host 半边：10 个工具 + 数据路由
  client.js                  Client 半边：可视化面板
  cordis.patch.yml           插件配置
```

## 文档

| 文档 | 内容 |
|---|---|
| [STRUCTURE.md](enderwriter/STRUCTURE.md) | 整体架构、四路索引原理、分层与检索流程 |
| [CODE.md](enderwriter/CODE.md) | 逐模块代码说明与调用关系 |
| [INTERFACES.md](enderwriter/INTERFACES.md) | 模块间契约与数据结构 |
| [ACCEPTANCE.md](enderwriter/ACCEPTANCE.md) | 验收标准与测试覆盖 |

## 设计取舍

- **记忆不靠模型记**：所有事实落在 SQLite，检索结果带来源与版本，可追溯、可纠正。
- **两层分离**：小说细节随作品走，写作经验跨作品复用，互不污染。
- **本地优先**：检索用小模型或纯算法完成，不把整本书送进大模型窗口。
- **规则而非判断**：一致性检查用确定性规则，避免小模型误报。
- **失败隔离**：插件任何异常都被兜住，最坏只是面板不可用，不影响宿主。

## 许可

MIT
