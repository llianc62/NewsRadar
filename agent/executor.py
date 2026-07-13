"""Executor hierarchy — DirectExecutor and ReActExecutor."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any

from .hub import ModelHub
from .memory import MemoryModule, NullMemory
from .models import Context


class PolicyDecision(Enum):
    """工具策略决策结果。"""
    ALLOW = auto()
    REJECT = auto()
    APPROVAL_REQUIRED = auto()


@dataclass
class PolicyResult:
    """工具策略检查结果。"""
    decision: PolicyDecision
    reason: str = ""
    message: str = ""


class Executor(ABC):
    """执行器基类——定义 Agent 的执行策略。"""

    def __init__(self, approval_callback=None):
        self._approval_callback = approval_callback
        # approval_callback signature:
        #   async (tool_def, args) -> {"approved": bool, "reason": str}

    @abstractmethod
    async def run(
        self,
        ctx: Context,
        brain: ModelHub,
        memory: MemoryModule | None = None,
        **kwargs: Any,
    ) -> str:
        """执行一次完整的推理循环。"""
        ...

    @abstractmethod
    async def run_stream(
        self,
        ctx: Context,
        brain: ModelHub,
        memory: MemoryModule | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        """流式推理循环。"""
        ...

    async def _exec_tool_with_policy(
        self, tools, name: str, args: dict, running_mode: str = "normal"
    ) -> str:
        """执行工具调用，带策略拦截。"""
        from .tools import Registry

        if not isinstance(tools, Registry):
            return await tools.execute(name, args)

        tool = tools.get_tool(name)
        if not tool:
            return f"[Policy] 工具 '{name}' 不存在"

        result = self._check_policy(tool, running_mode)
        if result.decision == PolicyDecision.REJECT:
            return f"[Policy] {result.message}"

        if result.decision == PolicyDecision.APPROVAL_REQUIRED:
            if self._approval_callback:
                decision = await self._approval_callback(tool.get_def(), args)
                if decision.get("approved"):
                    return await tools.execute(name, args)
                return (
                    f"[Policy] 工具调用被拒绝: "
                    f"{decision.get('reason', '需要人工审批')}"
                )
            return (
                f"[Policy] 工具 '{name}' 需要审批，"
                f"但未配置审批回调"
            )

        return await tools.execute(name, args)

    @staticmethod
    def _check_policy(tool, running_mode: str = "normal") -> PolicyResult:
        """根据工具等级和运行模式做策略判断。"""
        threshold = {"strict": 2, "normal": 3, "loose": 4}.get(running_mode, 3)
        level = tool.level

        if level >= threshold:
            return PolicyResult(
                PolicyDecision.APPROVAL_REQUIRED,
                f"level {level} >= threshold {threshold}",
            )
        return PolicyResult(PolicyDecision.ALLOW)


class DirectExecutor(Executor):
    """简单直调执行器——没有 ReAct 循环，没有工具调用。

    适用于：简单问答、分类、不需要工具的纯文本场景。
    Phase 2: 已接入 MemoryModule hook。
    Phase 3: 忽略 tools 参数，直接返回 LLM 文本响应。
    """

    def __init__(self, approval_callback=None):
        super().__init__(approval_callback=approval_callback)

    async def run(
        self,
        ctx: Context,
        brain: ModelHub,
        memory: MemoryModule | None = None,
        **kwargs: Any,
    ) -> str:
        _memory = memory or NullMemory()
        _ = kwargs  # 预留：knowledge, tools 参数
        await _memory.on_before_execute(ctx)

        client = brain.get(ctx.model_name)
        model_version = brain.get_model_version(ctx.model_name)
        messages = self._build_messages(ctx)
        result = await client.chat(model=model_version, messages=messages)

        ctx.assistant_output = result.content
        ctx.model_used = model_version
        if result.tool_calls:
            ctx.tool_calls = result.tool_calls
        await _memory.on_after_execute(ctx)
        return result.content

    async def run_stream(
        self,
        ctx: Context,
        brain: ModelHub,
        memory: MemoryModule | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        _memory = memory or NullMemory()
        _ = kwargs  # 预留：knowledge, tools 参数
        await _memory.on_before_execute(ctx)

        client = brain.get(ctx.model_name)
        model_version = brain.get_model_version(ctx.model_name)
        messages = self._build_messages(ctx)

        chunks: list[str] = []
        async for token in client.chat_stream(model=model_version, messages=messages):
            chunks.append(token)
            yield token

        ctx.assistant_output = "".join(chunks)
        ctx.model_used = model_version
        await _memory.on_after_execute(ctx)

    @staticmethod
    def _build_messages(ctx: Context) -> list[dict]:
        messages: list[dict] = []
        if ctx.system_prompt:
            messages.append({"role": "system", "content": ctx.system_prompt})
        if ctx.memory_context:
            memory_text = (
                ctx.memory_context
                if isinstance(ctx.memory_context, str)
                else _format_memory_for_prompt(ctx.memory_context)
            )
            messages.append({"role": "system", "content": f"## 对话历史\n{memory_text}"})
        messages.append({"role": "user", "content": ctx.user_input})
        return messages


class ReActExecutor(Executor):
    """ReAct 风格的推理循环——Agent 自主决定调工具还是回答。

    流程:
    1. memory.on_before   → 检索相关记忆，注入 Context
    2. tools.get_schemas  → 获取所有工具 schema
    3. 构建 messages（system + memory_context + user + history）
    4. 调 LLM（带 tools 参数）
    5. 解析 LLM 响应:
       - 如果是 tool_call → 执行工具 → 记录到 Context → 回到 3
       - 如果是 text       → 存记忆 → 返回
    6. 超过 max_steps → 终止并返回当前输出

    Phase 3 实现。
    """

    def __init__(self, max_steps: int = 10, max_retries: int = 3, approval_callback=None):
        if max_steps < 1:
            raise ValueError("max_steps must be >= 1")
        super().__init__(approval_callback=approval_callback)
        self.max_steps = max_steps
        self.max_retries = max_retries

    async def run(
        self,
        ctx: Context,
        brain: ModelHub,
        memory: MemoryModule | None = None,
        **kwargs: Any,
    ) -> str:
        from .tools import Registry

        _memory = memory or NullMemory()
        tools: Registry | None = kwargs.get("tools")
        await _memory.on_before_execute(ctx)

        tool_schemas = tools.get_schemas() if tools else None
        model_name = ctx.model_name or "default"
        client = brain.get(model_name)
        model_version = brain.get_model_version(model_name)

        for step in range(self.max_steps):
            messages = self._build_messages(ctx)
            result = await client.chat(
                model=model_version,
                messages=messages,
                tools=tool_schemas,
            )

            if not result.tool_calls:
                # LLM 直接返回文本 → 完成
                ctx.assistant_output = result.content
                ctx.model_used = model_name
                ctx.step_count = step + 1
                await _memory.on_after_execute(ctx)
                return result.content

            # 执行工具调用
            for tc in result.tool_calls:
                try:
                    fn_info = tc.get("function", tc)
                    fn_name = fn_info.get("name", "")
                    raw_args = fn_info.get("arguments", "{}")
                    if isinstance(raw_args, str):
                        fn_args = json.loads(raw_args)
                    else:
                        fn_args = raw_args

                    tool_result = await self._exec_tool_with_policy(
                        tools, fn_name, fn_args, running_mode=ctx.running_mode
                    )
                    ctx.tool_calls.append(tc)
                    ctx.tool_results.append(tool_result)

                    # 注入工具结果作为新一轮消息
                    ctx.history.append({
                        "role": "user",
                        "content": f"工具 {fn_name} 返回: {tool_result}",
                    })
                except Exception as e:
                    ctx.history.append({
                        "role": "user",
                        "content": f"工具调用失败: {e}",
                    })

        # 超过 max_steps：返回最后的内容
        ctx.assistant_output = ctx.history[-1]["content"] if ctx.history else "已达最大步数"
        ctx.step_count = self.max_steps
        return ctx.assistant_output

    async def run_stream(
        self,
        ctx: Context,
        brain: ModelHub,
        memory: MemoryModule | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        """流式版本：非流式调工具循环，流式输出最终回答。"""
        result = await self.run(ctx, brain, memory, **kwargs)
        yield result

    @staticmethod
    def _build_messages(ctx: Context) -> list[dict]:
        messages: list[dict] = []
        if ctx.system_prompt:
            messages.append({"role": "system", "content": ctx.system_prompt})
        if ctx.memory_context:
            memory_text = (
                ctx.memory_context
                if isinstance(ctx.memory_context, str)
                else _format_memory_for_prompt(ctx.memory_context)
            )
            messages.append({"role": "system", "content": f"## 相关记忆\n{memory_text}"})
        messages.append({"role": "user", "content": ctx.user_input})
        messages.extend(ctx.history)
        return messages


def _format_memory_for_prompt(memories: list[dict]) -> str:
    """将记忆消息列表格式化为 prompt 文本。"""
    lines: list[str] = []
    for msg in memories:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        label = "User" if role == "user" else "Assistant"
        lines.append(f"{label}: {content}")
    return "\n".join(lines[-10:])  # 最多最近 5 轮
