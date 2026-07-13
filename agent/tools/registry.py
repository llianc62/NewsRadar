"""Registry — 工具注册中心。

管理 Agent 的所有可用工具（FunctionTool + MCPTool），
对外输出统一的 OpenAI format tool schema。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .base import BaseTool

if TYPE_CHECKING:
    from agent.mcp import MCPClient


class Registry:
    """工具注册中心——管理 Agent 的所有可用工具。

    同时持有 FunctionTool 和 MCPTool，对外输出统一的 schema。
    """

    def __init__(self):
        self._tools: dict[str, BaseTool] = {}

    def add_tool(self, tool: BaseTool) -> None:
        """注册一个工具（FunctionTool 或 MCPTool）。

        Args:
            tool: BaseTool 实例

        Raises:
            TypeError: tool 不是 BaseTool 实例
            ValueError: 工具名重复
        """
        if not isinstance(tool, BaseTool):
            raise TypeError(f"Expected BaseTool instance, got {type(tool)}")
        name = tool.get_def().name
        if name in self._tools:
            raise ValueError(f"Tool '{name}' is already registered")
        self._tools[name] = tool

    def add_mcp(self, client: MCPClient, level_map: dict[str, int] | None = None) -> None:
        """将一个 MCP Client 的所有工具批量注册。

        Args:
            client: 已连接的 MCPClient 实例
            level_map: 可选，工具名到 level 的映射，未指定的工具默认 level=1

        Raises:
            RuntimeError: MCPClient 未连接
        """
        from agent.mcp import MCPTool

        if not client.is_connected:
            raise RuntimeError(
                f"MCPClient '{client.name}' is not connected. "
                "Call connect_stdio() or connect_sse() first."
            )
        for t in client.get_tools():
            name = t["name"]
            if name not in self._tools:
                level = (level_map or {}).get(name, 1)
                self._tools[name] = MCPTool(client, t, level=level)

    def get_schemas(self) -> list[dict]:
        """返回所有工具的 OpenAI format schema 列表。

        输出格式兼容 OpenAI / Anthropic function calling。
        """
        return [self._to_openai_schema(tool) for tool in self._tools.values()]

    async def execute(self, name: str, args: dict) -> str:
        """执行工具调用。

        Args:
            name: 工具名
            args: 参数字典

        Returns:
            工具执行结果的文本

        Raises:
            KeyError: 工具不存在
        """
        tool = self._tools.get(name)
        if not tool:
            raise KeyError(f"Tool '{name}' not found in registry")
        return await tool.execute(**args)

    def get_tool(self, name: str) -> BaseTool | None:
        """按名称获取工具实例。"""
        return self._tools.get(name)

    def get_tool_level(self, name: str) -> int:
        """按名称获取工具等级。

        Raises:
            KeyError: 工具不存在
        """
        tool = self._tools.get(name)
        if not tool:
            raise KeyError(f"Tool '{name}' not found in registry")
        return tool.level

    def list_tools(self) -> list[str]:
        """返回所有注册的工具名列表。"""
        return list(self._tools.keys())

    def remove_tool(self, name: str) -> None:
        """移除已注册的工具。

        Raises:
            KeyError: 工具不存在
        """
        if name not in self._tools:
            raise KeyError(f"Tool '{name}' not found in registry")
        del self._tools[name]

    @staticmethod
    def _to_openai_schema(tool: BaseTool) -> dict:
        d = tool.get_def()
        return {
            "type": "function",
            "function": {
                "name": d.name,
                "description": d.description,
                "parameters": d.input_schema,
            },
        }
