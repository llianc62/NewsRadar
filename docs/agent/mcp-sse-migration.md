# MCP Server SSE 化改造方案

## 背景

当前问题：每个 agent 创建时都启动一个独立的 `python -m agent.mcp.news_server` stdio 子进程，
浪费资源。多个 agent 并行调用工具时，stdio 管道是串行的，不支持并发。

## 目标

1. 全局只启动**一个** MCP Server 进程（SSE 模式）
2. 每个 agent 通过 HTTP SSE 独立连接，天然支持并发
3. 消除冗余的子进程和 PG 连接

## 配置

在 `config.yaml` 中显式声明 MCP 新闻服务：

```yaml
# MCP 新闻服务 — 独立 HTTP 服务，提供 search_news/get_hot_topics 等工具。
# Agent 通过 SSE 长连接调用，每个 agent 独立连接，天然支持并发。
# 由 daemon 管理生命周期（启动时拉起，关闭时终止）。
mcp_server:
  enabled: true               # 启动时拉起 MCP Server 子进程
  transport: sse              # sse（推荐）| stdio（仅调试）
  host: "0.0.0.0"
  port: 8001
```

`config/loader.py` 新增 `_load_mcp_server_config()` 加载此段，并入 `config["mcp_server"]`。

---

## 架构

```
┌─────────────────────────────────────────────────────────┐
│  Daemon 进程                                            │
│                                                         │
│  ┌─────────────────────────────────────┐                │
│  │  MCP Server (SSE, port 8001)        │                │
│  │  └─ FastMCP async handlers          │                │
│  │     ├─ search_news()                │                │
│  │     ├─ get_hot_topics()             │                │
│  │     ├─ get_news_detail()            │                │
│  │     ├─ analyze_sentiment()          │                │
│  │     └─ get_source_stats()           │                │
│  └──────────────────┬──────────────────┘                │
│                     │                                   │
│        ┌────────────┼────────────┐                      │
│        ▼            ▼            ▼                      │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐                │
│  │ Main     │ │ Buffett  │ │ Graham   │  ...            │
│  │ Agent    │ │ Persona  │ │ Persona  │                │
│  │ SSE conn │ │ SSE conn │ │ SSE conn │                │
│  └──────────┘ └──────────┘ └──────────┘                │
│                                                         │
│  ┌─────────────────────────────────────┐                │
│  │  PostgreSQL Connection Pool         │                │
│  │  (MCP Server 内部共享)               │                │
│  └─────────────────────────────────────┘                │
└─────────────────────────────────────────────────────────┘
```

**关键变化：**
- MCP Server 由 daemon 管理生命周期（启动 → 使用 → 关闭）
- 每个 agent 持有独立的 `MCPClient`，通过 SSE 连接
- SSE 连接是 HTTP 长连接，MCP Server 的 FastMCP 内部 async 处理并发请求
- 不再创建任何 stdio 子进程

---

## 变更清单

### 1. `agent/mcp/mcp_client.py` — 修复 SSE 连接协议

**问题：** 当前 `connect_sse()` 把 POST 请求直接发到 SSE URL（`/sse`），
但 FastMCP 的 SSE 模式使用两个端点：
- `GET /sse` — SSE 事件流（服务端推送）
- `POST /messages/` — 客户端请求

**改动：**

```python
class MCPClient:
    def __init__(self, name: str = ""):
        ...
        self._sse_url: str = ""          # SSE 流端点
        self._message_url: str = ""      # 请求端点
        self._httpx_client = None

    async def connect_sse(self, base_url: str) -> None:
        """连接到 SSE MCP Server。
        
        Args:
            base_url: 如 "http://localhost:8001"
        """
        self._sse_url = base_url.rstrip("/") + "/sse"
        self._message_url = base_url.rstrip("/") + "/messages/"
        self._httpx_client = httpx.AsyncClient()

        # 发送 initialize 请求到 /messages/
        init_result = await self._send_sse_request("initialize", {...})
        if not init_result:
            raise RuntimeError(...)

        self._connected = True
        await self._load_tools_sse()

    async def _send_sse_request(self, method, params=None) -> dict | None:
        """POST 请求发送到 /messages/ 端点。"""
        resp = await self._httpx_client.post(
            self._message_url,  # ← 改这里：发到 /messages/ 而非 /sse
            json=request,
            timeout=30,
        )
        ...
```

### 2. `agent/mcp/__init__.py` — 导出（可能无需改动）

### 3. `agent/mcp/mcp_tool.py` — 添加并发安全

由于多个 agent 共享同一个 MCP Server，但每个 agent 有独立的 `MCPClient`/SSE 连接，
**每个连接是独立的，不需要锁**。FastMCP 内部 async 处理并发请求。

### 4. `agent/factory.py` — 新增 MCP Server 生命周期管理，统一透传 mcp_cfg

所有工厂函数统一接受 `mcp_cfg: dict | None = None` 参数，
值来自 `config["mcp_server"]`，传递链：

```
main.py: mcp_cfg = config["mcp_server"]
  ├─ create_agent(..., mcp_cfg=mcp_cfg)
  └─ create_persona_orchestrator(..., mcp_cfg=mcp_cfg)
       └─ create_persona_manager(..., mcp_cfg=mcp_cfg)
            └─ PersonaManager(..., mcp_cfg=mcp_cfg)
                 └─ get("buffett") → create_persona(..., mcp_cfg=mcp_cfg)
```

```python
# MCP 工具 level 映射（模块级常量，多处复用）
LEVEL_MAP = {
    "search_news": 2,
    "get_hot_topics": 1,
    "get_news_detail": 2,
    "analyze_sentiment": 1,
    "get_source_stats": 1,
}


# 新增：创建并启动共享 MCP Server（SSE 模式）
async def start_mcp_server(mcp_cfg: dict) -> asyncio.subprocess.Process:
    """启动 MCP Server 作为 SSE 服务，返回子进程句柄。

    Args:
        mcp_cfg: ``config["mcp_server"]``，含 enabled/transport/host/port
    """
    proc = await asyncio.create_subprocess_exec(
        "python", "-m", "agent.mcp.news_server",
        "--transport", mcp_cfg["transport"],
        "--port", str(mcp_cfg["port"]),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    # 等待服务就绪（轮询端口）
    await _wait_for_server(mcp_cfg["port"], timeout=10)
    return proc


async def _wait_for_server(port: int, timeout: float = 10.0) -> None:
    """轮询直到 MCP Server 端口可访问。"""
    import httpx
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"http://localhost:{port}/health", timeout=2)
                if resp.status_code < 500:
                    return
        except (httpx.ConnectError, httpx.TimeoutException):
            await asyncio.sleep(0.3)
    raise RuntimeError(f"MCP Server 未在 {timeout}s 内就绪")


# 新增：创建到 MCP Server 的 SSE 连接
async def create_agent_connector(mcp_cfg: dict) -> MCPClient:
    """创建连接到 SSE MCP Server 的客户端。

    Args:
        mcp_cfg: ``config["mcp_server"]``
    """
    base_url = f"http://{mcp_cfg['host']}:{mcp_cfg['port']}"
    client = MCPClient(name="agent")
    await client.connect_sse(base_url)
    return client
```

**修改 `create_agent()`：**

```python
async def create_agent(
    config: dict,
    *,
    system_prompt: str = "",
    max_steps: int = 10,
    register_mcp: bool = True,
    mcp_cfg: dict | None = None,       # 新增：config["mcp_server"]
) -> DefaultAgent:
    registry = setup_builtin_tools()
    if register_mcp and mcp_cfg:
        mcp_client = await create_agent_connector(mcp_cfg)
        registry.add_mcp(mcp_client, level_map=LEVEL_MAP)
    ...
```

**修改 `create_persona()`：**

```python
async def create_persona(
    name: str,
    config: dict,
    *,
    register_mcp: bool = True,
    mcp_cfg: dict | None = None,       # 新增
    ...
):
    ...
    if tools is None and not spec.cls.prefer_direct_executor:
        tools = setup_builtin_tools()
        if register_mcp and mcp_cfg:
            mcp_client = await create_agent_connector(mcp_cfg)
            tools.add_mcp(mcp_client, level_map=LEVEL_MAP)
    ...
```

### 5. `agent/persona/manager.py` — 透传 mcp_cfg

```python
class PersonaManager:
    def __init__(self, models_config, *, mcp_cfg: dict | None = None, ...):
        self._mcp_cfg = mcp_cfg
        ...

    async def get(self, name: str):
        ...
        persona = await create_persona(
            name, self._models,
            register_mcp=True,
            mcp_cfg=self._mcp_cfg,
            ...
        )
```

### 6. `main.py` — 管理 MCP Server 生命周期

```python
class Daemon:
    async def run(self):
        ...
        # 启动 MCP Server (SSE 模式)
        mcp_server_proc = None
        mcp_cfg = self.config.get("mcp_server", {})
        if mcp_cfg.get("enabled") and self.config.get("models"):
            from agent.factory import start_mcp_server
            mcp_server_proc = await start_mcp_server(mcp_cfg)
            print(f"[Daemon] MCP Server started "
                  f"({mcp_cfg['transport']} mode, "
                  f"http://{mcp_cfg['host']}:{mcp_cfg['port']}).")

        # 创建 agent（传入 mcp_cfg）
        agent = await create_agent(
            self.config["models"],
            system_prompt=base_prompt,
            register_mcp=True,
            mcp_cfg=mcp_cfg,
        )

        # 创建角色编排器
        persona_orchestrator = await create_persona_orchestrator(
            self.config, db=self.db, base_prompt=base_prompt, mcp_cfg=mcp_cfg,
        )

        # 关闭时清理
        try:
            await shutdown_event.wait()
        finally:
            if mcp_server_proc:
                mcp_server_proc.terminate()
                await mcp_server_proc.wait()
```

### 7. `agent/tools/tools.py` — 废弃 `get_latest_news` 的 stdio 模式

当前 `get_latest_news` 每次调用都创建一个新的 stdio MCP 子进程：

```python
@tool(level=2, category="news")
async def get_latest_news(query: str = "热点", limit: int = 10) -> str:
    from agent.mcp import MCPClient
    client = MCPClient(name="news-query")
    await client.connect_stdio("python", "-m", "agent.mcp.news_server")
    ...
```

**改为：** 将 `get_latest_news` 的功能合并到 `search_news`（MCP 工具），
或内部使用 SSE 连接（复用全局 MCP Server URL）。

---

## 数据流

```
Agent A 调 search_news("长鑫科技")
  │
  ├─ MCPTool._client.call_tool("search_news", {"query": "长鑫科技"})
  │
  ├─ httpx.POST http://localhost:8001/messages/
  │   {"jsonrpc":"2.0","id":1,"method":"tools/call","params":{...}}
  │
  ├─ FastMCP 接收请求 → search_news(query="长鑫科技")
  │   ├─ PostgreSQL 查询
  │   └─ 返回 JSON 结果
  │
  └─ Agent A 收到结果

Agent B 调 search_news("AI芯片") 同时进行
  │
  ├─ httpx.POST http://localhost:8001/messages/  ← 独立连接，独立请求
  │   {"jsonrpc":"2.0","id":1,"method":"tools/call","params":{...}}
  │
  ├─ FastMCP 并发处理（async）
  └─ Agent B 收到结果
```

两个请求互不干扰，FastMCP 的 async handler 并发处理。

---

## 资源对比

| 资源 | 当前 (stdio 多进程) | SSE 方案 |
|------|-------------------|---------|
| MCP 子进程数 | 1 + N (角色数) | **1** |
| PG 连接数 | 1 + N (每个子进程一个) | **1**（MCP Server 内部） |
| 每个 agent 连接开销 | ~100MB 子进程 | **~0**（HTTP 长连接） |
| 并发能力 | 串行（stdio 管道） | **并行**（FastMCP async） |
| 容错性 | 1 子进程挂 → 只影响该 agent | 1 连接挂 → 只影响该 agent |

---

## 风险

1. **MCP Server 启动时机**：Daemon 启动时需要等 MCP Server 就绪再创建 agent，
   但 LLM client 初始化不依赖 MCP Server，可以用 `_wait_for_server()` 异步等待

2. **MCP Server 挂掉**：所有 agent 的 MCP 工具同时不可用。需要 daemon 监控重启

3. **`connect_sse` 协议兼容性**：当前 `_send_sse_request` 发 POST 到 `/sse`，
   FastMCP 的 SSE 模式期望 POST 到 `/messages/`。需要验证 FastMCP 的行为

4. **端口冲突**：如果 8001 被占用，MCP Server 启动失败。需要可配置端口

---

## 实施步骤

1. 修复 `MCPClient.connect_sse()` — 正确分离 `/sse` 和 `/messages/` 端点
2. `factory.py` 新增 `start_mcp_server()` / `create_agent_connector()`
3. `factory.py` 修改 `create_agent()` / `create_persona()` / `create_persona_manager()` / `create_persona_orchestrator()` — 接受 `mcp_cfg: dict | None` 参数
4. `PersonaManager` 接受并透传 `mcp_cfg`
5. `main.py` 管理 MCP Server 生命周期
6. 废弃 `get_latest_news` 的 stdio 模式
7. 测试：单 agent 聊天、团队会诊并发、MCP Server 重启