"""MCP subsystem — MCPClient, MCPTool, and NewsRadar MCP Server."""

from .client import MCPClient
from .mcp_tool import MCPTool

__all__ = [
    "MCPClient",
    "MCPTool",
]
