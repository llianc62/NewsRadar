"""MCP Client — 基于官方 mcp SDK 的 ClientSession 封装。

使用 ``mcp.ClientSession`` + ``mcp.client.sse.sse_client`` 代替自实现 JSON-RPC 2.0 协议层。

用法:
    from agent.mcp.client import MCPClient

    # SSE 模式
    session = await MCPClient.connect_sse("http://localhost:8001")
    tools = await session.list_tools()
    result = await session.call_tool("search_news", {"query": "AI"})
    await session.close()

    # stdio 模式
    session = await MCPClient.connect_stdio("python", "-m", "agent.mcp.news_server")
    result = await session.call_tool("search_news", {"query": "AI"})
    await session.close()
"""

from __future__ import annotations

from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client


class MCPClient:
    """MCP 会话——封装 ``ClientSession`` + 传输层生命周期。

    提供 ``connect_sse`` / ``connect_stdio`` 工厂方法，
    自动建立传输连接、初始化会话、加载工具列表。
    兼容 ``MCPTool`` 和 ``Registry.add_mcp()`` 的接口约定。

    注意:
        由于 ``stdio_client`` 内部使用 ``anyio`` cancel scopes，
        ``close()`` 必须在调用 ``connect_*`` 的同一 asyncio 任务中执行。
    """

    def __init__(self) -> None:
        self._session: ClientSession | None = None
        self._tools: list[dict] = []
        self._streams_ctx = None  # 传输层上下文管理器
        self._session_ctx = None  # ClientSession 上下文管理器
        self._connected = False

    # ── 工厂方法 ────────────────────────────────────────────────

    @classmethod
    async def connect_sse(cls, base_url: str) -> MCPClient:
        """通过 SSE 连接到 MCP Server。

        Args:
            base_url: HTTP 基础地址，如 ``"http://localhost:8001"``

        Returns:
            已初始化并加载好工具列表的 ``MCPClient`` 实例。
        """
        self = cls()
        url = base_url.rstrip("/") + "/sse"
        sse_ctx = sse_client(url)
        streams = await sse_ctx.__aenter__()
        self._streams_ctx = sse_ctx
        self._streams = streams
        read, write = streams
        session_ctx = ClientSession(read, write)
        session = await session_ctx.__aenter__()
        self._session_ctx = session_ctx
        self._session = session
        await session.initialize()
        self._connected = True
        await self._load_tools()
        return self

    @classmethod
    async def connect_stdio(cls, command: str, *args: str) -> MCPClient:
        """通过子进程 stdio 连接到 MCP Server。

        Args:
            command: 可执行文件路径
            *args: 命令行参数
        """
        self = cls()
        params = StdioServerParameters(command=command, args=list(args))
        stdio_ctx = stdio_client(params)
        streams = await stdio_ctx.__aenter__()
        self._streams_ctx = stdio_ctx
        self._streams = streams
        read, write = streams
        session_ctx = ClientSession(read, write)
        session = await session_ctx.__aenter__()
        self._session_ctx = session_ctx
        self._session = session
        await session.initialize()
        self._connected = True
        await self._load_tools()
        return self

    # ── 内部方法 ────────────────────────────────────────────────

    async def _load_tools(self) -> None:
        """从 MCP Server 加载工具列表。"""
        if self._session is None:
            return
        result = await self._session.list_tools()
        self._tools = [
            {
                "name": t.name,
                "description": t.description or "",
                "inputSchema": t.inputSchema,
            }
            for t in result.tools
        ]

    # ── 公开 API ────────────────────────────────────────────────

    async def call_tool(self, name: str, args: dict[str, Any] | None = None) -> str:
        """调用 MCP 工具。

        Args:
            name: 工具名
            args: 工具参数字典

        Returns:
            工具执行结果的文本内容
        """
        if self._session is None:
            raise RuntimeError("MCPClient not connected")
        result = await self._session.call_tool(name, arguments=args or {})
        if result.isError:
            error_text = ""
            for item in result.content:
                if hasattr(item, "text") and item.text:
                    error_text += item.text + "\n"
            raise RuntimeError(error_text.strip() or f"Tool '{name}' execution failed")
        parts = [
            item.text
            for item in result.content
            if hasattr(item, "type") and item.type == "text" and item.text
        ]
        return "\n".join(parts)

    async def list_tools(self) -> list[dict]:
        """返回工具列表（每个工具含 name/description/inputSchema）。"""
        return list(self._tools)

    def get_tools(self) -> list[dict]:
        """返回工具列表快照，兼容 ``Registry.add_mcp()`` 接口。"""
        return list(self._tools)

    def get_schemas(self) -> list[dict]:
        """返回 OpenAI format 工具 schema 列表。"""
        schemas = []
        for t in self._tools:
            schemas.append({
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("inputSchema", {}),
                },
            })
        return schemas

    def has_tool(self, name: str) -> bool:
        """检查工具是否存在。"""
        return any(t["name"] == name for t in self._tools)

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def close(self) -> None:
        """清理：逆序退出上下文管理器。"""
        self._connected = False
        self._session = None
        self._tools = []
        # 先退出 session，再退出传输层（逆序）
        if self._session_ctx is not None:
            try:
                await self._session_ctx.__aexit__(None, None, None)
            except Exception:
                pass
            self._session_ctx = None
        if self._streams_ctx is not None:
            try:
                await self._streams_ctx.__aexit__(None, None, None)
            except Exception:
                pass
            self._streams_ctx = None