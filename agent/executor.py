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

#: ``max_tool_rounds`` 打满后注入的引导语。摘除 tool schemas 后必须显式告知
#: 模型"不要再调工具"，否则 DeepSeek 等模型会把工具调用意图写成 DSML 等
#: 标记纯文本（与请求不带 tools 时同一退化路径）。
_TOOL_CAP_GUIDANCE = (
    "工具调用轮次已达上限。请基于已获取的信息直接回答，不要再尝试调用工具。"
)


def _chunk_text(chunk: Any) -> str:
    """从 ``AIMessageChunk.content`` 提取文本增量。

    content 兼容两种形态：
    - OpenAI 系（含 DeepSeek）：str，直接返回；
    - Anthropic 系（含 thinking）：content block 列表，只取 ``type="text"``
      块的文本；thinking / tool_use 块跳过（否则 block repr 会被当文本拼进回复）。
    """
    content = getattr(chunk, "content", None)
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


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


class ReActExecutor(Executor):
    """ReAct 风格的推理循环--Agent 自主决定调工具还是回答。

    两级循环设计（参考 Pi agent loop）：
    - 内层循环：LLM -> 工具调用 -> LLM -> ... -> 文本回答
    - 工具执行三阶段：prepare -> execute -> finalize，每个工具独立隔离

    相较于旧版的核心改进：
    - ``_build_messages`` 只执行一次，不再每轮重复拼接 user_input
    - 检查 ``stop_reason``，截断时（length）不执行不完整的工具调用
    - 每个工具调用独立 try/except，错误不污染其他工具
    - 保留 tool schemas 直到 ``max_tool_rounds`` 轮后移除（并注入收尾引导），
      确保 LLM 始终能正确输出结构化 JSON tool_calls，避免因 schemas 缺失导致
      LLM 退化为 XML/DSML 文本格式
    - 真正的流式输出：流式路径每步 ``chat_stream`` 且始终绑定 tool schemas，
      聚合 chunk 还原 tool_calls，最终回答不再二次生成
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
        """调用前准备 -- 检索知识 + 追加本轮 user 到 ctx.messages。

        memory.load 已由 DefaultAgent 首次 chat 时调用(懒加载),此处不重复。
        ctx.messages 跨轮累积,本轮只 append user(不重置)。
        knowledge.search 每轮调(与当前用户输入相关),失败降级不阻断。
        """
        if self._knowledge:
            try:
                await self._knowledge.search(ctx)
            except Exception as e:
                logger.warning("knowledge.search failed, degrade: %s", e)
        ctx.messages.append(Message(role="user", content=ctx.user_input))

    # ── LLM 消息拼装 ───────────────────────────────────────────

    def _build_llm_messages(self, ctx: Context) -> list[dict]:
        """每轮 LLM 调用前拼装消息列表。

        顺序: system_prompt -> memories(按 order 升序,每个拼成 system 块)
              -> messages(跨轮累积的完整对话,含 tool_calls 转换)。
        """
        msgs: list[dict] = []
        if ctx.system_prompt:
            msgs.append({"role": "system", "content": ctx.system_prompt})
        for mb in sorted(ctx.memories, key=lambda m: m.order):
            msgs.append({"role": "system", "content": f"## {mb.title}\n{mb.content}"})
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

    @staticmethod
    def _merge_guidance(llm_messages: list[dict], guidance: str) -> None:
        """把收尾引导并入头部 system 块(就地修改)。

        不能追加为尾部 system 消息:langchain-anthropic 拒绝非连续的多条
        system 消息( anthropic 协议请求会直接 400)。无 system 消息时在
        头部插入一条。
        """
        if not guidance:
            return
        if llm_messages and llm_messages[0].get("role") == "system":
            llm_messages[0]["content"] = (
                f"{llm_messages[0]['content']}\n\n{guidance}"
            )
        else:
            llm_messages.insert(0, {"role": "system", "content": guidance})

    async def _react_step(self, ctx: Context, client, tool_schemas, model_version,
                          guidance: str = ""):
        """单步 LLM 调用 -- hooks + 重试 + 字段提取 + token 累计。

        不修改 ``ctx.messages``(由调用方决定如何 append assistant 消息)。
        ``guidance`` 非空时并入头部 system 块(不进 ctx.messages)，用于
        ``max_tool_rounds`` 打满后的收尾引导。
        返回 ``(ai, tool_calls, finish_reason)``。
        """
        for h in self._hooks:
            try: await h.before_chat(ctx)
            except Exception as e: logger.warning("before_chat hook: %s", e)

        llm_messages = self._build_llm_messages(ctx)
        self._merge_guidance(llm_messages, guidance)
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

    async def _loop(self, ctx: Context) -> None:
        tool_schemas = self._tools.get_schemas() if self._tools else None
        client = self._brain.get(ctx.model_name or "default")
        model_version = self._brain.get_model_version(ctx.model_name or "default")
        tool_rounds = 0
        guidance = ""

        for step in range(self.max_steps):
            ai, tool_calls, finish_reason = await self._react_step(
                ctx, client, tool_schemas, model_version, guidance=guidance,
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

            await self._execute_tool_calls(tool_calls, ctx)

            tool_rounds += 1
            if tool_rounds >= self.max_tool_rounds:
                tool_schemas = None
                guidance = _TOOL_CAP_GUIDANCE
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
            await self._loop(ctx)
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

        每步都流式且绑定 tool schemas(见 ``_loop_stream``)。异常兜底同
        ``run``:catch 后 ``yield [Executor 错误]`` 文本,
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

    async def _execute_tool_calls(self, tool_calls: list[dict], ctx: Context) -> None:
        """依次执行工具调用并 append 结果;用户中断时补占位再抛。

        assistant(tool_calls) 消息此时已入历史,若 tool_call 无对应 tool
        结果消息,下轮 ``_build_llm_messages`` 产生的请求会被
        OpenAI/DeepSeek 以 400 拒绝,故 CancelledError 时为未回填的
        tool_call 补中断占位消息。
        """
        try:
            for tc in tool_calls:
                msg = await self._execute_tool(tc, ctx)
                ctx.messages.append(msg)
        except asyncio.CancelledError:
            answered = {m.tool_call_id for m in ctx.messages if m.role == "tool"}
            for tc in tool_calls:
                if tc.get("id", "") not in answered:
                    ctx.messages.append(Message(
                        role="tool", tool_call_id=tc.get("id", ""),
                        content="[Error] 用户中断,工具未执行", name=tc.get("name", ""),
                    ))
            raise

    async def _loop_stream(self, ctx: Context) -> AsyncIterator[str]:
        """真流式推理循环 -- 每步都 ``chat_stream(tools=...)``，聚合 chunk。

        早期实现是两段式：非流式 ``chat``（带 tools）判定无工具调用后，再用
        ``chat_stream``（**不带 tools**）重新生成最终回答。重新生成时请求缺
        tools，DeepSeek 等模型会把工具调用意图写成 DSML 等标记纯文本流给用户，
        而判定调用生成的真正回答被丢弃（还会为最终回答付双倍生成成本）。

        现改为与 ``_loop`` 同构的单层循环：每步流式且始终绑定 tool schemas，
        ``AIMessageChunk`` 聚合（``+`` 合并）后从 ``agg.tool_calls`` 还原工具
        调用——无工具调用的步即最终回答，逐 token 已流出，不再二次生成。
        """
        tool_schemas = self._tools.get_schemas() if self._tools else None
        client = self._brain.get(ctx.model_name or "default")
        model_version = self._brain.get_model_version(ctx.model_name or "default")
        tool_rounds = 0
        guidance = ""

        for step in range(self.max_steps):
            for h in self._hooks:
                try: await h.before_chat(ctx)
                except Exception as e: logger.warning("before_chat hook: %s", e)

            llm_messages = self._build_llm_messages(ctx)
            self._merge_guidance(llm_messages, guidance)

            agg = None
            chunks: list[str] = []
            try:
                for attempt in range(self._llm_max_retries + 1):
                    try:
                        async for chunk in client.chat_stream(
                            messages=llm_messages, tools=tool_schemas,
                        ):
                            token = _chunk_text(chunk)
                            if token:
                                chunks.append(token)
                                # 调用方只可能以 cancel(CancelledError,
                                # BaseException)中断生成器,不会 athrow 普通
                                # Exception,故此处 yield 不会被误当流式失败重试
                                yield token
                            agg = chunk if agg is None else agg + chunk
                        break
                    except Exception:
                        # 已消费过 chunk 则不能重试(重放会重复输出),直接抛
                        if agg is not None or attempt >= self._llm_max_retries:
                            raise
                        await asyncio.sleep(2 ** attempt)
            except asyncio.CancelledError:
                # 用户 stop:已流出的 partial 先入历史,
                # run_stream 的 finally 会 _finalize 落库(不丢 partial)
                if chunks:
                    ctx.messages.append(
                        self._make_assistant_msg(
                            agg, [], model_version, content="".join(chunks),
                        )
                    )
                raise

            ai = agg  # AIMessageChunk 是 AIMessage 子类,复用消息构建
            if ai is None:
                # 流式响应完全为空:按空最终回答收尾,避免 AttributeError
                ctx.step_count = step + 1
                ctx.messages.append(
                    Message(role="assistant", content=None, model_used=model_version)
                )
                return
            for h in self._hooks:
                try: await h.after_chat(ctx, ai)
                except Exception as e: logger.warning("after_chat hook: %s", e)

            tool_calls = list(ai.tool_calls) if ai.tool_calls else []
            finish_reason = (ai.response_metadata or {}).get("finish_reason", "")
            usage = _ai_usage_to_dict(ai)
            if usage:
                ctx.total_input_tokens += usage.get("prompt_tokens", 0)
                ctx.total_output_tokens += usage.get("completion_tokens", 0)

            if not tool_calls:
                # 最终文本步:内容已逐 token 流出,直接入历史
                ctx.step_count = step + 1
                ctx.messages.append(
                    self._make_assistant_msg(
                        ai, tool_calls, model_version,
                        content="".join(chunks) or None,
                    )
                )
                return

            # 有工具调用:append assistant 消息(含 tool_calls)再执行工具
            ctx.messages.append(
                self._make_assistant_msg(
                    ai, tool_calls, model_version,
                    content="".join(chunks) or None,
                )
            )

            if finish_reason in ("length", "max_tokens"):
                for tc in tool_calls:
                    ctx.messages.append(Message(
                        role="tool", tool_call_id=tc.get("id", ""),
                        content="[Error] 工具调用被截断", name=tc.get("name", ""),
                    ))
                ctx.step_count = step + 1
                return

            await self._execute_tool_calls(tool_calls, ctx)

            tool_rounds += 1
            if tool_rounds >= self.max_tool_rounds:
                tool_schemas = None
                guidance = _TOOL_CAP_GUIDANCE
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


class DirectExecutor(ReActExecutor):
    """简单直调执行器 -- 单次 chat,无工具循环。共享 _prepare/_finalize。

    流式路径继承 ``ReActExecutor.run_stream``(无 tools 时即单次直答)。
    """

    async def _loop(self, ctx: Context) -> None:
        client = self._brain.get(ctx.model_name or "default")
        model_version = self._brain.get_model_version(ctx.model_name or "default")
        for h in self._hooks:
            try: await h.before_chat(ctx)
            except Exception as e: logger.warning("before_chat hook: %s", e)

        llm_messages = self._build_llm_messages(ctx)
        ai = await self._call_llm(client, llm_messages, None, ctx)
        content = ai.content or ""
        for h in self._hooks:
            try: await h.after_chat(ctx, ai)
            except Exception as e: logger.warning("after_chat hook: %s", e)

        ctx.messages.append(Message(role="assistant", content=content or None, model_used=model_version))
        ctx.step_count = 1
