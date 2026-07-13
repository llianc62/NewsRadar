"""MCP Client — lightweight MCP protocol implementation.

不需要 mcp Python SDK，使用 JSON-RPC 2.0 over stdio/SSE 实现。

MCP (Model Context Protocol) 协议：
- list_tools → 获取工具列表
- call_tool → 调用工具
- 传输层：stdio（子进程管道）或 SSE（HTTP）
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from typing import Any


class MCPClient:
    """MCP 协议客户端——连接到一个 MCP Server。

    支持两种传输方式：
    - stdio：服务端作为子进程运行（stdin/stdout 管道）
    - SSE：  服务端作为 HTTP 服务运行（Server-Sent Events）

    使用方式:
        client = MCPClient("news-radar")
        await client.connect_stdio("python", "-m", "agent.mcp.news_server")
        tools = client.get_tools()
        result = await client.call_tool("search_news", {"query": "AI"})
        await client.close()
    """

    def __init__(self, name: str = ""):
        if not name.strip():
            raise ValueError("MCPClient name must not be empty")
        self._name = name
        self._tools: list[dict] = []
        self._proc: subprocess.Popen | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._request_id = 0
        self._connected = False

    # ── Connection ────────────────────────────────────────────────

    async def connect_stdio(self, command: str, *args: str) -> None:
        """通过子进程 stdio 连接 MCP Server。

        Agent 内部启动的子进程 MCP Server，零网络延迟。

        Args:
            command: 可执行命令（如 "python"）
            *args: 命令参数（如 "-m", "agent.mcp.news_server"）
        """
        if self._connected:
            raise RuntimeError(f"MCPClient '{self._name}' already connected")

        self._proc = await asyncio.create_subprocess_exec(
            command, *args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        # create_subprocess_exec 返回的 stdin/stdout 已经是
        # StreamWriter / StreamReader，可直接使用
        self._writer = self._proc.stdin
        self._reader = self._proc.stdout

        self._connected = True
        await self._initialize()
        await self._load_tools()

    async def connect_sse(self, url: str) -> None:
        """通过 SSE 连接远程 MCP Server（HTTP 流）。

        Args:
            url: MCP Server 的 HTTP 端点（如 "http://localhost:8000/mcp"）
        """
        if self._connected:
            raise RuntimeError(f"MCPClient '{self._name}' already connected")

        import httpx

        # MCP over SSE: 发送初始化请求，建立会话
        self._sse_url = url
        self._httpx_client = httpx.AsyncClient()

        # 先发送 initialize 请求
        init_result = await self._send_sse_request("initialize", {
            "protocolVersion": "0.1.0",
            "capabilities": {},
            "clientInfo": {"name": "NewsRadarAgent", "version": "1.0.0"},
        })
        if not init_result:
            raise RuntimeError(f"Failed to initialize MCP session at {url}")

        self._connected = True
        await self._load_tools_sse()

    # ── Internal: stdio JSON-RPC ──────────────────────────────────

    async def _send_request(self, method: str, params: dict | None = None) -> dict | None:
        """发送 JSON-RPC 请求到 stdio MCP Server。"""
        if not self._writer or not self._reader:
            raise RuntimeError(f"MCPClient '{self._name}' not connected")

        self._request_id += 1
        request = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
            "params": params or {},
        }
        data = (json.dumps(request) + "\n").encode("utf-8")
        self._writer.write(data)
        await self._writer.drain()

        # 读取响应
        response_line = await self._reader.readline()
        if not response_line:
            return None

        try:
            response = json.loads(response_line.decode("utf-8"))
        except json.JSONDecodeError:
            return None

        if "error" in response and response["error"]:
            raise RuntimeError(
                f"MCP request '{method}' failed: {response['error']}"
            )
        return response.get("result")

    async def _initialize(self) -> None:
        """初始化 MCP 会话。"""
        result = await self._send_request("initialize", {
            "protocolVersion": "0.1.0",
            "capabilities": {},
            "clientInfo": {"name": "NewsRadarAgent", "version": "1.0.0"},
        })
        if result is None:
            raise RuntimeError(f"MCPClient '{self._name}' initialize failed")

    async def _load_tools(self) -> None:
        """加载 MCP Server 的工具列表。"""
        result = await self._send_request("tools/list")
        if result and "tools" in result:
            self._tools = result["tools"]

    # ── Internal: SSE JSON-RPC ────────────────────────────────────

    async def _send_sse_request(self, method: str, params: dict | None = None) -> dict | None:
        """通过 HTTP POST 发送 JSON-RPC 请求到 SSE MCP Server。"""
        import httpx

        self._request_id += 1
        request = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
            "params": params or {},
        }
        try:
            resp = await self._httpx_client.post(
                self._sse_url,
                json=request,
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            if "error" in data and data["error"]:
                raise RuntimeError(
                    f"MCP request '{method}' failed: {data['error']}"
                )
            return data.get("result")
        except Exception as e:
            raise RuntimeError(
                f"MCP SSE request '{method}' failed: {e}"
            ) from e

    async def _load_tools_sse(self) -> None:
        """通过 SSE 加载工具列表。"""
        result = await self._send_sse_request("tools/list")
        if result and "tools" in result:
            self._tools = result["tools"]

    # ── Public API ────────────────────────────────────────────────

    async def call_tool(self, name: str, args: dict) -> str:
        """调用工具，返回文本结果。

        Args:
            name: 工具名
            args: 参数字典

        Returns:
            工具的文本输出

        Raises:
            RuntimeError: 未连接或调用失败
        """
        if not self._connected:
            raise RuntimeError(f"MCPClient '{self._name}' not connected")

        if self._proc is not None:
            # stdio 模式
            result = await self._send_request("tools/call", {
                "name": name,
                "arguments": args,
            })
        else:
            # SSE 模式
            result = await self._send_sse_request("tools/call", {
                "name": name,
                "arguments": args,
            })

        if result is None:
            raise RuntimeError(f"Tool '{name}' returned no result")

        # FastMCP 将未知工具等错误放在 isError 中，而非 JSON-RPC error
        if result.get("isError"):
            error_text = ""
            if "content" in result:
                texts = [
                    item.get("text", "")
                    for item in result["content"]
                    if isinstance(item, dict) and item.get("type") == "text"
                ]
                error_text = "\n".join(texts)
            raise RuntimeError(error_text or f"Tool '{name}' execution failed")

        # 解析 MCP 工具返回格式
        if "content" in result:
            parts = []
            for item in result["content"]:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(item.get("text", ""))
            return "\n".join(parts)

        return str(result)

    async def close(self) -> None:
        """关闭连接，清理资源。"""
        self._connected = False
        if self._writer:
            try:
                self._writer.close()
            except Exception:
                pass
            self._writer = None
        if self._proc and self._proc.returncode is None:
            try:
                self._proc.terminate()
                await self._proc.wait()
            except Exception:
                pass
            self._proc = None
        # 清理 SSE 资源
        if hasattr(self, "_httpx_client"):
            try:
                await self._httpx_client.aclose()
            except Exception:
                pass

    def has_tool(self, name: str) -> bool:
        """检查本连接是否有指定工具。"""
        return any(t["name"] == name for t in self._tools)

    def get_tools(self) -> list[dict]:
        """返回原始工具列表。"""
        return list(self._tools)

    def get_schemas(self) -> list[dict]:
        """返回本连接所有工具的 OpenAI format schema。

        输出格式:
            [
                {
                    "type": "function",
                    "function": {
                        "name": "...",
                        "description": "...",
                        "parameters": {...},
                    },
                },
            ]
        """
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

    @property
    def name(self) -> str:
        return self._name

    @property
    def is_connected(self) -> bool:
        return self._connected
