"""AgentFactory — 统一构建带工具的 DefaultAgent。

简化 Agent 的创建流程，自动装配 ReActExecutor、内置工具、
MCP 工具等依赖。
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import time
from typing import TYPE_CHECKING

from .agent import DefaultAgent
from .executor import ReActExecutor
from .memory import LongTermMemory, PgMemoryStorage
from .model_hub import ModelHub
from .data import AgentDefinition
from .tools import Registry
from .tools.tools import setup_builtin_tools

if TYPE_CHECKING:
    from .knowledge import KnowledgeEngine

logger = logging.getLogger("agent.factory")

# MCP 工具 level 映射（模块级常量，多处复用）
LEVEL_MAP = {
    "search_news": 2,
    "get_hot_topics": 1,
    "get_news_detail": 2,
    "analyze_sentiment": 1,
    "get_source_stats": 1,
}


class AgentFactory:
    """从 :class:`AgentDefinition` 组装 :class:`DefaultAgent` 实例。

    将数据层（DB 中的角色定义）与运行时层（DefaultAgent 实例）
    连接起来。解析工具、知识库等依赖，一键构建可运行的 Agent。

    Args:
        models_config: 模型配置 dict（``config["models"]``）。
        db: ``PostgreSQL`` 实例，用于知识库查询与 LongTermMemory 存储。
        registry: 全局工具注册表（``Registry``），所有可用工具。
        top_k: 知识库检索返回片段数，默认 5。
    """

    def __init__(
        self,
        models_config: dict,
        db,
        registry: Registry,
        *,
        base_prompt: str = "",
        top_k: int = 5,
    ):
        self._models_config = models_config
        self._db = db
        self._registry = registry
        self._base_prompt = base_prompt
        self._top_k = top_k

    def build(self, defn: AgentDefinition) -> DefaultAgent:
        """从角色定义构建可运行的 :class:`DefaultAgent`。

        自动解析工具（从全局 Registry 按名查找）、知识库（从 DB
        查询 ``AgentKnowledge`` -> 构建 ``KnowledgeEngine``），
        并装配 ReActExecutor + LongTermMemory。所有运行时组件在构造时
        注入 executor，DefaultAgent 只持有引用。

        Args:
            defn: 角色定义（含 system_prompt、tools、knowledge_id 等）。

        Returns:
            已装配的 :class:`DefaultAgent` 实例。
        """
        tools = self._resolve_tools(defn.tools)
        knowledge, _kb_namespace = self._resolve_knowledge(defn.knowledge_id)
        brain = ModelHub(self._models_config)
        memory = LongTermMemory(self._db, PgMemoryStorage(self._db))
        executor = ReActExecutor(
            brain=brain, memory=memory,
            knowledge=knowledge, tools=tools,
        )

        # 拼接基座提示词 + 角色个性
        full_prompt = defn.system_prompt
        if self._base_prompt:
            full_prompt = self._base_prompt + "\n\n" + full_prompt

        agent = DefaultAgent(
            config=self._models_config,
            executor=executor,
            memory=memory,
            knowledge=knowledge,
            tools=tools,
            system_prompt=full_prompt,
        )
        return agent

    def _resolve_tools(self, tool_names: list[str]) -> Registry:
        """从全局注册表按名解析工具，创建子注册表。

        未知工具名静默跳过（不阻断构建）。
        """
        reg = Registry()
        for name in tool_names:
            tool = self._registry.get_tool(name)
            if tool is not None:
                reg.add_tool(tool)
        return reg

    def _resolve_knowledge(
        self, knowledge_id: str | None
    ) -> tuple[KnowledgeEngine | None, str | None]:
        """从 DB 查询知识库定义，构建 :class:`KnowledgeEngine`。

        ``knowledge_id`` 为 ``None`` 或 DB 中未找到时返回 ``(None, None)``。
        否则返回构建好的 ``KnowledgeEngine``（已绑定 ``namespace``）与
        对应 ``AgentKnowledge.namespace``。Embedding 配置从环境变量读取：
        ``KNOWLEDGE_EMBEDDING_API_KEY`` / ``KNOWLEDGE_EMBEDDING_BASE_URL`` /
        ``KNOWLEDGE_EMBEDDING_MODEL``。
        """
        if not knowledge_id:
            return None, None
        kb = self._db.get_agent_knowledge(knowledge_id)
        if not kb:
            return None, None
        from agent.knowledge import EmbeddingClient, KnowledgeEngine, PgVectorKnowledgeStore

        engine = KnowledgeEngine(
            store=PgVectorKnowledgeStore(self._db),
            embedding=EmbeddingClient(
                api_key=os.environ.get("KNOWLEDGE_EMBEDDING_API_KEY", ""),
                base_url=os.environ.get("KNOWLEDGE_EMBEDDING_BASE_URL", ""),
                model=os.environ.get("KNOWLEDGE_EMBEDDING_MODEL", "text-embedding-3-small"),
            ),
            top_k=self._top_k,
            namespace=kb.namespace or "",
        )
        return engine, kb.namespace or None


async def create_agent(
    config: dict,
    *,
    system_prompt: str = "",
    max_steps: int = 10,
    register_mcp: bool = True,
    mcp_cfg: dict | None = None,
    db=None,
    memory_type: str = "short",
) -> DefaultAgent:
    """创建配置完整的 DefaultAgent（ReActExecutor + 工具）。

    Args:
        config: 模型配置 dict（即 config.yaml 的 models 段）
        system_prompt: 系统提示词
        max_steps: ReAct 循环最大步数
        register_mcp: 是否注册 News MCP Server 的工具
        mcp_cfg: ``config["mcp_server"]``，为 None 时不注册 MCP 工具
        db: 可选 PostgreSQL 实例，传入时使用 ShortTermMemory 持久化对话
        memory_type: 记忆类型 "null"(无记忆)/"short"(ShortTermMemory)/"long"(LongTermMemory)，默认 "short"。无 db 时强制 "null"。

    Returns:
        已装配好 ReActExecutor 和 Registry 的 DefaultAgent

    用法::
        agent = await create_agent(config={
            "default": {"protocol": "openai", "model": "gpt-4o", "api_key": "..."},
        })
    """
    # 1. 创建内置工具注册表
    registry = setup_builtin_tools()

    # 2. 连接 MCP Server 并注册其工具（SSE 模式）
    if register_mcp and mcp_cfg:
        await _register_mcp_tools(registry, mcp_cfg)

    # 3. 记忆:按 memory_type 选择(无 db 时强制 NullMemory)
    from .memory import ShortTermMemory, NullMemory, LongTermMemory, PgMemoryStorage
    if memory_type == "null" or not db:
        memory = NullMemory()
    elif memory_type == "long":
        memory = LongTermMemory(db, PgMemoryStorage(db))
    else:
        memory = ShortTermMemory(db, window_size=20)

    # 4. 创建 ReActExecutor（构造时注入 brain/memory/tools）
    executor = ReActExecutor(
        brain=ModelHub(config), memory=memory,
        tools=registry, max_steps=max_steps,
    )

    # 5. 创建 Agent
    agent = DefaultAgent(
        config=config,
        executor=executor,
        memory=memory,
        tools=registry,
        system_prompt=system_prompt,
    )
    return agent


async def start_mcp_server(mcp_cfg: dict) -> subprocess.Popen:
    """启动 MCP Server 作为 SSE 服务，返回子进程句柄。

    Args:
        mcp_cfg: ``config["mcp_server"]``，含 host/port/transport
    """
    proc = await asyncio.create_subprocess_exec(
        "python", "-m", "agent.mcp.news_server",
        "--transport", mcp_cfg["transport"],
        "--port", str(mcp_cfg["port"]),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    await _wait_for_server(mcp_cfg["port"], timeout=10)
    logger.info(
        "MCP Server started (%s mode, http://%s:%s).",
        mcp_cfg["transport"], mcp_cfg["host"], mcp_cfg["port"],
    )
    return proc


async def _wait_for_server(port: int, timeout: float = 10.0) -> None:
    """轮询直到 MCP Server 端口可接受 TCP 连接。"""
    import asyncio

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection("localhost", port),
                timeout=2,
            )
            writer.close()
            await writer.wait_closed()
            return
        except (OSError, asyncio.TimeoutError):
            await asyncio.sleep(0.3)
    raise RuntimeError(f"MCP Server 端口 {port} 未在 {timeout}s 内就绪")


async def _create_agent_connector(mcp_cfg: dict):
    """创建到 SSE MCP Server 的客户端连接。

    Args:
        mcp_cfg: ``config["mcp_server"]``
    """
    from .mcp import MCPClient

    base_url = f"http://{mcp_cfg['host']}:{mcp_cfg['port']}"
    session = await MCPClient.connect_sse(base_url)
    return session


async def _register_mcp_tools(registry: Registry, mcp_cfg: dict) -> None:
    """连接到 MCP Server（SSE）并注册其工具到 Registry。

    每个 agent 持有独立的 SSE 连接，FastMCP 服务端 async 处理并发请求。
    """
    for attempt in range(1, 4):
        try:
            client = await _create_agent_connector(mcp_cfg)
            registry.add_mcp(client, level_map=LEVEL_MAP)
            tool_names = [t["name"] for t in client.get_tools()]
            logger.info(
                "Registered %d MCP tools (attempt %d): %s",
                len(tool_names), attempt, tool_names,
            )
            return
        except Exception as exc:
            if attempt < 3:
                wait = 1.0 * (2 ** (attempt - 1))
                logger.warning(
                    "MCP register failed (attempt %d/3): %s. Retrying in %.1fs...",
                    attempt, exc, wait,
                )
                await asyncio.sleep(wait)
            else:
                logger.error(
                    "MCP register failed after 3 attempts: %s", exc,
                )

