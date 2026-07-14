"""AgentFactory — 统一构建带工具的 DefaultAgent。

简化 Agent 的创建流程，自动装配 ReActExecutor、内置工具、
MCP 工具等依赖。
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from .agent import DefaultAgent
from .executor import ReActExecutor
from .memory import LongTermMemory, PgMemoryStorage
from .models import AgentDefinition
from .tools import Registry
from .tools.tools import setup_builtin_tools

if TYPE_CHECKING:
    from .knowledge import KnowledgeEngine


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
        top_k: int = 5,
    ):
        self._models_config = models_config
        self._db = db
        self._registry = registry
        self._top_k = top_k

    def build(self, defn: AgentDefinition) -> DefaultAgent:
        """从角色定义构建可运行的 :class:`DefaultAgent`。

        自动解析工具（从全局 Registry 按名查找）、知识库（从 DB
        查询 ``AgentKnowledge`` -> 构建 ``KnowledgeEngine``），
        并装配 ReActExecutor + LongTermMemory。

        Args:
            defn: 角色定义（含 system_prompt、tools、knowledge_id 等）。

        Returns:
            已装配的 :class:`DefaultAgent` 实例。
        """
        tools = self._resolve_tools(defn.tools)
        knowledge = self._resolve_knowledge(defn.knowledge_id)
        executor = ReActExecutor(max_steps=10)
        memory = LongTermMemory(PgMemoryStorage(self._db))

        agent = DefaultAgent(
            config=self._models_config,
            executor=executor,
            memory=memory,
            tools=tools,
            system_prompt=defn.system_prompt,
        )
        agent._knowledge = knowledge
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

    def _resolve_knowledge(self, knowledge_id: str | None) -> KnowledgeEngine | None:
        """从 DB 查询知识库定义，构建 :class:`KnowledgeEngine`。

        ``knowledge_id`` 为 ``None`` 或 DB 中未找到时返回 ``None``。
        Embedding 配置从环境变量读取：
        ``KNOWLEDGE_EMBEDDING_API_KEY`` / ``KNOWLEDGE_EMBEDDING_BASE_URL`` /
        ``KNOWLEDGE_EMBEDDING_MODEL``。
        """
        if not knowledge_id:
            return None
        kb = self._db.get_agent_knowledge(knowledge_id)
        if not kb:
            return None
        from agent.knowledge import EmbeddingClient, KnowledgeEngine, PgVectorKnowledgeStore

        return KnowledgeEngine(
            store=PgVectorKnowledgeStore(self._db),
            embedding=EmbeddingClient(
                api_key=os.environ.get("KNOWLEDGE_EMBEDDING_API_KEY", ""),
                base_url=os.environ.get("KNOWLEDGE_EMBEDDING_BASE_URL", ""),
                model=os.environ.get("KNOWLEDGE_EMBEDDING_MODEL", "text-embedding-3-small"),
            ),
            top_k=self._top_k,
        )


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


async def create_persona(
    name: str,
    config: dict,
    *,
    knowledge=None,
    analyzer=None,
    max_steps: int = 10,
    register_mcp: bool = True,
    executor=None,
    tools=None,
    memory=None,
    running_mode: str = "normal",
):
    """创建单个角色 agent（默认装配 ReActExecutor + 内置工具 + MCP）。

    仿 ``create_agent`` 的装配流程，但构造 ``PersonaAgent`` 子类（按 ``name``
    从 ``PERSONA_REGISTRY`` 查规格）。角色人格 prompt 来自子类的
    ``get_system_prompt()``，知识库按规格的 ``kb_namespace`` 检索。

    Args:
        name: 角色名（``PERSONA_REGISTRY`` 的 key，如 ``"buffett"``）。
        config: 模型配置 dict（``config["models"]``）。
        knowledge: 可选 ``KnowledgeEngine``，按角色 kb_namespace 检索知识。
        analyzer: 可选 ``JiebaAnalyzer``（仅 ``requires_analyzer`` 的角色需要）。
        register_mcp: 是否自动注册 News MCP Server 工具。
        executor/tools/memory: 显式传入则跳过默认装配（测试用）。

    用法::

        agent = await create_persona("buffett", config["models"],
                                     knowledge=knowledge_engine)
    """
    from .persona import PERSONA_REGISTRY

    spec = PERSONA_REGISTRY.get(name)
    if not spec:
        raise ValueError(
            f"未知角色: {name!r}（可用: {sorted(PERSONA_REGISTRY)}）"
        )

    if tools is None and not spec.cls.prefer_direct_executor:
        # 纯叙事角色（如主编）不需要工具，跳过 MCP 子进程开销
        tools = setup_builtin_tools()
        if register_mcp:
            await _register_mcp_tools(tools)
    if executor is None:
        if spec.cls.prefer_direct_executor:
            from .executor import DirectExecutor
            executor = DirectExecutor()
        else:
            executor = ReActExecutor(max_steps=max_steps)

    kwargs: dict = {
        "knowledge": knowledge,
        "executor": executor,
        "tools": tools,
        "memory": memory,
        "running_mode": running_mode,
    }
    if spec.cls.requires_analyzer:
        kwargs["analyzer"] = analyzer
    return spec.cls(config, **kwargs)


async def create_persona_manager(
    config: dict, db=None, *, register_mcp: bool = True
):
    """从完整配置构建 :class:`PersonaManager`（角色 agent 懒构建缓存）。

    自动装配两份共享依赖，失败均降级为 ``None``（角色仍可对话，仅缺知识/分析）：

    - **analyzer**：``news.analyzer.create_analyzer(config, db)``，供
      ``requires_analyzer`` 的角色（如 sentiment）做硬编码情感分析。
    - **knowledge**：当 ``knowledge.enabled`` 且 ``embedding_api_key`` 就绪、
      且传入 ``db`` 时，构建 ``KnowledgeEngine``（pgvector 检索）。

    Args:
        config: 完整配置 dict（含 ``models`` / ``knowledge`` / ``analyzer`` 段）。
        db: 可选 ``PostgreSQL`` 实例（analyzer 与 knowledge store 共用）。
        register_mcp: 角色首用时是否注册 News MCP Server 工具。

    Returns:
        :class:`PersonaManager`，已注入 knowledge 与 analyzer。
    """
    from .persona.manager import PersonaManager

    # analyzer（JiebaAnalyzer）—— 失败不致命
    analyzer = None
    try:
        from news.analyzer import create_analyzer

        analyzer = create_analyzer(config, db=db)
    except Exception:
        analyzer = None

    # knowledge engine —— 可选，按需构建
    knowledge = None
    kcfg = config.get("knowledge", {})
    if kcfg.get("enabled") and kcfg.get("embedding_api_key") and db is not None:
        try:
            from agent.knowledge import (
                EmbeddingClient,
                KnowledgeEngine,
                PgVectorKnowledgeStore,
            )

            embedding = EmbeddingClient(
                api_key=kcfg["embedding_api_key"],
                base_url=kcfg.get("embedding_base_url", ""),
                model=kcfg.get("embedding_model", "text-embedding-3-small"),
            )
            knowledge = KnowledgeEngine(
                store=PgVectorKnowledgeStore(db),
                embedding=embedding,
                top_k=kcfg.get("top_k", 5),
            )
        except Exception:
            knowledge = None

    return PersonaManager(
        config["models"],
        knowledge=knowledge,
        analyzer=analyzer,
        register_mcp=register_mcp,
    )


async def create_persona_orchestrator(
    config: dict, db=None, *, register_mcp: bool = True, max_concurrent: int = 4
):
    """构建 :class:`PersonaOrchestrator`（多角色并行编排 + 主编聚合）。

    复用 :func:`create_persona_manager` 装配 knowledge/analyzer，再包一层
    :class:`PersonaOrchestrator`。主编（``editor``）必须已注册于
    ``PERSONA_REGISTRY``。

    Args:
        config: 完整配置 dict。
        db: 可选 ``PostgreSQL`` 实例。
        register_mcp: 角色首用时是否注册 News MCP 工具。
        max_concurrent: Phase 1 并行角色数上限。

    Returns:
        :class:`PersonaOrchestrator`。
    """
    from .persona import PersonaOrchestrator

    manager = await create_persona_manager(config, db=db, register_mcp=register_mcp)
    return PersonaOrchestrator(manager, max_concurrent=max_concurrent)
