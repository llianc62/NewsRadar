"""Tests for agent/memory.py — MemoryModule hierarchy."""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent import (
    AgentResult,
    Context,
    DefaultAgent,
    DirectExecutor,
    LongTermMemory,
    MemoryModule,
    MemoryStorage,
    NullMemory,
    ShortTermMemory,
)
from agent.hub import ModelHub
from tests.test_agent_agent import MockClient


# ── Helpers ────────────────────────────────────────────────────


@pytest.fixture
def mock_hub(monkeypatch):
    from agent import hub as hub_module

    monkeypatch.setitem(hub_module._PROVIDER_MAP, "openai", MockClient)
    return ModelHub(config={
        "default": {"protocol": "openai", "model": "gpt-4o", "api_key": "sk-xxx"},
    })


def make_ctx(user_input: str = "hello", **kwargs) -> Context:
    return Context(user_input=user_input, **kwargs)


# ── Test MemoryModule ABC ──────────────────────────────────────


class TestMemoryModuleABC:
    def test_abstract_class_cannot_be_instantiated(self):
        with pytest.raises(TypeError):
            MemoryModule()  # type: ignore

    def test_null_memory_is_concrete(self):
        m = NullMemory()
        assert isinstance(m, MemoryModule)


# ── Test NullMemory ────────────────────────────────────────────


class TestNullMemory:
    @pytest.mark.asyncio
    async def test_on_before_does_nothing(self):
        m = NullMemory()
        ctx = make_ctx()
        await m.on_before_execute(ctx)
        assert ctx.memory_context is None  # 不设置任何东西

    @pytest.mark.asyncio
    async def test_on_after_does_nothing(self):
        m = NullMemory()
        ctx = make_ctx()
        ctx.assistant_output = "response"
        await m.on_after_execute(ctx)
        # 不抛异常即可


# ── Test ShortTermMemory ───────────────────────────────────────


class TestShortTermMemory:
    def test_init(self):
        m = ShortTermMemory(window_size=10)
        assert m.turn_count == 0

    def test_invalid_window_size(self):
        with pytest.raises(ValueError, match="window_size"):
            ShortTermMemory(window_size=0)
        with pytest.raises(ValueError, match="window_size"):
            ShortTermMemory(window_size=-1)

    @pytest.mark.asyncio
    async def test_on_before_empty_returns_empty_list(self):
        m = ShortTermMemory()
        ctx = make_ctx()
        await m.on_before_execute(ctx)
        assert ctx.memory_context == []

    @pytest.mark.asyncio
    async def test_on_before_returns_shallow_copy(self):
        m = ShortTermMemory()
        ctx = make_ctx()
        ctx.assistant_output = "hi"
        await m.on_after_execute(ctx)
        await m.on_before_execute(ctx)
        assert ctx.memory_context == [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]

    @pytest.mark.asyncio
    async def test_on_after_stores_turns(self):
        m = ShortTermMemory()
        ctx = make_ctx(user_input="hello")
        ctx.assistant_output = "world"
        await m.on_after_execute(ctx)
        assert m.turn_count == 1

        ctx2 = make_ctx(user_input="foo")
        ctx2.assistant_output = "bar"
        await m.on_after_execute(ctx2)
        assert m.turn_count == 2

    @pytest.mark.asyncio
    async def test_window_trimming(self):
        m = ShortTermMemory(window_size=2)  # 最多 2 轮（4 条消息）
        for i in range(5):
            ctx = make_ctx(user_input=f"q{i}")
            ctx.assistant_output = f"a{i}"
            await m.on_after_execute(ctx)

        assert m.turn_count == 2  # 只保留最近 2 轮
        assert len(m._window) == 4
        assert m._window[0]["content"] == "q3"  # 丢弃了最早的 3 轮
        assert m._window[-1]["content"] == "a4"

    @pytest.mark.asyncio
    async def test_clear(self):
        m = ShortTermMemory()
        ctx = make_ctx()
        ctx.assistant_output = "a"
        await m.on_after_execute(ctx)
        assert m.turn_count == 1
        m.clear()
        assert m.turn_count == 0
        assert m._window == []

    @pytest.mark.asyncio
    async def test_turn_count_property(self):
        m = ShortTermMemory()
        assert m.turn_count == 0
        ctx = make_ctx()
        ctx.assistant_output = "a"
        await m.on_after_execute(ctx)
        assert m.turn_count == 1
        await m.on_after_execute(ctx)
        assert m.turn_count == 2


# ── Test MemoryStorage ABC ─────────────────────────────────────


class TestMemoryStorageABC:
    def test_abstract_class_cannot_be_instantiated(self):
        with pytest.raises(TypeError):
            MemoryStorage()  # type: ignore


# ── Test LongTermMemory ────────────────────────────────────────


class TestLongTermMemory:
    @pytest.fixture
    def mock_storage(self):
        return AsyncMock(spec=MemoryStorage)

    @pytest.fixture
    def ltm(self, mock_storage):
        return LongTermMemory(storage=mock_storage, extract_interval=3)

    def test_invalid_interval(self, mock_storage):
        with pytest.raises(ValueError, match="extract_interval"):
            LongTermMemory(storage=mock_storage, extract_interval=0)

    @pytest.mark.asyncio
    async def test_on_before_skips_empty_input(self, ltm, mock_storage):
        ctx = make_ctx(user_input="")
        await ltm.on_before_execute(ctx)
        mock_storage.search.assert_not_called()

    @pytest.mark.asyncio
    async def test_on_before_skips_empty_query_after_cleaning(self, ltm, mock_storage):
        """纯符号输入清洗后应跳过检索（jieba 无法从中提取关键词）。"""
        ctx = make_ctx(user_input="!@#$%^&*()")
        await ltm.on_before_execute(ctx)
        mock_storage.search.assert_not_called()

    @pytest.mark.asyncio
    async def test_on_before_searches_memories(self, ltm, mock_storage):
        mock_storage.search.return_value = [
            {"content": "user likes python", "memory_type": "fact"},
        ]
        ctx = make_ctx(user_input="tell me about python")
        await ltm.on_before_execute(ctx)
        # jieba TF-IDF 提取关键词，确保 "python" 在查询中
        mock_storage.search.assert_called_once()
        query_arg = mock_storage.search.call_args[0][0]
        assert "python" in query_arg
        assert "user likes python" in ctx.memory_context

    @pytest.mark.asyncio
    async def test_on_before_no_results(self, ltm, mock_storage):
        mock_storage.search.return_value = []
        ctx = make_ctx(user_input="something")
        await ltm.on_before_execute(ctx)
        assert ctx.memory_context is None

    @pytest.mark.asyncio
    async def test_on_after_extract_triggered(self, ltm, mock_storage):
        """assistant_output > 100 字符应触发性存储。"""
        ctx = make_ctx(user_input="hi", session_id="sess-1")
        ctx.assistant_output = "x" * 101  # 超过 100 字符阈值
        await ltm.on_after_execute(ctx)
        mock_storage.save.assert_called_once()
        kwargs = mock_storage.save.call_args.kwargs
        assert kwargs["session_id"] == "sess-1"
        assert kwargs["memory_type"] == "fact"

    @pytest.mark.asyncio
    async def test_on_after_extract_triggered_by_keyword(self, ltm, mock_storage):
        """用户输入包含命名实体（张三 → nr）应触发性存储，即使输出很短。"""
        ctx = make_ctx(user_input="记住我叫张三", session_id="sess-1")
        ctx.assistant_output = "ok"  # 很短，不走长度触发
        await ltm.on_after_execute(ctx)
        mock_storage.save.assert_called_once()
        kwargs = mock_storage.save.call_args.kwargs
        assert kwargs["session_id"] == "sess-1"
        assert kwargs["memory_type"] == "fact"

    @pytest.mark.asyncio
    async def test_on_after_batch_merge_on_interval(self, ltm, mock_storage):
        """达到 extract_interval 时应周期性存储。"""
        ctx = make_ctx(user_input="hi", session_id="sess-1")
        ctx.assistant_output = "short"  # 不超过 100，不走触发性

        # 前 2 轮：不触发
        await ltm.on_after_execute(ctx)
        await ltm.on_after_execute(ctx)
        mock_storage.save.assert_not_called()

        # 第 3 轮：触发周期性存储
        await ltm.on_after_execute(ctx)
        mock_storage.save.assert_called_once()
        kwargs = mock_storage.save.call_args.kwargs
        assert kwargs["memory_type"] == "summary"

    @pytest.mark.asyncio
    async def test_on_after_short_output_no_extract(self, ltm, mock_storage):
        """短输出不应触发存储。"""
        ctx = make_ctx(user_input="hi")
        ctx.assistant_output = "ok"
        await ltm.on_after_execute(ctx)
        mock_storage.save.assert_not_called()

    def test_format_memories(self):
        memories = [
            {"content": "user likes python", "memory_type": "fact"},
            {"content": "user hates java", "memory_type": "fact"},
        ]
        result = LongTermMemory._format_memories(memories)
        assert "## 相关记忆" in result
        assert "[fact] user likes python" in result
        assert "[fact] user hates java" in result


# ── Integration: Executor + Memory ─────────────────────────────


class TestExecutorWithMemory:
    @pytest.mark.asyncio
    async def test_direct_executor_calls_memory_hooks(self, mock_hub):
        """验证 DirectExecutor 在 run 中调用了 memory hook。"""
        memory = MagicMock(spec=ShortTermMemory)
        memory.on_before_execute = AsyncMock()
        memory.on_after_execute = AsyncMock()

        executor = DirectExecutor()
        ctx = Context(user_input="hello", system_prompt="You are a bot.")

        await executor.run(ctx=ctx, brain=mock_hub, memory=memory)

        memory.on_before_execute.assert_awaited_once_with(ctx)
        memory.on_after_execute.assert_awaited_once_with(ctx)

    @pytest.mark.asyncio
    async def test_direct_executor_calls_memory_hooks_stream(self, mock_hub):
        memory = MagicMock(spec=ShortTermMemory)
        memory.on_before_execute = AsyncMock()
        memory.on_after_execute = AsyncMock()

        executor = DirectExecutor()
        ctx = Context(user_input="hello", system_prompt="You are a bot.")

        tokens = [t async for t in executor.run_stream(ctx=ctx, brain=mock_hub, memory=memory)]
        assert tokens == ["mock ", "response"]

        memory.on_before_execute.assert_awaited_once_with(ctx)
        memory.on_after_execute.assert_awaited_once_with(ctx)

    @pytest.mark.asyncio
    async def test_memory_context_injected_into_messages(self, mock_hub):
        """验证 memory_context 被注入到 LLM 的 messages 中。"""
        memory = ShortTermMemory(window_size=5)
        executor = DirectExecutor()
        ctx = Context(user_input="hello", system_prompt="You are a bot.")

        # 第一轮：memory_context 为空
        await executor.run(ctx=ctx, brain=mock_hub, memory=memory)
        client = mock_hub._clients["default"]
        # 没有 memory_context，只有 system + user
        assert len(client.last_messages) == 2

        # 第二轮：memory_context 应包含上轮对话
        ctx2 = Context(user_input="world", system_prompt="You are a bot.")
        await executor.run(ctx=ctx2, brain=mock_hub, memory=memory)
        assert len(client.last_messages) == 3  # system + memory + user
        assert "## 对话历史" in client.last_messages[1]["content"]
        assert "hello" in client.last_messages[1]["content"]

    @pytest.mark.asyncio
    async def test_default_memory_is_null(self, mock_hub):
        """不传 memory 时默认 NullMemory，不应注入 memory_context。"""
        executor = DirectExecutor()
        ctx = Context(user_input="hello", system_prompt="You are a bot.")
        await executor.run(ctx=ctx, brain=mock_hub)  # 不传 memory
        client = mock_hub._clients["default"]
        assert len(client.last_messages) == 2  # 只有 system + user


# ── Integration: DefaultAgent + Memory ─────────────────────────


class TestDefaultAgentWithMemory:
    @pytest.fixture
    def agent_with_memory(self, monkeypatch):
        from agent import hub as hub_module

        monkeypatch.setitem(hub_module._PROVIDER_MAP, "openai", MockClient)
        return DefaultAgent(
            config={
                "default": {"protocol": "openai", "model": "gpt-4o", "api_key": "sk-xxx"},
            },
            system_prompt="You are a helper.",
            memory=ShortTermMemory(window_size=10),
        )

    @pytest.mark.asyncio
    async def test_chat_with_memory_stores_history(self, agent_with_memory):
        """连续 chat 应积累记忆。"""
        await agent_with_memory.chat("hello")
        assert agent_with_memory.memory.turn_count == 1

        await agent_with_memory.chat("world")
        assert agent_with_memory.memory.turn_count == 2

    @pytest.mark.asyncio
    async def test_chat_stream_with_memory_stores_history(self, agent_with_memory):
        tokens = [t async for t in agent_with_memory.chat_stream("hello")]
        assert "".join(tokens) == "mock response"
        assert agent_with_memory.memory.turn_count == 1

        tokens = [t async for t in agent_with_memory.chat_stream("world")]
        assert "".join(tokens) == "mock response"
        assert agent_with_memory.memory.turn_count == 2

    @pytest.mark.asyncio
    async def test_default_agent_has_null_memory(self, monkeypatch):
        """不传 memory 时应默认使用 NullMemory。"""
        from agent import hub as hub_module

        monkeypatch.setitem(hub_module._PROVIDER_MAP, "openai", MockClient)
        agent = DefaultAgent(
            config={
                "default": {"protocol": "openai", "model": "gpt-4o", "api_key": "sk-xxx"},
            },
        )
        assert isinstance(agent.memory, NullMemory)

    @pytest.mark.asyncio
    async def test_chat_with_session_id(self, agent_with_memory):
        """传入 session_id 应传递到 Context。"""
        result = await agent_with_memory.chat("hello", session_id="sess-123")
        assert isinstance(result, AgentResult)
        assert result.content == "mock response for gpt-4o"
