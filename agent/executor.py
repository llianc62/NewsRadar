"""Executor hierarchy — DirectExecutor and ReActExecutor.

All executors use the LLMClient protocol (``client.chat()`` / ``client.chat_stream()``)
which returns ``AIMessage`` directly — no ``ChatResult`` intermediate format.
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any

from .model_hub import ModelHub
from .memory import MemoryModule, NullMemory
from .data import Context, Message


def _tool_calls_to_api(tool_calls: list[dict]) -> list[dict]:
    """将存储格式 ``[{name, args, id}]`` 转为 API 格式 ``[{function: {name, arguments}}]``。

    ``AIMessage.tool_calls`` 使用简化格式，OpenAI API 要求旧格式。
    此函数在 ``_messages_to_dicts`` 中调用，确保消息历史中的 tool_calls 能被 API 识别。
    """
    return [
        {
            "id": tc.get("id", ""),
            "type": "function",
            "function": {
                "name": tc.get("name", ""),
                "arguments": json.dumps(tc.get("args", {}), ensure_ascii=False),
            },
        }
        for tc in tool_calls
    ]


def _ai_usage_to_dict(ai: Any) -> dict:
    """将 ``AIMessage.usage_metadata`` 转为兼容的 usage dict。"""
    um = getattr(ai, "usage_metadata", None)
    if um:
        return {
            "prompt_tokens": um.get("input_tokens", 0),
            "completion_tokens": um.get("output_tokens", 0),
            "total_tokens": um.get("total_tokens", 0),
        }
    return {}


def _ai_reasoning_content(ai: Any) -> str:
    """从 ``AIMessage.additional_kwargs`` 读取 ``reasoning_content``。"""
    return (getattr(ai, "additional_kwargs", None) or {}).get("reasoning_content", "") or ""


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


class ExecutorHook(ABC):
    """executor 生命周期 hook -- 开放各关键时机,子类按需 override,默认 no-op。"""

    async def before_chat(self, ctx: Context) -> None:
        """每次 LLM 调用前(可检查/改 messages、限流、监控)。"""

    async def after_chat(self, ctx: Context, ai: Any) -> None:
        """每次 LLM 返回后(AIMessage 已解析)。"""

    async def before_tool(self, name: str, args: dict, ctx: Context) -> dict | None:
        """工具执行前,可返回新 args 改写参数(返回 None 不改)。"""
        return None

    async def after_tool(self, tool_msg: Message, ctx: Context) -> None:
        """工具执行后(tool_msg 携带 tool_result 执行详情)。"""

    async def on_error(self, error: Exception, ctx: Context) -> None:
        """executor 捕获异常时。"""


class Executor(ABC):
    """执行器基类——定义 Agent 的执行策略。"""

    def __init__(self, approval_callback=None, hooks: list[ExecutorHook] | None = None):
        self._approval_callback = approval_callback
        # approval_callback signature:
        #   async (tool_def, args) -> {"approved": bool, "reason": str}
        self._hooks = hooks or []

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
        result = await client.chat(messages=messages)

        ctx.assistant_output = result.content or ""
        ctx.model_used = model_version
        if result.tool_calls:
            ctx.tool_calls = result.tool_calls
        await _memory.on_after_execute(ctx)
        return ctx.assistant_output

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
        async for chunk in client.chat_stream(messages=messages):
            token = chunk.content or ""
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
        _append_context_blocks(messages, ctx)
        messages.append({"role": "user", "content": ctx.user_input})
        return messages


class ReActExecutor(Executor):
    """ReAct 风格的推理循环——Agent 自主决定调工具还是回答。

    两级循环设计（参考 Pi agent loop）：
    - 内层循环：LLM → 工具调用 → LLM → ... → 文本回答
    - 工具执行三阶段：prepare → execute → finalize，每个工具独立隔离

    相较于旧版的核心改进：
    - ``_build_messages`` 只执行一次，不再每轮重复拼接 user_input
    - 检查 ``stop_reason``，截断时（length）不执行不完整的工具调用
    - 每个工具调用独立 try/except，错误不污染其他工具
    - 保留 tool schemas 直到 ``max_tool_rounds`` 轮后移除，确保 LLM 始终能正确输出
      结构化 JSON tool_calls，避免因 schemas 缺失导致 LLM 退化为 XML 文本格式
    - 真正的流式输出：最后一步使用 ``chat_stream`` 逐 token 输出
    """

    def __init__(
        self,
        max_steps: int = 10,
        max_tool_rounds: int = 5,
        max_retries: int = 3,
        approval_callback=None,
    ):
        if max_steps < 1:
            raise ValueError("max_steps must be >= 1")
        super().__init__(approval_callback=approval_callback)
        self.max_steps = max_steps
        self.max_tool_rounds = max_tool_rounds
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
        tool_schemas = tools.get_schemas() if tools else None
        model_name = ctx.model_name or "default"
        client = brain.get(model_name)
        model_version = brain.get_model_version(model_name)

        # 构建初始消息（只执行一次！）
        await _memory.on_before_execute(ctx)
        ctx.messages = self._build_initial_messages(ctx)

        # 注入历史 role 消息（由 WebSocket handler 从 DB 加载，作为实际对话轮次）
        history_messages = kwargs.get("history_messages")
        if history_messages:
            last = ctx.messages.pop()
            ctx.messages.extend(history_messages)
            ctx.messages.append(last)

        _result_text = ""
        tool_rounds = 0

        for step in range(self.max_steps):
            # 1. 调 LLM（返回 AIMessage，直接使用其字段）
            llm_messages = self._messages_to_dicts(ctx.messages)
            result = await client.chat(
                messages=llm_messages,
                tools=tool_schemas,
            )

            # 2. 从 AIMessage 提取字段
            tool_calls = list(result.tool_calls) if result.tool_calls else []
            finish_reason = (
                result.response_metadata.get("finish_reason", "")
                if result.response_metadata
                else ""
            )
            usage = _ai_usage_to_dict(result)
            reasoning = _ai_reasoning_content(result)

            # 3. 保存 assistant 消息（tool_calls 用简化格式存储）
            assistant_msg = Message(
                role="assistant",
                content=result.content or None,
                tool_calls=tool_calls or None,
                usage=usage or None,
                reasoning_content=reasoning or None,
            )
            ctx.messages.append(assistant_msg)

            # 更新 token 追踪
            if usage:
                ctx.total_input_tokens += usage.get("prompt_tokens", 0)
                ctx.total_output_tokens += usage.get("completion_tokens", 0)

            # 4. 判断 LLM 做了什么
            # 4a. 无工具调用 → 文本回答
            if not tool_calls:
                _result_text = result.content or ""
                ctx.assistant_output = _result_text
                ctx.model_used = model_version
                ctx.step_count = step + 1
                break

            # 4b. 有工具调用但被截断 → 标记为不完整，不执行
            if finish_reason in ("length", "max_tokens"):
                for tc in tool_calls:
                    ctx.messages.append(Message(
                        role="tool",
                        tool_call_id=tc.get("id", ""),
                        content="[Error] 工具调用被截断: 输出达到 token 限制，参数可能不完整",
                        name=tc.get("name", ""),
                    ))
                _result_text = result.content or ""
                ctx.assistant_output = _result_text
                ctx.model_used = model_version
                ctx.step_count = step + 1
                break

            # 4c. 正常工具调用 → 执行
            for tc in tool_calls:
                tool_msg = await self._execute_tool(tc, tools, ctx)
                ctx.messages.append(tool_msg)

            # 5. 达到 max_tool_rounds 后移除 schemas，强制 LLM 用文本回答
            tool_rounds += 1
            if tool_rounds >= self.max_tool_rounds:
                tool_schemas = None
        else:
            # 超过 max_steps
            _result_text = ctx.messages[-1].content or "" if ctx.messages else "已达最大步数"
            ctx.assistant_output = _result_text
            ctx.step_count = self.max_steps

        await _memory.on_after_execute(ctx)
        return _result_text

    async def run_stream(
        self,
        ctx: Context,
        brain: ModelHub,
        memory: MemoryModule | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        """流式版本：ReAct 循环在内部执行，最后一步用 chat_stream 逐 token 输出。

        工具调用步骤（非流式）：
        1. LLM 返回 tool_call → 执行工具
        2. 工具结果返回给 LLM
        3. 最终步骤用 chat_stream 流式输出文本
        """
        from .tools import Registry

        _memory = memory or NullMemory()
        tools: Registry | None = kwargs.get("tools")
        tool_schemas = tools.get_schemas() if tools else None
        model_name = ctx.model_name or "default"
        client = brain.get(model_name)
        model_version = brain.get_model_version(model_name)

        # 构建初始消息
        await _memory.on_before_execute(ctx)
        ctx.messages = self._build_initial_messages(ctx)

        # 注入历史 role 消息（由 WebSocket handler 从 DB 加载，作为实际对话轮次）
        history_messages = kwargs.get("history_messages")
        if history_messages:
            last = ctx.messages.pop()
            ctx.messages.extend(history_messages)
            ctx.messages.append(last)

        tool_rounds = 0

        for step in range(self.max_steps):
            llm_messages = self._messages_to_dicts(ctx.messages)
            result = await client.chat(
                messages=llm_messages,
                tools=tool_schemas,
            )

            # 从 AIMessage 提取字段
            tool_calls = list(result.tool_calls) if result.tool_calls else []
            finish_reason = (
                result.response_metadata.get("finish_reason", "")
                if result.response_metadata
                else ""
            )
            usage = _ai_usage_to_dict(result)
            reasoning = _ai_reasoning_content(result)

            # 保存 assistant 消息
            assistant_msg = Message(
                role="assistant",
                content=result.content or None,
                tool_calls=tool_calls or None,
                usage=usage or None,
                reasoning_content=reasoning or None,
            )
            ctx.messages.append(assistant_msg)
            if usage:
                ctx.total_input_tokens += usage.get("prompt_tokens", 0)
                ctx.total_output_tokens += usage.get("completion_tokens", 0)

            # 文本回答（无工具调用）
            if not tool_calls:
                ctx.assistant_output = result.content or ""
                ctx.model_used = model_version
                ctx.step_count = step + 1
                if result.content:
                    # 模拟流式输出
                    text = result.content
                    tokens = re.split(r'(?<=\s|，|。|！|？|；|、|）|」)', text)
                    for tok in tokens:
                        if tok:
                            yield tok + " "
                break

            # 截断 + 不完整工具调用
            if finish_reason in ("length", "max_tokens"):
                for tc in tool_calls:
                    ctx.messages.append(Message(
                        role="tool",
                        tool_call_id=tc.get("id", ""),
                        content="[Error] 工具调用被截断: 输出达到 token 限制，参数可能不完整",
                        name=tc.get("name", ""),
                    ))
                ctx.assistant_output = result.content or ""
                ctx.model_used = model_version
                ctx.step_count = step + 1
                if result.content:
                    yield result.content
                break

            # 正常工具调用
            for tc in tool_calls:
                tool_msg = await self._execute_tool(tc, tools, ctx)
                ctx.messages.append(tool_msg)

            # 达到 max_tool_rounds 后移除 schemas，强制 LLM 用文本回答
            tool_rounds += 1
            if tool_rounds >= self.max_tool_rounds:
                tool_schemas = None
        else:
            # 超过 max_steps
            fallback = ctx.messages[-1].content or "" if ctx.messages else "已达最大步数"
            ctx.assistant_output = fallback
            ctx.step_count = self.max_steps
            yield fallback

        await _memory.on_after_execute(ctx)

    # ── 工具执行三阶段状态机 ──────────────────────────────────

    async def _execute_tool(self, tc: dict, tools, ctx: Context) -> Message:
        """执行一个工具调用，返回 tool 结果消息。

        三阶段：
        1. prepare: 解析参数 + 校验
        2. execute: 调用工具函数
        3. finalize: 构造结果消息

        ``tc`` 为简化格式 ``{"name": ..., "args": ..., "id": ...}``
        （``AIMessage.tool_calls`` 原生格式）。
        """
        # Phase 1: 准备（简化格式，直接取 name/args）
        try:
            fn_name = tc.get("name", "")
            fn_args = tc.get("args", {})
            if isinstance(fn_args, str):
                fn_args = json.loads(fn_args)
        except (json.JSONDecodeError, TypeError, ValueError) as e:
            return Message(
                role="tool",
                tool_call_id=tc.get("id", ""),
                content=f"[Error] 工具参数解析失败: {e}",
                name=tc.get("name", ""),
            )

        # Phase 2: 执行
        try:
            tool_result_text = await self._exec_tool_with_policy(
                tools, fn_name, fn_args, running_mode=ctx.running_mode,
            )
        except Exception as e:
            return Message(
                role="tool",
                tool_call_id=tc.get("id", ""),
                content=f"[Error] 工具执行失败: {e}",
                name=fn_name,
            )

        # 记录到兼容字段
        ctx.tool_calls.append(tc)
        ctx.tool_results.append(tool_result_text)

        # Phase 3: 归一化后终结
        normalized = self._normalize_tool_result(tool_result_text)
        return Message(
            role="tool",
            tool_call_id=tc.get("id", ""),
            content=normalized,
            name=fn_name,
        )

    # ── 工具结果归一化 ──────────────────────────────────────────

    @staticmethod
    def _normalize_tool_result(raw: str) -> str:
        """归一化工具返回结果，确保对 LLM 友好。

        截断超长文本避免撑爆 context；对 JSON 数组/对象做展平，
        其他情况直接返回原文。
        """
        MAX_LEN = 4096
        if len(raw) > MAX_LEN:
            raw = raw[:MAX_LEN] + f"\n\n[截断: 原始结果过长 ({len(raw)} 字符)，仅保留前 {MAX_LEN} 字符]"

        try:
            obj = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            obj = None

        # JSON 数组 -> 逐条展平（MCP search_news 等返回 list[dict]）
        if isinstance(obj, list):
            parts = [f"共 {len(obj)} 条结果:\n"]
            for i, item in enumerate(obj, 1):
                if not isinstance(item, dict):
                    parts.append(f"{i}. {item}")
                    continue
                title = item.get("title") or item.get("name") or f"条目 {i}"
                source = item.get("source") or item.get("source_name") or ""
                desc = item.get("summary") or item.get("description") or ""
                line = f"{i}. {title}"
                if source:
                    line += f" ({source})"
                parts.append(line)
                if desc:
                    parts.append(f"   {desc[:300]}")
                for key in ("heat_score", "sentiment_score", "score", "price", "change"):
                    val = item.get(key)
                    if val is not None:
                        parts.append(f"   {key}: {val}")
                parts.append("")
            return "\n".join(parts)

        # 单个 JSON 对象 -> key: value 展平
        if isinstance(obj, dict):
            parts = []
            for k, v in obj.items():
                if isinstance(v, (list, dict)):
                    v_str = json.dumps(v, ensure_ascii=False)[:200]
                else:
                    v_str = str(v)
                parts.append(f"{k}: {v_str}")
            return "\n".join(parts)

        return raw

    # ── 消息构建 ──────────────────────────────────────────────

    @staticmethod
    def _build_initial_messages(ctx: Context) -> list[Message]:
        """构建初始消息列表（只执行一次，后续通过 append 追加）。"""
        messages: list[Message] = []
        if ctx.system_prompt:
            messages.append(Message(role="system", content=ctx.system_prompt))
        if ctx.knowledge_context:
            messages.append(Message(role="system", content=f"## 知识库\n{ctx.knowledge_context}"))
        if ctx.memory_context:
            memory_text = (
                ctx.memory_context
                if isinstance(ctx.memory_context, str)
                else _format_memory_for_prompt(ctx.memory_context)
            )
            messages.append(Message(role="system", content=f"## 相关记忆\n{memory_text}"))
        messages.append(Message(role="user", content=ctx.user_input))
        return messages

    @staticmethod
    def _messages_to_dicts(messages: list[Message]) -> list[dict]:
        """将 Message 列表转为 LLM API 所需的 dict 列表。

        转换时处理 tool_calls 格式：存储格式 ``[{name, args, id}]`` → API 格式 ``[{function: {name, arguments}}]``。
        """
        result: list[dict] = []
        for msg in messages:
            d: dict = {"role": msg.role}
            if msg.role == "assistant" and msg.tool_calls:
                # DeepSeek 思考模式要求 tool call 消息的 content 不能为 None
                # （ OpenAI / Anthropic 均兼容空字符串，不影响 ）
                d["content"] = msg.content or ""
                d["tool_calls"] = _tool_calls_to_api(msg.tool_calls)
                # DeepSeek 思考模式：reasoning_content 必须回传给 API
                if msg.reasoning_content:
                    d["reasoning_content"] = msg.reasoning_content
            elif msg.role == "tool":
                d["tool_call_id"] = msg.tool_call_id or ""
                d["content"] = msg.content or ""
            else:
                d["content"] = msg.content or ""
            result.append(d)
        return result


def _format_memory_for_prompt(memories: list[dict]) -> str:
    """将记忆消息列表格式化为 prompt 文本。"""
    lines: list[str] = []
    for msg in memories:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        label = "User" if role == "user" else "Assistant"
        lines.append(f"{label}: {content}")
    return "\n".join(lines[-10:])  # 最多最近 5 轮


def _append_context_blocks(messages: list[dict], ctx: Context) -> None:
    """注入知识库与专业分析上下文块（紧跟记忆块之后、user 之前）。

    - ``ctx.knowledge_context`` -> ``## 知识库``（检索到的文档片段）
    - ``ctx.analysis_context``  -> ``## 专业分析``（硬编码逻辑产出的结构化事实）

    两个 ``_build_messages`` 共用此 helper，避免注入逻辑漂移。
    """
    if ctx.knowledge_context:
        text = (
            ctx.knowledge_context
            if isinstance(ctx.knowledge_context, str)
            else str(ctx.knowledge_context)
        )
        messages.append({"role": "system", "content": f"## 知识库\n{text}"})
    analysis_context = getattr(ctx, "analysis_context", None)
    if analysis_context:
        text = (
            analysis_context
            if isinstance(analysis_context, str)
            else str(analysis_context)
        )
        messages.append({"role": "system", "content": f"## 专业分析\n{text}"})
