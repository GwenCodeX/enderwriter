# DSH 插件

把 EnderWriter 接入 [DeepSeek Harness](https://github.com/GwenCodeX)：

- **6 个会话内工具** —— 直接在对话里查设定、巡检伏笔、跑写作流水线
- **可视化面板** —— 左侧边栏「末影箱」图标，浏览七格统计与全部条目

## 安装

```bash
# 1) 先装好 Python 侧
pip install numpy
python -m enderwriter install --check

# 2) 把本目录注册为 DSH bundle
#    在 DSH 里执行 plugin_manager install_bundle，target 指向本目录
```

安装后重启 DSH，左侧边栏底部会出现箱子图标。

## 配置

`cordis.patch.yml` 里的 `root` 与 `python` 默认留空，插件会自动探测：

| 项 | 探测顺序 |
| --- | --- |
| `root` | Config.root → `EW_ROOT` → 同级 `enderwriter/` → 当前目录下 `enderwriter/` |
| `python` | Config.python → `EW_PYTHON` → DSH 自带运行时 → PATH 上的 `python` |

克隆本仓库后通常无需任何配置。

## 工具

| 工具 | 用途 |
| --- | --- |
| `enderwriter_search` | 分层检索（high 全文 / medium 摘要 / low 抽样），别名自动归一化 |
| `enderwriter_patrol` | 伏笔巡检，返回可直接写进下一章细纲的回收建议 |
| `enderwriter_stats` | 末影箱存储概况 |
| `enderwriter_writeback` | 应用 §3.5 回写报告 + 四类矛盾检测 |
| `enderwriter_demo` | 跑完整六阶段流水线 |
| `enderwriter_info` | 环境与模型就绪状态 |

## 结构

```
index.js          Host 半边：注册工具 + 数据通道
client.js         Web 半边：侧边栏图标 + 末影箱面板
cordis.patch.yml  Loader 补丁
locale/           插件标题与描述（中/英）
icon.svg          图标
```

数据通过 `python -m enderwriter.bridge --stdin` 与 Python 通信：
一次调用一个 JSON，stdout 只回一个 JSON。
