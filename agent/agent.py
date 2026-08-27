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
    （即 ``config["agent"]["models"]``），直接传递给 ModelHub。
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
        # executor 由调用方传入时要求已装配（brain/memory 已设）；
        # 未传则 DefaultAgent 构造 ReActExecutor 并注入 brain/memory/tools。
        self.executor = executor or ReActExecutor(
            brain=self.brain,
            memory=self.memory,
            knowledge=knowledge,
            tools=tools,
            approval_callback=approval_callback,
            hooks=None,
        )
        self._ctx: "Context | None" = None

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

    async def _get_or_create_ctx(
        self, user_input: str, session_id: str, model_name: str
    ) -> Context:
        """复用已有 ctx;首次创建 + memory.load 恢复历史。

        后续轮只更新 user_input/model_name,不再 load(history 在 ctx.messages 累积)。
        memory.load 失败降级(不阻断),与原 _prepare 行为一致。
        """
        if self._ctx is not None:
            ctx = self._ctx
            ctx.user_input = user_input
            ctx.model_name = model_name or "default"
            return ctx
        ctx = await self._make_ctx(user_input, session_id, model_name)
        try:
            await self.memory.load(ctx)
        except Exception as e:
            import logging
            logging.getLogger("agent").warning("memory.load failed, degrade: %s", e)
        self._ctx = ctx
        return ctx

    # ── public API ──────────────────────────────────────────────

    async def activate(self, session_id: str = "") -> None:
        """手动 reload:重置 ctx 并从 DB 加载全量历史。

        切换 agent 时由 session 调用(切回的缓存 agent ctx 可能陈旧,
        缺其他 agent 期间的轮次)。同 agent 连续对话无需调用(ctx 复用)。
        无 session_id 且无既有 ctx 时仅重置(下次 chat 懒加载);
        load 失败降级不抛,与 ``_get_or_create_ctx`` 行为一致。
        """
        sid = session_id or (self._ctx.session_id if self._ctx else "")
        if not sid:
            self._ctx = None
            return
        ctx = await self._make_ctx("", sid, "default")
        try:
            await self.memory.load(ctx)
        except Exception as e:
            import logging
            logging.getLogger("agent").warning("activate memory.load failed, degrade: %s", e)
        self._ctx = ctx

    async def freeze(self) -> None:
        """手动保存状态:memory.save 增量落库。

        水位机制保证幂等(无新消息即 no-op),通常如此--自动 ``_finalize``
        已每轮落库,freeze 兜底其偶发失败。ctx 未创建时跳过,失败降级不抛。
        """
        if self._ctx is None:
            return
        try:
            await self.memory.save(self._ctx)
        except Exception as e:
            import logging
            logging.getLogger("agent").warning("freeze memory.save failed, degrade: %s", e)

    def get_conversation(self) -> list[dict]:
        """有序对话记录投影:``[{role, content}]``。

        供显示侧就近读取(热源):滤掉 tool 消息与纯 tool_call(无 content)
        的 assistant,保序返回 user/assistant 对话。ctx 未创建返回空列表;
        无内容时回退 DB 由调用方兜底。
        """
        if self._ctx is None:
            return []
        out: list[dict] = []
        for m in self._ctx.messages:
            if m.role == "user":
                out.append({"role": "user", "content": m.content or ""})
            elif m.role == "assistant" and m.content:
                out.append({"role": "assistant", "content": m.content})
        return out

    async def chat(
        self,
        user_input: str,
        session_id: str = "",
        model_name: str = "",
    ) -> AgentResult:
        """执行一次完整的 Agent 调用（非流式）。"""
        ctx = await self._get_or_create_ctx(user_input, session_id, model_name)
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
        ctx = await self._get_or_create_ctx(user_input, session_id, model_name)
        async for token in self.executor.run_stream(ctx):
            yield token
