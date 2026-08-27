# Agent 与现有系统的融合

> **父文档**: [architecture-v1.md](architecture-v1.md)

---

## 1. 改动最小化

- **不改现有新闻管线一行代码**。Agent 子系统是 `agent/` 下的独立模块
- **config.yaml 加 `models:` 段**——不改变已有配置结构，只是新增节
- **`web/app.py` 通过 `include_router` 注册 agent 路由**——作为额外路由注册，不修改现有新闻路由
- **`web/templates/base.html` 侧边栏加一个入口**——不改变已有组件
- **`storage/postgres.py` 在 `_init_schema()` 中新增 agent 表**——新增表不干扰现有表
- **Agent 路由始终注册**（页面显示空状态提示），Agent 实例仅在配置了 `models` 段时由 daemon 预构建

---

## 2. 通知系统现状

通知系统**维持双通道**，未迁移到统一 WebSocket：

| 通道 | 用途 | 端点 |
|------|------|------|
| SSE | 新闻抓取/同步/refetch 状态推送 | `GET /api/notifications/stream` |
| WebSocket | Agent 实时聊天 + 工具审批 | `WS /api/ws` |

`NotificationState`（`web/notification.py`）管理 SSE 客户端连接池，跨线程通过 `call_soon_threadsafe` 分发事件。Agent WebSocket 独立管理自己的连接池。

---

## 3. main.py 改动

```python
# main.py 中 create_app() 之前：启动 MCP Server + 构建 AgentFactory
await self._start_mcp_server(mcp_cfg)          # 聊天室 agent 依赖 MCP
tool_registry = setup_builtin_tools()
agent_factory = AgentFactory(self.config["agent"]["models"], self.db,
                             tool_registry, base_prompt=base_prompt)
app = create_app(db, s3_config, queues=queues, crawler=crawler,
                 agent_config=self.config, tool_registry=tool_registry,
                 agent_factory=agent_factory, base_prompt=base_prompt)
```

Agent 路由在 `create_app()` 中**始终**注册（`web/agent.py`）。聊天室 agent 由 `_build_chat_agent` per-session 惰性构建，在没有模型配置时 WebSocket 端点返回"模型未配置"错误。

### 角色编排接线（Phase B/C）

当 `config["personas"]` 非空时，额外构建 `PersonaOrchestrator` 挂 `app.state.persona_orchestrator`；`PersonaRegistry` 挂 `app.state.persona_registry` 供前端 `/api/agent/personas` 拉取。WebSocket chat 消息扩展 `persona`（单角色）/ `personas`（团队会诊）字段，`web/agent.py` 优先路由到 orchestrator/registry，降级到默认聊天 agent（`_build_chat_agent`）。详见 [persona.md](persona.md)。

---

## 4. 数据库表

### 会话管理

```sql
CREATE TABLE IF NOT EXISTS agent_sessions (
    id SERIAL PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '新会话',
    message_count INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS agent_messages (
    id SERIAL PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES agent_sessions(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_agent_messages_session
    ON agent_messages(session_id, created_at);
```

### 记忆存储（Phase 2，无 pgvector）

```sql
CREATE TABLE IF NOT EXISTS agent_memories (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id  TEXT NOT NULL,
    agent_name  TEXT NOT NULL DEFAULT '',
    memory_type TEXT NOT NULL DEFAULT 'summary',
    content     TEXT NOT NULL,
    created_at  TIMESTAMPTZ DEFAULT now(),
    updated_at  TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_memories_session
    ON agent_memories(session_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_memories_search
    ON agent_memories USING GIN (to_tsvector('simple', content));
```

**说明**：记忆系统**不使用 pgvector**。搜索策略：
- CJK 文本 → jieba TF-IDF 提取关键词 → `content ILIKE ANY(patterns)`（GIN + pg_trgm 双重索引）
- ASCII 文本 → `to_tsvector('english', content) @@ to_tsquery('english', ...)`（FTS + ts_rank）
- 索引：`idx_memories_search`（GIN `to_tsvector('english', content)`）+ `idx_memories_search_trgm`（GIN `content gin_trgm_ops`）

### 知识库存储（Phase 3，使用 pgvector）

> 与记忆系统不同：知识库**使用 pgvector** 语义向量检索。详见 [phase3-knowledge.md](phase3-knowledge.md)。

```sql
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS knowledge_chunks (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source      TEXT NOT NULL,
    namespace   TEXT NOT NULL DEFAULT '',
    content     TEXT NOT NULL,
    embedding   vector(1536),
    metadata    JSONB DEFAULT '{}',
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_knowledge_namespace ON knowledge_chunks(namespace);
CREATE INDEX IF NOT EXISTS idx_knowledge_embedding
    ON knowledge_chunks USING hnsw (embedding vector_cosine_ops);
```

**前提**：`docker-compose.yml` 镜像 `postgres:16-alpine` -> `pgvector/pgvector:pg16`（vanilla 镜像无 pgvector 扩展）。

CRUD 方法（`get_conn()` ctx manager）：`ingest_knowledge` / `search_knowledge(embedding, namespace, top_k)` / `delete_knowledge(namespace)` / `count_knowledge(namespace)`。