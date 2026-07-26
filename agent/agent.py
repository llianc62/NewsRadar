"""DefaultAgent - 模块化 Agent 基座。

轻量化设计：DefaultAgent 只持有配置和 executor，不参与 LLM/工具/记忆的
运行时调度。所有运行时组件在构造时注入 executor（或由 DefaultAgent 自动
注入到默认 ReActExecutor），chat/chat_stream 只构建 Context 并调
``executor.run(ctx)`` / ``executor.run_stream(ctx)``。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from .executor import Executor, ReActExecutor
from .model_hub import ModelHub
from .memory import MemoryModule, NullMemory
from .data import AgentResult, Context

if TYPE_CHECKING:
    from .knowledge import KnowledgeEngine
    from .tools import Registry


def _last_model_used(ctx: Context) -> str:
    """取 ``ctx.messages`` 中最后一条 assistant 消息的 ``model_used``。"""
    for msg in reversed(ctx.messages):
        if msg.role == "assistant":
            return msg.model_used or ""
    return ""


class DefaultAgent:
    """模块化 Agent 基座。

    config 接收总配置文件（config.yaml）中关于模型配置的子项 dict
    （即 ``config["models"]``），直接传递给 ModelHub。
    config 是必传参数。

    使用方式::

        # 最小构造 -- 只传必填参数（自动创建 ReActExecutor + brain）
        agent = DefaultAgent(
            config={
                "default": {"protocol": "openai", "model": "gpt-4o-mini", "api_key": "..."},
            },
        )

        # 传入已装配的 executor（推荐，组件由 factory 注入）
        agent = DefaultAgent(
            config={...},
            executor=ReActExecutor(brain=hub, memory=mem, tools=reg),
            memory=mem,
            tools=reg,
        )
    """

    def __init__(
        self,
        config: dict,
        executor: Executor | None = None,
        memory: MemoryModule | None = None,
        knowledge: "KnowledgeEngine | None" = None,
        tools: "Registry | None" = None,
        system_prompt: str = "",
        running_mode: str = "normal",
        approval_callback=None,
    ):
        if running_mode not in ("strict", "normal", "loose"):
            raise ValueError(f"Invalid running_mode: {running_mode}")
        self._running_mode = running_mode
        self._approval_callback = approval_callback
        self.brain = ModelHub(config=config)
        self.memory = memory or NullMemory()
        self._knowledge = knowledge
        self.tools = tools
        self.system_prompt = system_prompt
        # executor 构造时注入组件（或由调用方传入已装配的 executor）
        self.executor = executor or ReActExecutor(
            brain=self.brain,
            memory=self.memory,
            knowledge=knowledge,
            tools=tools,
            approval_callback=approval_callback,
            hooks=None,
        )

    @property
    def running_mode(self) -> str:
        return self._running_mode

    @running_mode.setter
    def running_mode(self, value: str):
        if value not in ("strict", "normal", "loose"):
            raise ValueError(f"Invalid running_mode: {value}")
        self._running_mode = value

    async def _make_ctx(
        self, user_input: str, session_id: str, model_name: str
    ) -> Context:
        """构建本次调用的 Context（仅基本字段，知识/记忆由 executor 注入）。"""
        return Context(
            user_input=user_input,
            session_id=session_id,
            system_prompt=self.system_prompt,
            model_name=model_name or "default",
            running_mode=self._running_mode,
        )

    # ── public API ──────────────────────────────────────────────

    async def chat(
        self,
        user_input: str,
        session_id: str = "",
        model_name: str = "",
    ) -> AgentResult:
        """执行一次完整的 Agent 调用（非流式）。"""
        ctx = await self._make_ctx(user_input, session_id, model_name)
        output = await self.executor.run(ctx)
        return AgentResult(
            content=output,
            model_used=ctx.final_output and _last_model_used(ctx),
            total_tokens=ctx.total_input_tokens + ctx.total_output_tokens,
            step_count=ctx.step_count,
        )

    async def chat_stream(
        self,
        user_input: str,
        session_id: str = "",
        model_name: str = "",
    ) -> AsyncIterator[str]:
        """流式版本 -- 逐 token 返回 LLM 输出。"""
        ctx = await self._make_ctx(user_input, session_id, model_name)
        async for token in self.executor.run_stream(ctx):
            yield token
