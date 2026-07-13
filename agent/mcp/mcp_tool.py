"""MCPTool — MCP 工具包装器。

将 MCPClient 中的工具包装为 BaseTool 接口，
使 Registry 可以用统一方式管理 FunctionTool 和 MCPTool。
"""

from __future__ import annotations

from typing import Any

from agent.tools.base import BaseTool, ToolDef
from .mcp_client import MCPClient


class MCPTool(BaseTool):
    """MCP 工具——通过 MCP Client 代理执行。

    不直接持有连接，通过 MCPClient 的 session 转发调用。
    由 Registry.add_mcp() 自动创建。
    """

    def __init__(self, client: MCPClient, tool_info: dict, level: int = 1):
        if not isinstance(client, MCPClient):
            raise TypeError("client must be an MCPClient instance")
        if not tool_info.get("name"):
            raise ValueError("tool_info must have a 'name' field")
        self._client = client
        self._name = tool_info["name"]
        self._description = tool_info.get("description", "")
        self._input_schema = tool_info.get("inputSchema", {})
        self._level = level

    @property
    def level(self) -> int:
        return self._level

    def get_def(self) -> ToolDef:
        return ToolDef(self._name, self._description, self._input_schema, level=self._level)

    async def execute(self, **kwargs: Any) -> str:
        try:
            return await self._client.call_tool(self._name, kwargs)
        except Exception as e:
            return f"Error executing MCP tool '{self._name}': {e}"
