# Agent 与现有系统的融合

> **父文档**: [index.md](index.md)

---

## 1. 改动最小化

- **不改现有新闻管线一行代码**。Agent 子系统是 `agent/` 下的独立模块
- **config.yaml 加 `llm:` + `agent:` 段**——不改变已有配置结构，只是新增节
- **`web/app.py` 加 `register_agent_routes()`**——作为额外路由注册，不修改现有路由
- **`web/templates/base.html` 侧边栏加一个入口**——不改变已有组件
- **`storage/postgres.py` 加 `_init_agent_schema()`**——新增表不干扰现有表
- **`web/app.py` 移除 SSE 通知端点**——`/api/notifications/stream` 替换为 WS `type: "notification"`

---

## 2. 通知系统迁移

现有通知系统使用 SSE（`/api/notifications/stream`），Phase 0 实施时将其迁移到统一的 WebSocket 通道：

| 组件 | 现状（SSE） | 迁移后（WS） |
|------|------------|-------------|
| 通知推送 | `_push_sse_event()` 推 SSE | 遍历 WS 连接池，推 `{"type":"notification",...}` |
| 客户端接收 | `EventSource` 监听 `new`/`update` 事件 | `WebSocket.onmessage` 按 `type` 分发 |
| 心跳 | SSE 30s keepalive | WebSocket 自带 ping/pong |
| 重连 | EventSource 自动 | 前端手动重连（带退避）|

**服务端 WS 连接管理：**
```python
# 取代 _sse_clients: set[asyncio.Queue]
_ws_clients: dict[int, WebSocket] = {}  # id → WebSocket 连接

def _push_notification(data: dict) -> None:
    """遍历所有 WS 连接，推送通知。"""
    for ws in list(_ws_clients.values()):
        try:
            ws.send_json({"type": "notification", "notification": data})
        except Exception:
            pass  # 连接已断开，忽略

async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    client_id = id(ws)
    _ws_clients[client_id] = ws
    try:
        while True:
            data = await ws.receive_json()
            await handle_ws_message(ws, data)
    except WebSocketDisconnect:
        _ws_clients.pop(client_id, None)
```

---

## 3. main.py 改动

```python
# main.py 中 create_app() 之后
if config.get("llm"):
    from web.app import register_agent_routes
    register_agent_routes(app, config, db)
```

`config` 中没有 `llm` 段时，agent 路由完全不注册，侧边栏不显示入口，零影响。

---

## 4. 数据库表

### Phase 0：会话管理

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

### Phase 2：记忆存储

```sql
-- 需要 pgvector 扩展
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS agent_memories (
    id SERIAL PRIMARY KEY,
    key TEXT NOT NULL,
    content TEXT NOT NULL,
    embedding vector(1536),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(key)
);

CREATE INDEX IF NOT EXISTS idx_agent_memories_embedding
    ON agent_memories USING ivfflat (embedding vector_cosine_ops);
```

---

## 5. schema 初始化

在 `storage/postgres.py` 中新增：

```python
def _init_agent_schema(self) -> None:
    """初始化 agent 子系统所需的所有表（幂等）。"""
    # Phase 0: 会话表
    self.execute("""
        CREATE TABLE IF NOT EXISTS agent_sessions (
            id SERIAL PRIMARY KEY,
            title TEXT NOT NULL DEFAULT '新会话',
            message_count INTEGER DEFAULT 0,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        );
    """)
    self.execute("""
        CREATE TABLE IF NOT EXISTS agent_messages (
            id SERIAL PRIMARY KEY,
            session_id INTEGER NOT NULL REFERENCES agent_sessions(id) ON DELETE CASCADE,
            role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
            content TEXT NOT NULL,
            created_at TIMESTAMPTZ DEFAULT NOW()
        );
    """)
```