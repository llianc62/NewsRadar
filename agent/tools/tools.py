"""内置工具集——展示 @tool 装饰器的各种用法，集中管理所有内置函数工具。

使用 @tool 装饰器实现「就近注册」：
- 每个工具在函数定义处用 @tool 装饰
- 由 setup_builtin_tools() 统一添加到 Registry

用法:
    from agent.tools import Registry
    from agent.tools.tools import setup_builtin_tools

    registry = setup_builtin_tools()
"""

from __future__ import annotations

import httpx
import random
from datetime import datetime

from .base import tool
from .registry import Registry


# ── 基础用法：@tool 无参数 ──────────────────────────────────────


@tool(category="general")
def get_current_time() -> str:
    """获取当前的日期和时间（北京时间）。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@tool(category="general")
def get_random_number(max_value: int = 100) -> int:
    """生成一个随机整数。

    Args:
        max_value: 随机数的最大值（默认 100）
    """
    return random.randint(0, max_value)


# ── 指定名称和描述 ────────────────────────────────────────────


@tool(name="calculator", description="执行四则运算，支持加/减/乘/除", category="general")
def calc(a: float, b: float, op: str) -> str:
    """计算器。

    Args:
        a: 第一个数字
        b: 第二个数字
        op: 运算符，可选值 +, -, *, /
    """
    ops = {"+": a + b, "-": a - b, "*": a * b, "/": a / b if b != 0 else float("nan")}
    result = ops.get(op, float("nan"))
    return f"{a} {op} {b} = {result}"


# ── 带复杂类型的参数 ──────────────────────────────────────────


@tool(category="general")
def roll_dice(count: int = 2, sides: int = 6) -> list[int]:
    """掷骰子。

    Args:
        count: 骰子数量（默认 2）
        sides: 骰子面数（默认 6）
    """
    return [random.randint(1, sides) for _ in range(count)]


# ── 天气工具 ────────────────────────────────────────────────────────


@tool(level=1, category="general")
async def get_current_weather(city: str = "北京") -> str:
    """获取指定城市的当前天气。

    通过 wttr.in 公开 API 查询天气，支持中文城市名。

    Args:
        city: 城市名称（默认北京）
    """
    url = f"https://wttr.in/{city}?format=%C+%t+%h+%w"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            text = resp.text.strip()
            return f"{city} 今日天气: {text}"
    except Exception as e:
        return f"获取 {city} 天气失败: {e}"


# ── 新闻查询工具（通过 MCP Server） ──────────────────────────────


@tool(level=2, category="news")
async def get_latest_news(query: str = "热点", limit: int = 10) -> str:
    """查询最新新闻，通过 News MCP Server 获取。

    支持关键词搜索，返回新闻标题、来源、热度等摘要信息。

    Args:
        query: 搜索关键词（默认"热点"）
        limit: 返回条数（默认 10）
    """
    from agent.mcp import MCPClient

    client = MCPClient(name="news-query")
    await client.connect_stdio("python", "-m", "agent.mcp.news_server")
    try:
        result = await client.call_tool("search_news", {"query": query, "limit": limit})
        return result
    finally:
        await client.close()


# ── 批量注册 ────────────────────────────────────────────────────


def setup_builtin_tools() -> Registry:
    """创建 Registry 并注册所有内置工具。

    @tool 装饰的工具本身就是 FunctionTool 实例，
    直接 add_tool 即可注册。
    """
    registry = Registry()
    registry.add_tool(get_current_time)
    registry.add_tool(get_random_number)
    registry.add_tool(calc)
    registry.add_tool(roll_dice)
    registry.add_tool(get_current_weather)
    registry.add_tool(get_latest_news)
    return registry
