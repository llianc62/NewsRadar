"""角色 agent 懒构建管理器。

按名取 :class:`PersonaAgent`，首用时构建（含 MCP 连接）并缓存，避免 daemon
启动期为每个角色各开一条 MCP 子进程连接。单角色取用归本管理器；多角色并发
编排由 Phase C 的 :class:`PersonaOrchestrator` 接管。

设计要点：
- **懒构建**：``get()`` 首次调用才 ``create_persona``，命中缓存后零开销。
- **并发安全**：``asyncio.Lock`` + double-check，并发首取同一角色只构建一次。
- **未知角色 fail-fast**：``get()`` 对未注册名抛 ``ValueError``，不静默降级。
"""

from __future__ import annotations

import asyncio

from .registry import PERSONA_REGISTRY, PersonaSpec, list_personas


class PersonaManager:
    """角色 agent 的懒构建缓存。

    Args:
        models_config: 模型配置 dict（即 ``config["models"]``）。
        knowledge: 可选 ``KnowledgeEngine``，按角色 ``kb_namespace`` 检索知识。
        analyzer: 可选 ``JiebaAnalyzer``，仅 ``requires_analyzer`` 的角色需要。
        register_mcp: 角色首用时是否注册 News MCP Server 工具。
        max_steps: ReAct 循环最大步数。
        mcp_cfg: ``config["mcp_server"]``，透传给每个角色 agent 用于 SSE 连接。
    """

    def __init__(
        self,
        models_config: dict,
        *,
        knowledge=None,
        analyzer=None,
        register_mcp: bool = True,
        max_steps: int = 10,
        base_prompt: str = "",
        mcp_cfg: dict | None = None,
    ):
        self._models = models_config
        self._knowledge = knowledge
        self._analyzer = analyzer
        self._register_mcp = register_mcp
        self._max_steps = max_steps
        self._base_prompt = base_prompt
        self._mcp_cfg = mcp_cfg
        self._cache: dict = {}
        self._lock = asyncio.Lock()
        # 运行时配置：每次 get() 应用到返回的 persona（含缓存命中）
        self._running_mode = "strict"
        self._approval_callback = None

    def set_running_config(self, running_mode: str, approval_callback=None) -> None:
        """设置运行模式与工具审批回调，后续每次 ``get()`` 都会应用到角色。

        供 WebSocket 处理器在每次对话前调用，确保编排器内部 ``get()`` 取到的
        角色（主编 + 各 fan-out 角色）也带正确的审批通道。
        """
        self._running_mode = running_mode
        self._approval_callback = approval_callback

    def _apply_running_config(self, persona) -> None:
        persona.running_mode = self._running_mode
        if self._approval_callback is not None:
            persona.executor._approval_callback = self._approval_callback

    def available(self) -> list[PersonaSpec]:
        """返回全部已注册角色规格（按 ``order`` 排序，供前端面板渲染）。"""
        return list_personas()

    def has(self, name: str) -> bool:
        """角色名是否已注册。"""
        return name in PERSONA_REGISTRY

    async def get(self, name: str):
        """按名取角色 agent（首用构建，后续命中缓存）。

        每次返回前应用当前运行模式与审批回调。未知角色抛 ``ValueError``。
        """
        cached = self._cache.get(name)
        if cached is not None:
            self._apply_running_config(cached)
            return cached
        if not self.has(name):
            raise ValueError(
                f"未知角色: {name!r}（可用: {sorted(PERSONA_REGISTRY)}）"
            )
        async with self._lock:
            # double-check：并发首取同一角色时只构建一次
            cached = self._cache.get(name)
            if cached is not None:
                self._apply_running_config(cached)
                return cached
            # 延迟导入，避免与 agent.factory 形成循环导入
            from agent.factory import create_persona

            persona = await create_persona(
                name,
                self._models,
                knowledge=self._knowledge,
                analyzer=self._analyzer,
                register_mcp=self._register_mcp,
                max_steps=self._max_steps,
                base_prompt=self._base_prompt,
                mcp_cfg=self._mcp_cfg,
            )
            self._cache[name] = persona
            self._apply_running_config(persona)
            return persona
