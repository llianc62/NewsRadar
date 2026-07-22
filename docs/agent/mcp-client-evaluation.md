# MCP 客户端方案评估：FastMCP SDK vs 自实现 MCPClient

## 背景

当前 `news_server.py` 使用官方 MCP Python SDK 的 `FastMCP` 构建服务器端，但客户端侧是自实现的 `MCPClient`（`agent/mcp/mcp_client.py`），手动处理 JSON-RPC 2.0 握手、stdio 管道、SSE chunked 传输等底层协议细节。

本文评估能否用官方 SDK 的 `Client` 替代自实现，以及是否能满足 [mcp-sse-migration.md](./mcp-sse-migration.md) 的迁移需求。

---

## 症状对比

### 自实现 MCPClient（当前）

482 行自维护代码，涵盖：

| 层次 | 实现方式 |
|------|---------|
| 传输层 | `asyncio.create_subprocess_exec` + 原生 TCP 逐字节解析 HTTP chunked 响应 |
| 协议层 | 手写 JSON-RPC 2.0 请求/响应序列化 |
| 握手 | 手动 `initialize` → `tools/list` 两步 |
| SSE 连接 | 原生 TCP 发 HTTP 请求、逐字节读响应头、解析 chunked body |
| 响应路由 | 自行从 SSE 流中匹配 `data:` 行，反向关联 `id` 字段 |
| 错误处理 | 只覆盖基本 `isError` 检查，无超时/重试/断开恢复 |

### 官方 MCP SDK Client（`mcp` 包）

```python
from mcp import Client
from mcp.client.sse import sse_client

# stdio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
```

SDK 提供三层接口：

| 层次 | SDK 组件 | 职责 |
|------|---------|------|
| **高阶** `Client` | `mcp.Client` | 自动初始化 + 协议协商，`list_tools()` / `call_tool()` 一步到位 |
| **中阶** `ClientSession` | `mcp.ClientSession` | 精细控制会话生命周期，支持 `resources/list` / `prompts/get` 等全部 MCP 方法 |
| **传输层** | `stdio_client` / `sse_client` | 纯传输抽象，返回 `(read, write)` 流，协议无关 |

---

## 方案对比

### 1. 传输层：原生 TCP 手写 vs SDK sse_client

**自实现（当前）：** `MCPClient.connect_sse()` 用原生 TCP 写 HTTP 请求头、逐字节解析 chunked 响应。需要手写 `_read_sse_endpoint()` 解析 HTTP streaming 的 `data:` 行。

**SDK：** `sse_client(url)` 使用 `httpx` + `sse-starlette` 的 SSE 客户端，自动处理：

- HTTP 连接建立
- `endpoint` 事件的接收与 session_id 提取
- 后续 POST 请求路由到 `/messages/?session_id=xxx`
- 连接超时（`timeout`）和 SSE 读超时（`sse_read_timeout`）

```python
# SDK 一行代码 = 自实现 80 行
async with sse_client("http://localhost:8001/sse") as streams:
    async with ClientSession(*streams) as session:
        await session.initialize()
        tools = await session.list_tools()
        result = await session.call_tool("search_news", {"query": "AI"})
```

### 2. 协议层：JSON-RPC 手写 vs SDK 自动处理

**自实现（当前）：** 手写 `_send_request()` / `_sse_call()` 构造 JSON-RPC 2.0 帧、管理 `_request_id` 自增计数器、解析响应中的 `result` / `error` 字段。

**SDK：** `ClientSession` 自动管理 JSON-RPC 2.0 协议：

- 自动递增 request_id
- 自动匹配 request → response（SSE 流中响应可能乱序到达）
- 自动处理 `initialize` 协议协商（protocolVersion、capabilities）
- 原生 `CallToolResult` 对象，无需手动解析 content 数组

### 3. SSE 连接并发：两者都 OK

``mcp-sse-migration.md`` 的核心需求是「每个 agent 有独立 SSE 连接，FastMCP 服务端 async 并发处理」。SDK 的 `sse_client` 每个调用创建独立 httpx 连接，天然满足这一需求——和自实现一样。

---

## 迁移方案评估

对照 `mcp-sse-migration.md` 的 7 个实施步骤：

### ✅ 步骤 1：修复 `MCPClient.connect_sse()` — SDK 直接解决

SDK 的 `sse_client` 已经正确处理了：
- `GET /sse` 建立 SSE 流
- 从 `endpoint` 事件提取 `/messages/` URL 和 session_id
- `POST /messages/` 发送 JSON-RPC 请求

**结论：** 不需要「修复」，直接替换。

### ✅ 步骤 2：`factory.py` 新增 `start_mcp_server()` / `create_agent_connector()`

`start_mcp_server()` 与客户端无关，保持不变。`create_agent_connector()` 可以从：

```python
# 当前（自实现）
client = MCPClient(name="agent")
await client.connect_sse(base_url)

# 改为（SDK）
async def create_agent_connector(mcp_cfg: dict) -> ClientSession:
    url = f"http://{mcp_cfg['host']}:{mcp_cfg['port']}/sse"
    streams = await sse_client(url).__aenter__()
    session = ClientSession(*streams)
    await session.__aenter__()
    await session.initialize()
    return session
```

### ✅ 步骤 3：`factory.py` 修改 `create_agent()` / `create_persona()` — 需适配

`add_mcp()` 方法当前接受 `MCPClient` 实例。需要将 `MCPTool` 改为适配 `ClientSession` 的接口，或新增 `MCPSessionTool`。

### ✅ 步骤 4：`PersonaManager` 透传 mcp_cfg — 不受影响

这是架构层面的参数传递，与客户端实现无关。

### ✅ 步骤 5：`main.py` 管理 MCP Server 生命周期 — 不受影响

MCP Server 的启动/关闭逻辑不变，与客户端无关。

### ✅ 步骤 6：废弃 `get_latest_news` 的 stdio 模式 — SDK 简化

当前 `get_latest_news` 每次调用都 `MCPClient(name="news-query").connect_stdio(...)`。改用 SDK 后：

```python
# 可以直接复用全局 SSE 连接，无需每次开子进程
```

### ✅ 步骤 7：测试 — SDK 的测试覆盖更全

SDK 自身有完整的测试套件。我们只需测试 `MCPTool` 适配层。

---

## 风险分析

### 风险 1：SDK 的 SSE 传输是「legacy」状态

文档标记 `sse_client` 为 **legacy**，已被 Streamable HTTP 替代。但：
- FastMCP 服务器端目前仍使用 SSE 传输
- 官方 SDK 明确表示 SSE 兼容旧服务器
- MCP 协议仍然在工作组讨论中，Streamable HTTP 尚未成为标准

**缓解：** 在 `MCPTool` 抽象层背后封装传输选择，未来迁移到 Streamable HTTP 只需替换 transport 实现。

### 风险 2：当前 `mcp` 包未安装

`mcp` 包（官方 Anthropic MCP SDK）不在 `pyproject.toml` 中。需要添加依赖：

```bash
uv add mcp
```

`mcp` 包依赖 `httpx`、`sse-starlette`、`pydantic`，与现有依赖兼容。

### 风险 3：`MCPTool` 的 `BaseTool` 接口适配

当前 `MCPTool` 直接持有 `MCPClient` 实例。改用 `ClientSession` 后，`MCPTool.execute()` 需要调用 `session.call_tool()` 而不是 `client.call_tool()`。变更范围：

| 文件 | 变更量 |
|------|--------|
| `agent/mcp/mcp_client.py` | **删除**（482 行） |
| `agent/mcp/mcp_tool.py` | 修改 `__init__` 接受 `ClientSession`，`execute()` 适配 `CallToolResult` 解析 |
| `agent/mcp/__init__.py` | 修改导出 |
| `agent/tools/registry.py` | `add_mcp()` 签名改为接受 `ClientSession` |
| `agent/tools/tools.py` | `get_latest_news` 改用全局 session |
| `agent/factory.py` | `_create_agent_connector()` 改用 SDK |
| 测试文件 | 更新 mock 方式 |

---

## 建议

### 推荐：用官方 SDK `ClientSession` + `sse_client` 替代自实现

**理由：**

1. **减少 482 行手写代码** — 去掉最脆弱的协议层和传输层
2. **协议正确性** — SDK 由 MCP 协议作者维护，处理了边缘情况（乱序响应、连接断开、协议版本协商）
3. **并发能力** — 每个 agent 独立 `sse_client` 连接，SDK 使用 httpx 异步 client，天然支持并发
4. **不增加额外依赖** — `mcp` 包（官方 Anthropic SDK）已有 `FastMCP` 服务器端，加客户端是同一包

### 不推荐的方案

- **FastMCP（Prefect 版，`fastmcp` PyPI 包）**：这是另一个框架，与当前 `from mcp.server.fastmcp import FastMCP` 冲突，且两者同名不同源，会造成混淆
- **继续自实现**：问题已经在文档中暴露——`connect_sse()` 的 HTTP chunked 解析脆弱、协议版本硬编码 `0.1.0`、缺少重试和超时

### 实施步骤

1. `uv add mcp` 添加依赖
2. 新建 `agent/mcp/client.py` — 用 SDK `ClientSession` + `sse_client` 封装
3. 修改 `MCPTool` — 接受 `ClientSession` 而非 `MCPClient`
4. 修改 `Registry.add_mcp()` — 适配新签名
5. 删除 `agent/mcp/mcp_client.py`
6. 更新 `agent/mcp/__init__.py` 导出
7. 更新 `factory.py` 的 `_create_agent_connector` / `_register_mcp_tools`
8. 更新 `get_latest_news` 改用全局 SSE session
9. 更新测试

---

## 总结

| 需求 | 自实现 MCPClient | 官方 SDK Client |
|------|-----------------|----------------|
| SSE 连接 | ✅ 脆弱 | ✅ 健壮 |
| 协议兼容性 | ⚠️ 版本 0.1.0 硬编码 | ✅ 自动协商 |
| 并发支持 | ✅ 独立连接 | ✅ 独立连接 |
| 代码量 | 482 行 | ~20 行封装 |
| 依赖 | 无额外 | `mcp` 包（已在用） |
| 维护成本 | 高 | 低（官方维护） |
| 测试覆盖 | 低 | 高 |
| 容错性 | ⚠️ 无重试/超时 | ✅ 可配置超时 |