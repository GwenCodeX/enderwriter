"""EnderWriter —— 规划驱动的长篇小说写作框架（最小原型）。

分层：
  common       冻结契约：Schema、token 预算、ID 与序列化工具
  lexicon      中文分词（双字组）与实体抽取
  embed        确定性哈希向量（离线可复现，可外部注入真嵌入）
  chest        末影箱：SQLite 分格存储、版本历史、多重索引召回
  retrieval    记忆守护者：三档分层检索与 search_memory 工具
  orchestrator 编排层：P0-P4 上下文组装与动态预取
  writeback    回写代理：状态更新、矛盾检测、伏笔巡检
  llm          大模型后端（Ollama / 离线 Stub）
  cli          命令行入口
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
