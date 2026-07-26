"""Executor hierarchy - DirectExecutor and ReActExecutor.

All executors use the LLMClient protocol (``client.chat()`` / ``client.chat_stream()``)
which returns ``AIMessage`` directly - no ``ChatResult`` intermediate format.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any

from .model_hub import ModelHub
from .memory import MemoryModule, NullMemory
from .data import Context, Message, ToolResult

logger = logging.getLogger(__name__)


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
    """执行器基类--定义 Agent 的执行策略。"""

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
    """简单直调执行器--没有 ReAct 循环，没有工具调用。

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
    """ReAct 风格的推理循环--Agent 自主决定调工具还是回答。

    两级循环设计（参考 Pi agent loop）：
    - 内层循环：LLM -> 工具调用 -> LLM -> ... -> 文本回答
    - 工具执行三阶段：prepare -> execute -> finalize，每个工具独立隔离

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
        brain: ModelHub,
        memory: MemoryModule,
        knowledge=None,
        tools=None,
        max_steps: int = 10,
        max_tool_rounds: int = 5,
        llm_max_retries: int = 2,
        tool_max_retries: int = 1,
        approval_callback=None,
        hooks: list[ExecutorHook] | None = None,
    ):
        if max_steps < 1:
            raise ValueError("max_steps must be >= 1")
        super().__init__(approval_callback=approval_callback, hooks=hooks)
        self._brain = brain
        self._memory = memory
        self._knowledge = knowledge
        self._tools = tools
        self.max_steps = max_steps
        self.max_tool_rounds = max_tool_rounds
        self._llm_max_retries = llm_max_retries
        self._tool_max_retries = tool_max_retries

    # ── 三阶段:prepare ─────────────────────────────────────────

    async def _prepare(self, ctx: Context) -> None:
        """调用前准备 -- 加载记忆 + 检索知识 + 初始化工作区 messages。

        memory.load / knowledge.search 任一失败均 catch 降级(日志 warning),
        不阻断执行流(spec 6.2 / 降级矩阵)。
        """
        try:
            await self._memory.load(ctx)
        except Exception as e:
            logger.warning("memory.load failed, degrade: %s", e)
        if self._knowledge:
            try:
                await self._knowledge.search(ctx)
            except Exception as e:
                logger.warning("knowledge.search failed, degrade: %s", e)
        ctx.messages = [Message(role="user", content=ctx.user_input)]

    # ── LLM 消息拼装 ───────────────────────────────────────────

    def _build_llm_messages(self, ctx: Context) -> list[dict]:
        """每轮 LLM 调用前拼装消息列表(spec 3.5 注入规则)。

        顺序: system_prompt -> memories(按 order 升序,每个拼成 system 块)
              -> history_messages(直接拼接) -> messages(工作区,含 tool_calls 转换)。
        """
        msgs: list[dict] = []
        if ctx.system_prompt:
            msgs.append({"role": "system", "content": ctx.system_prompt})
        for mb in sorted(ctx.memories, key=lambda m: m.order):
            msgs.append({"role": "system", "content": f"## {mb.title}\n{mb.content}"})
        for m in ctx.history_messages:
            msgs.append({"role": m.role, "content": m.content or ""})
        for m in ctx.messages:
            msgs.append(self._message_to_dict(m))
        return msgs

    @staticmethod
    def _message_to_dict(m: Message) -> dict:
        """将单条 Message 转为 LLM API dict。

        处理 tool_calls 格式转换(存储格式 -> API 格式)、reasoning_content
        回传(DeepSeek 思考模式兼容)、tool 消息的 tool_call_id。
        """
        d: dict = {"role": m.role}
        if m.role == "assistant" and m.tool_calls:
            # DeepSeek 思考模式要求 tool call 消息的 content 不能为 None
            # （ OpenAI / Anthropic 均兼容空字符串，不影响 ）
            d["content"] = m.content or ""
            d["tool_calls"] = _tool_calls_to_api(m.tool_calls)
            # DeepSeek 思考模式：reasoning_content 必须回传给 API
            if m.reasoning_content:
                d["reasoning_content"] = m.reasoning_content
        elif m.role == "tool":
            d["tool_call_id"] = m.tool_call_id or ""
            d["content"] = m.content or ""
        else:
            d["content"] = m.content or ""
        return d

    # ── 推理循环 (Task 8) ──────────────────────────────────────

    async def _react_step(self, ctx: Context, client, tool_schemas, model_version):
        """单步 LLM 调用 -- hooks + 重试 + 字段提取 + token 累计。

        不修改 ``ctx.messages``(由调用方决定如何 append assistant 消息)。
        返回 ``(ai, tool_calls, finish_reason)``。
        """
        for h in self._hooks:
            try: await h.before_chat(ctx)
            except Exception as e: logger.warning("before_chat hook: %s", e)

        llm_messages = self._build_llm_messages(ctx)
        ai = await self._call_llm(client, llm_messages, tool_schemas, ctx)

        for h in self._hooks:
            try: await h.after_chat(ctx, ai)
            except Exception as e: logger.warning("after_chat hook: %s", e)

        tool_calls = list(ai.tool_calls) if ai.tool_calls else []
        finish_reason = (ai.response_metadata or {}).get("finish_reason", "")
        usage = _ai_usage_to_dict(ai)
        if usage:
            ctx.total_input_tokens += usage.get("prompt_tokens", 0)
            ctx.total_output_tokens += usage.get("completion_tokens", 0)
        return ai, tool_calls, finish_reason

    @staticmethod
    def _make_assistant_msg(ai, tool_calls, model_version, content=None):
        """从 AIMessage 构建 assistant Message(可选覆盖 content,用于流式累积)。"""
        return Message(
            role="assistant",
            content=content if content is not None else (ai.content or None),
            tool_calls=tool_calls or None,
            usage=_ai_usage_to_dict(ai) or None,
            reasoning_content=_ai_reasoning_content(ai) or None,
            model_used=model_version,
        )

    async def _loop(self, ctx: Context, stream: bool) -> None:
        tool_schemas = self._tools.get_schemas() if self._tools else None
        client = self._brain.get(ctx.model_name or "default")
        model_version = self._brain.get_model_version(ctx.model_name or "default")
        tool_rounds = 0

        for step in range(self.max_steps):
            ai, tool_calls, finish_reason = await self._react_step(
                ctx, client, tool_schemas, model_version,
            )
            ctx.messages.append(
                self._make_assistant_msg(ai, tool_calls, model_version)
            )

            if not tool_calls:
                ctx.step_count = step + 1
                return

            if finish_reason in ("length", "max_tokens"):
                for tc in tool_calls:
                    ctx.messages.append(Message(
                        role="tool", tool_call_id=tc.get("id", ""),
                        content="[Error] 工具调用被截断", name=tc.get("name", ""),
                    ))
                ctx.step_count = step + 1
                return

            for tc in tool_calls:
                msg = await self._execute_tool(tc, ctx)
                ctx.messages.append(msg)

            tool_rounds += 1
            if tool_rounds >= self.max_tool_rounds:
                tool_schemas = None
        ctx.step_count = self.max_steps

    async def _call_llm(self, client, messages, tool_schemas, ctx):
        last = None
        for attempt in range(self._llm_max_retries + 1):
            try:
                return await client.chat(messages=messages, tools=tool_schemas)
            except Exception as e:
                last = e
                if attempt < self._llm_max_retries:
                    await asyncio.sleep(2 ** attempt)
                else:
                    raise

    # ── 工具执行 (Task 8) ──────────────────────────────────────

    async def _execute_tool(self, tc: dict, ctx: Context) -> Message:
        name, args = tc["name"], tc.get("args", {})
        for h in self._hooks:
            try:
                new = await h.before_tool(name, args, ctx)
                if new is not None: args = new
            except Exception as e: logger.warning("before_tool hook: %s", e)

        tr = ToolResult(name=name, args=args, tool_call_id=tc.get("id", ""))
        start = time.monotonic()
        for attempt in range(self._tool_max_retries + 1):
            try:
                raw = await self._exec_tool_with_policy(self._tools, name, args, ctx.running_mode)
                tr.result = raw
                tr.success = True
                break
            except Exception as e:
                tr.error = str(e)
                tr.retries = attempt
                if attempt < self._tool_max_retries:
                    await asyncio.sleep(2 ** attempt)
                else:
                    tr.success = False
        tr.timing_ms = int((time.monotonic() - start) * 1000)

        tr.result = self._normalize_tool_result(tr.result) if tr.success else tr.result
        content = tr.result if tr.success else f"[Error] {tr.error}"
        tool_msg = Message(
            role="tool", tool_call_id=tr.tool_call_id,
            content=content, name=name, tool_result=tr,
        )
        for h in self._hooks:
            try: await h.after_tool(tool_msg, ctx)
            except Exception as e: logger.warning("after_tool hook: %s", e)
        return tool_msg

    # ── 三阶段:finalize + run/run_stream (Task 9) ──────────────

    async def _finalize(self, ctx: Context) -> None:
        """收尾 -- 保存对话 + 提炼长期记忆。

        ``memory.save`` 失败仅 log.warning,不影响已完成的返回值
        (spec 6.2 / 9.1 异常矩阵)。
        """
        try:
            await self._memory.save(ctx)
        except Exception as e:
            logger.warning("memory.save failed, not fatal: %s", e)

    async def run(self, ctx: Context) -> str:
        """非流式推理 -- ``_prepare`` -> ``_loop`` -> ``_finalize``。

        异常兜底:``_loop`` 内未捕获的异常(如 LLM 重试耗尽)在此 catch,
        记 ``log.exception`` + ``on_error`` hook,返回 ``[Executor 错误]`` 文本
        (不进 messages,spec 9.2 原则)。``_finalize`` 始终执行。
        """
        await self._prepare(ctx)
        try:
            await self._loop(ctx, stream=False)
            output = ctx.final_output
        except Exception as e:
            logger.exception("executor run failed")
            for h in self._hooks:
                try: await h.on_error(e, ctx)
                except Exception: pass
            output = f"[Executor 错误] {e}"
        finally:
            await self._finalize(ctx)
        return output

    async def run_stream(self, ctx: Context) -> AsyncIterator[str]:
        """流式推理 -- ``_prepare`` -> ``_loop_stream`` -> ``_finalize``。

        工具调用步骤非流式,最终文本步用 ``chat_stream`` 逐 token yield。
        异常兜底同 ``run``:catch 后 ``yield [Executor 错误]`` 文本,
        ``_finalize`` 始终执行。
        """
        await self._prepare(ctx)
        try:
            async for tok in self._loop_stream(ctx):
                yield tok
        except Exception as e:
            logger.exception("executor run_stream failed")
            for h in self._hooks:
                try: await h.on_error(e, ctx)
                except Exception: pass
            yield f"[Executor 错误] {e}"
        finally:
            await self._finalize(ctx)

    async def _loop_stream(self, ctx: Context) -> AsyncIterator[str]:
        """流式推理循环 -- 共享 ``_react_step`` 单步逻辑,最终文本步真流式。

        与 ``_loop`` 的差异:无 tool_calls 的最终步先用非流式 ``chat`` 判定
        无工具调用,再用 ``chat_stream`` 逐 token yield(assistant 消息以
        流式累积内容入历史,确保 ``ctx.final_output`` 反映真实输出)。
        """
        tool_schemas = self._tools.get_schemas() if self._tools else None
        client = self._brain.get(ctx.model_name or "default")
        model_version = self._brain.get_model_version(ctx.model_name or "default")
        tool_rounds = 0

        for step in range(self.max_steps):
            ai, tool_calls, finish_reason = await self._react_step(
                ctx, client, tool_schemas, model_version,
            )

            if not tool_calls:
                # 最终文本步:chat_stream 真流式(assistant 消息尚未入历史,
                # 避免把答案回传给 LLM)
                ctx.step_count = step + 1
                chunks: list[str] = []
                async for chunk in client.chat_stream(
                    messages=self._build_llm_messages(ctx),
                ):
                    token = chunk.content or ""
                    if token:
                        chunks.append(token)
                        yield token
                ctx.messages.append(
                    self._make_assistant_msg(
                        ai, tool_calls, model_version,
                        content="".join(chunks) or None,
                    )
                )
                return

            # 有工具调用:append assistant 消息(含 tool_calls)再执行工具
            ctx.messages.append(
                self._make_assistant_msg(ai, tool_calls, model_version)
            )

            if finish_reason in ("length", "max_tokens"):
                for tc in tool_calls:
                    ctx.messages.append(Message(
                        role="tool", tool_call_id=tc.get("id", ""),
                        content="[Error] 工具调用被截断", name=tc.get("name", ""),
                    ))
                ctx.step_count = step + 1
                if ai.content:
                    yield ai.content
                return

            for tc in tool_calls:
                msg = await self._execute_tool(tc, ctx)
                ctx.messages.append(msg)

            tool_rounds += 1
            if tool_rounds >= self.max_tool_rounds:
                tool_schemas = None
        ctx.step_count = self.max_steps
        fallback = (ctx.messages[-1].content or "已达最大步数") if ctx.messages else "已达最大步数"
        yield fallback

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
    def _messages_to_dicts(messages: list[Message]) -> list[dict]:
        """将 Message 列表转为 LLM API 所需的 dict 列表。

        转换时处理 tool_calls 格式：存储格式 ``[{name, args, id}]`` -> API 格式 ``[{function: {name, arguments}}]``。
        单条转换逻辑见 ``_message_to_dict``。
        """
        return [ReActExecutor._message_to_dict(m) for m in messages]


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
