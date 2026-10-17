# dsh-enderwriter · 末影写作插件

把「末影箱」长时记忆接入 DSH 会话，并提供可视化面板。

## 依赖

- DSH Harness
- Python 3.10+
- EnderWriter 核心包（本仓库的 `enderwriter/` 目录）

## 安装

1. 确认 Python 可用：

   ```bash
   python -c "import sqlite3; print('ok')"
   ```

2. 安装插件（在 DSH 里指向本目录）：

   ```bash
   dsh plugin add /path/to/dsh-enderwriter-plugin
   ```

3. 插件会自动探测：
   - **包根**：同级或上级目录中带 `enderwriter/bridge.py` 的文件夹
   - **Python**：`DSH_HOME` 下的运行时 → 用户目录 `.dsh` → 系统 PATH

   探测失败时可手动指定（`cordis.patch.yml` 或插件设置里）：

   ```yaml
   root: 'D:/code/enderwriter'      # 含 enderwriter/bridge.py 的目录
   python: 'C:/Python312/python.exe'
   work: ''                          # 留空 = 每个对话独立记忆
   ```

## 提供的工具

| 工具 | 作用 |
|---|---|
| `enderwriter_recall` | 跨层检索人物 / 设定 / 伏笔，返回高·中·弱三档 |
| `enderwriter_remember` | 写入一条记忆，自动分层 |
| `enderwriter_works` | 列出所有作品库 |
| `enderwriter_patrol` | 伏笔巡检：超期未回收、长期陈旧 |
| `enderwriter_check` | 正文一致性检查（死而复生 / 伤情 / 持有者 / 境界） |
| `enderwriter_writeback` | 写完一章后回写事实变化 |
| `enderwriter_forget` | 清除记忆（自动备份） |
| `enderwriter_stats` | 存储概况 |
| `enderwriter_info` | 运行环境自检 |
| `enderwriter_demo` | 跑一遍六阶段流水线 |

## 面板

侧边栏「末影箱」图标 → 三级内容区块：

```
内容区块
  本对话          人物 / 世界 / 时间线 / 伏笔 / 物品
  全局文学库      写作手法 / 叙事节奏 / 风格选型 / 经验教训
  技术库          跨项目的工程笔记
```

点进任一区块：**搜索框 → 统计 → 条目列表**，每条可展开全文或单独删除。

危险操作（清除整库 / 删除单条）都需要**勾选确认框**才能提交。

## 数据位置

默认存放在 EnderWriter 包根的 `memory/` 下：

```
memory/
  works/
    作品名-哈希.db      每个作品一套
    对话-xxxx.db        每个对话一套
  global.db             全局文学库
  tech.db               技术库
```

删除前会生成 `.bak-时间戳` 备份。

## 故障排查

| 现象 | 检查 |
|---|---|
| 面板数据全是 0 | 看 `memory/plugin-diag.json` 里 `root` / `python` 是否为 null |
| 点了没反应 | 确认 DSH 已重启（Client 代码改动必须重启） |
| 路由 404 | 看 `memory/plugin-route.txt` 的注册结果 |
| 找不到 Python | 在配置里显式写 `python` 绝对路径 |

## 许可

MIT
