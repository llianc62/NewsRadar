# Agent 子系统设计文档

> **版本**: v0.3  
> **目标**: 为 NewsRadar 引入可演进的 AI Agent 子系统，从"无角色对话"渐进到"深度角色扮演+知识库+工具编排"  
> **设计原则**: 增量搭建、每层可验证、不侵入现有新闻管线

---

## 模块文档

| 模块 | 文档 | 说明 |
|------|------|------|
| 总览 | [agent/index.md](agent/index.md) | 架构总览 + 目录结构 + 设计决策 |
| Phase 0 | [agent/phase0-chat.md](agent/phase0-chat.md) | LLM 接入 + 聊天界面 + WebSocket |
| Phase 1 | [agent/phase1-context.md](agent/phase1-context.md) | 上下文工程（窗口→摘要→多级压缩） |
| Phase 2 | [agent/phase2-memory.md](agent/phase2-memory.md) | 跨会话记忆（提取/合并/检索） |
| Phase 3 | [agent/phase3-knowledge.md](agent/phase3-knowledge.md) | 知识库 RAG |
| Phase 4 | [agent/phase4-tools.md](agent/phase4-tools.md) | 工具调用 / MCP |
| 配置 | [agent/configuration.md](agent/configuration.md) | config.yaml + loader 设计 |
| 集成 | [agent/integration.md](agent/integration.md) | 与现有系统融合 + 数据库表 |