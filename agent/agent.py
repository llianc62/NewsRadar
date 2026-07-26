"""DefaultAgent — 模块化 Agent 基座。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from .executor import DirectExecutor, Executor
from .model_hub import ModelHub
from .memory import MemoryModule, NullMemory
from .data import AgentResult, Context

if TYPE_CHECKING:
    from .knowledge import KnowledgeEngine
    from .tools import Registry


class DefaultAgent:
    """模块化 Agent 基座。

    config 接收总配置文件（config.yaml）中关于模型配置的子项 dict
    （即 config["models"]），直接传递给 ModelHub。
    config 是必传参数。

    使用方式:
        # 最小构造——只传必填参数
        agent = DefaultAgent(
            config={
                "default": {"protocol": "openai", "model": "gpt-4o-mini", "api_key": "..."},
            },
        )

        # 带工具调用
        agent = DefaultAgent(
            config={...},
            executor=ReActExecutor(),
            tools=tool_registry,
        )
    """

    def __init__(
        self,
        config: dict,
        executor: Executor | None = None,
        memory: MemoryModule | None = None,
        system_prompt: str = "",
        tools: Registry | None = None,  # Phase 3
        running_mode: str = "normal",
        approval_callback=None,
        knowledge: "KnowledgeEngine | None" = None,
        kb_namespace: str = "",
    ):
        if running_mode not in ("strict", "normal", "loose"):
            raise ValueError(f"Invalid running_mode: {running_mode}")
        self._running_mode = running_mode
        self._approval_callback = approval_callback
        self.brain = ModelHub(config=config)
        self.executor = executor or DirectExecutor(approval_callback=approval_callback)
        self.memory = memory or NullMemory()
        self.tools = tools
        self.system_prompt = system_prompt
        self._knowledge = knowledge
        self._kb_namespace = kb_namespace

    @property
    def running_mode(self) -> str:
        return self._running_mode

    @running_mode.setter
    def running_mode(self, value: str):
        if value not in ("strict", "normal", "loose"):
            raise ValueError(f"Invalid running_mode: {value}")
        self._running_mode = value

    @property
    def kb_namespace(self) -> str:
        """知识库命名空间（如 ``"investing/buffett"``）。"""
        return self._kb_namespace

    async def _make_ctx(
        self, user_input: str, session_id: str, model_name: str
    ) -> Context:
        """构建本次调用的 Context。

        注入知识库检索结果（``ctx.knowledge_context``）与记忆上下文
        （``ctx.memory_context``）。``chat``/``chat_stream`` 共用此入口，
        确保任意子类的注入对两个调用路径都生效。
        """
        ctx = Context(
            user_input=user_input,
            session_id=session_id,
            system_prompt=self.system_prompt,
            model_name=model_name or "default",
            running_mode=self._running_mode,
        )

        # 知识库检索（sync -> to_thread 避免阻塞事件循环）
        if self._knowledge and self._kb_namespace:
            text = await asyncio.to_thread(
                self._knowledge.retrieve_render,
                user_input,
                self._kb_namespace,
            )
            if text:
                ctx.knowledge_context = text

        return ctx

    # ── public API ──────────────────────────────────────────────

    async def chat(
        self,
        user_input: str,
        session_id: str = "",
        model_name: str = "",
    ) -> AgentResult:
        """执行一次完整的 Agent 调用（非流式）。"""
        ctx = await self._make_ctx(user_input, session_id, model_name)

        result_text = await self.executor.run(
            ctx=ctx,
            brain=self.brain,
            memory=self.memory,
            tools=self.tools,
            history_messages=getattr(self, "_history_messages", None),
        )

        return AgentResult(
            content=result_text,
            model_used=ctx.model_used,
            total_tokens=ctx.total_tokens,
            tool_calls=ctx.tool_calls,
            tool_results=ctx.tool_results,
            step_count=ctx.step_count,
        )

    async def chat_stream(
        self,
        user_input: str,
        session_id: str = "",
        model_name: str = "",
    ) -> AsyncIterator[str]:
        """流式版本——逐 token 返回 LLM 输出。"""
        ctx = await self._make_ctx(user_input, session_id, model_name)

        async for token in self.executor.run_stream(
            ctx=ctx,
            brain=self.brain,
            memory=self.memory,
            tools=self.tools,
            history_messages=getattr(self, "_history_messages", None),
        ):
            yield token
