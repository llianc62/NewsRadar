# Agent 子系统设计文档

> **版本**: v1.2（Phase 1-3 已完成）  
> **当前实现架构**: [architecture-v1.md](architecture-v1.md)（52k，权威设计文档）  
> **旧版 Phase 文档**: [phase0-chat.md](phase0-chat.md) 至 [phase4-tools.md](phase4-tools.md) 为历史设计记录，当前实现以 architecture-v1.md 为准  
> **目标**: 为 NewsRadar 引入可演进的 AI Agent 子系统，从"无角色对话"渐进到"深度角色扮演+知识库+工具编排"  
> **设计原则**: 增量搭建、每层可验证、不侵入现有新闻管线

---

## 模块索引

| 模块 | 文档 | 说明 |
|------|------|------|
| Phase 0 | [phase0-chat.md](phase0-chat.md) | LLM 接入 + 聊天界面 + WebSocket |
| Phase 1 | [phase1-context.md](phase1-context.md) | 上下文工程（窗口→摘要→多级压缩） |
| Phase 2 | [phase2-memory.md](phase2-memory.md) | 跨会话记忆（提取/合并/检索） |
| Phase 3 | [phase3-knowledge.md](phase3-knowledge.md) | 知识库 RAG（pgvector，已完成） |
| Phase 4 | [phase4-tools.md](phase4-tools.md) | 工具调用 / MCP |
| 角色扮演 | [persona.md](persona.md) | PersonaAgent + 多角色编排（仿 ai-hedge-fund，已完成） |
| KB 升级 | [knowledge-upgrade-plan.md](knowledge-upgrade-plan.md) | 知识库 RAG 后续演进（hybrid/rerank/query 改写，规划中） |
| 配置 | [configuration.md](configuration.md) | config.yaml + loader 设计 |
| 集成 | [integration.md](integration.md) | 与现有系统融合 + 数据库表 |

---

## 1. 架构总览

### 1.1 系统边界

Agent 子系统与现有 NewsRadar 代码隔离：

```
┌─────────────────────────────────────────────────────┐
│                    Web Frontend                       │
│  /agent  → 聊天界面（独立页面）                        │
│  ws://.../api/ws → 统一 WebSocket（聊天 + 通知）       │
├─────────────────────────────────────────────────────┤
│                   agent/ 模块                          │
│  Agent (核心类)                                       │
│  ├─ llm.py          → 模型工厂                        │
│  ├─ context.py      → 上下文管理                      │
│  ├─ memory/         → 记忆系统                        │
│  ├─ knowledge/      → 知识库（预留）                   │
│  └─ tools/          → 工具（预留）                     │
├─────────────────────────────────────────────────────┤
│  共享基础设施                                          │
│  config/loader.py    → 配置加载（扩展 llm 段）         │
│  storage/postgres.py → PG 复用（用户/记忆/知识库存储）  │
└─────────────────────────────────────────────────────┘
```

### 1.2 分阶段演进

| Phase | 产出 | 核心新增 | 可验证方式 |
|-------|------|---------|----------|
| 0 | 聊天窗口 + 默认 agent | LLM 工厂 + 聊天 UI + WebSocket | 浏览器 `/agent` 能对话 |
| 1 | 长对话不爆 token | 阶梯式上下文压缩（窗口→摘要→多级） | 20 轮对话后观察 token |
| 2 | 跨会话记忆 | 记忆提取/合并/检索（异步） | 关页重开，回忆之前信息 |
| 3 | 知识库支撑角色扮演 | 知识库 RAG（system prompt 注入） | 问知识库事实，答案正确 |
| 4 | 外部工具调用 | tools + ReAct 循环 | 自主决定调工具 |

---

## 2. 目录结构

```
agent/                              # 核心逻辑，不依赖 Web
├── __init__.py
├── types.py                        # LlmConfig / Turn 等 dataclass
├── llm.py                          # build_llm() 工厂
├── agent.py                        # Agent 核心类（逐 Phase 递增）
├── context.py                      # 上下文管理器
├── memory/
│   ├── __init__.py
│   ├── store.py                    # 记忆存储抽象 + pgvector 实现
│   └── manager.py                  # 提取/合并/检索（批量）
├── knowledge/
│   └── __init__.py                 # Phase 3 预留
└── tools/
    └── __init__.py                 # Phase 4 预留

web/
├── app.py                          # 新增 register_agent_routes()
├── templates/
│   ├── base.html                   # 侧边栏加"AI 助手"入口
│   └── pages/
│       └── agent_chat.html         # 聊天界面
├── static/
│   └── css/
│       └── agent_chat.css          # 聊天界面样式

storage/
├── postgres.py                     # 新增 _init_agent_schema() / _init_pgvector()
```

**说明**：
- 去掉 `agent/config.py`——配置加载统一在 `config/loader.py`
- 新增 `agent/types.py`——存放所有 dataclass/TypedDict 类型定义
- 存储层在 `storage/postgres.py` 中新增 agent 专用表的 schema 初始化

---

## 附录：设计决策记录

| 决策 | 选项 | 选择 | 理由 |
|------|------|------|------|
| 模型接入 | LiteLLM / 原生 SDK / langchain 薄包装 | `langchain-openai` / `langchain-anthropic` | 供应商已双协议，LiteLLM 多余；薄包装与 LangGraph 无缝集成 |
| 流式方案 | SSE / WebSocket | WebSocket | 统一实时通道，支持停止生成、工具调用可见性、通知推送；比双通道（SSE+WS）更简单 |
| 记忆存储 | mem0 / Letta / Zep / 自建 | 自建（pgvector 复用 PG） | 零新组件，可控制，后续可迁移到专用方案 |
| 知识库起步 | 裸 Neo4j+pgvector / LlamaIndex / GraphRAG | **LlamaIndex + pgvector** | 起步快，跑通后看量升级 |
| 知识库接入方式 | system prompt 注入 / tool call | Phase 3 用注入，Phase 4 转 tool | 注入简单可靠，tool 灵活可编排 |
| 聊天状态 | 服务端 / 客户端 | 服务端 PostgreSQL | 记忆系统需要会话记录做基础数据；避免 localStorage 丢失风险 |
| 记忆提取时机 | 仅 close() / 每条消息后异步 | 每条消息后异步 + close() 保底 | 关标签页不丢记忆，不阻塞用户 |
| 合并策略 | 逐条 LLM / 批量 LLM | 批量 LLM | 一次 prompt 处理所有新事实，大幅降低调用次数 |
| 用户系统 | 做 / 不做 | **不做** | Personal 分析系统，无需用户概念；session 由 cookie 自动管理 |
| 模型偏好 | 固定 / 用户可选 | 用户可选 deep/quick | 用户控制成本和质量；系统底层自动用 quick |
| 默认模型 | deep / quick | **quick** | 默认快速响应，需要深度时手动切换 |
| 会话标题 | 首消息前 30 字 / LLM 生成 | 首消息前 30 字 | 简单可靠，不浪费 LLM 调用 |
| 上下文压缩 | 截断 / LLM 摘要 / 多级摘要 | 三级演进（window → summary → multi） | 每阶段可验证，逐步提升压缩质量 |
| 与现有系统隔离 | 侵入 / 独立模块 | 独立模块 | 不改新闻管线一行，风险最低 |