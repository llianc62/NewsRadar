"""AgentFactory — 统一构建带工具的 DefaultAgent。

简化 Agent 的创建流程，自动装配 ReActExecutor、内置工具、
MCP 工具等依赖。
"""

from __future__ import annotations

from .agent import DefaultAgent
from .executor import ReActExecutor
from .tools import Registry
from .tools.tools import setup_builtin_tools


async def create_agent(
    config: dict,
    *,
    system_prompt: str = "",
    max_steps: int = 10,
    register_mcp: bool = True,
) -> DefaultAgent:
    """创建配置完整的 DefaultAgent（ReActExecutor + 工具）。

    Args:
        config: 模型配置 dict（即 config.yaml 的 models 段）
        system_prompt: 系统提示词
        max_steps: ReAct 循环最大步数
        register_mcp: 是否自动连接并注册 News MCP Server 的工具

    Returns:
        已装配好 ReActExecutor 和 Registry 的 DefaultAgent

    用法:
        agent = await create_agent(config={
            "default": {"protocol": "openai", "model": "gpt-4o", "api_key": "..."},
        })
    """
    # 1. 创建内置工具注册表
    registry = setup_builtin_tools()

    # 2. 连接 MCP Server 并注册其工具
    if register_mcp:
        await _register_mcp_tools(registry)

    # 3. 创建 ReActExecutor
    executor = ReActExecutor(max_steps=max_steps)

    # 4. 创建 Agent
    agent = DefaultAgent(
        config=config,
        executor=executor,
        tools=registry,
        system_prompt=system_prompt,
    )
    return agent


async def _register_mcp_tools(registry: Registry) -> None:
    """连接 News MCP Server 并注册其工具到 Registry。

    如果连接失败（如 news_server 不可用），静默跳过——不影响 Agent 启动。
    """
    from agent.mcp import MCPClient

    # MCP 工具 level 映射
    level_map = {
        "search_news": 2,
        "get_hot_topics": 1,
        "get_news_detail": 2,
        "analyze_sentiment": 1,
        "get_source_stats": 1,
    }

    try:
        client = MCPClient(name="news-mcp")
        await client.connect_stdio("python", "-m", "agent.mcp.news_server")
        registry.add_mcp(client, level_map=level_map)
    except Exception:
        pass  # MCP 不可用时静默跳过
