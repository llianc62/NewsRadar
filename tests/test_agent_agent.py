"""Tests for agent/v1/ — Phase 1: skeleton + mock LLM."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk

from agent import (
    AgentResult,
    AnthropicClient,
    Context,
    DefaultAgent,
    DirectExecutor,
    Executor,
    MemoryModule,
    ModelHub,
    NullMemory,
    OpenAIClient,
    ReActExecutor,
)
from agent.data import MemoryBlock, Message
from agent.executor import _chunk_text


# ── helpers ──────────────────────────────────────────────────────


def _patch_hub(monkeypatch):
    """用 MockClient 替换 hub._build_client，避免真实 API / LangChain 调用。"""
    from agent import model_hub as hub_module

    def _fake_build(cfg):
        return MockClient(api_key=cfg.get("api_key", ""), base_url=cfg.get("base_url", ""))

    monkeypatch.setattr(hub_module, "_build_client", _fake_build)


class MockClient:
    """Mock LLM Client 返回固定响应，不调真实 API。

    实现 ``LLMClient`` 协议（``chat()`` / ``chat_stream()``），
    返回 ``AIMessage`` 而非 ``ChatResult``。
    """

    def __init__(self, api_key: str = "", base_url: str = ""):
        self.api_key = api_key
        self.base_url = base_url
        self.last_messages: list[dict] = []
        # 如果非空，chat() 会返回包含 tool_calls 的 AIMessage
        self.tool_calls_to_return: list[dict] | None = None

    async def chat(self, messages, tools=None, **kwargs) -> AIMessage:
        self.last_messages = messages
        if self.tool_calls_to_return:
            return AIMessage(
                content="",
                tool_calls=self.tool_calls_to_return,
            )
        return AIMessage(content="mock response for gpt-4o")

    async def chat_stream(self, messages, **kwargs) -> AsyncIterator[AIMessageChunk]:
        self.last_messages = messages
        for token in ["mock ", "response"]:
            yield AIMessageChunk(content=token)


def make_ai(content="", tool_calls=None):
    """Create an AIMessage-like object for executor _loop tests.

    Normalizes tool_calls to langchain ToolCall format (name/args/id/type).
    """
    tcs = []
    for tc in (tool_calls or []):
        tcs.append({
            "name": tc["name"],
            "args": tc.get("args", {}),
            "id": tc.get("id", ""),
            "type": "tool_call",
        })
    return AIMessage(content=content, tool_calls=tcs)


# ── Test Context ─────────────────────────────────────────────────


class TestContext:
    def test_defaults(self):
        ctx = Context(user_input="hello")
        assert ctx.user_input == "hello"
        assert ctx.session_id == ""
        assert ctx.system_prompt == ""
        assert ctx.model_name == "default"
        assert ctx.final_output == ""
        assert ctx.step_count == 0
        assert ctx.total_input_tokens == 0
        assert ctx.total_output_tokens == 0

    def test_custom_values(self):
        ctx = Context(
            user_input="hi",
            session_id="sess-1",
            system_prompt="You are a helper.",
            model_name="cheap",
        )
        assert ctx.model_name == "cheap"
        assert ctx.system_prompt == "You are a helper."


# ── Test AgentResult ─────────────────────────────────────────────


class TestAgentResult:
    def test_creates_with_content(self):
        r = AgentResult(content="hello world")
        assert r.content == "hello world"
        assert r.model_used == ""
        assert r.total_tokens == 0
        assert r.step_count == 0

    def test_with_metadata(self):
        r = AgentResult(content="hi", model_used="gpt-4o", total_tokens=100)
        assert r.model_used == "gpt-4o"
        assert r.total_tokens == 100


# ── Test Client 构造 ──────────────────────────────────────────


class TestClientConstruction:
    def test_openai_client_imports(self):
        """OpenAIClient 可构造（仅验证导入路径和签名）。"""
        client = OpenAIClient(api_key="sk-xxx", model="gpt-4o")
        assert client.model_name == "gpt-4o"

    def test_anthropic_client_imports(self):
        client = AnthropicClient(api_key="sk-xxx", model="claude-sonnet-5")
        assert client.model == "claude-sonnet-5"


# ── Test ModelHub ────────────────────────────────────────────────


@pytest.fixture
def two_model_hub(monkeypatch):
    """用 MockClient 替换 _build_client，避免真实 API 调用。"""
    _patch_hub(monkeypatch)

    return ModelHub(config={
        "default": {"protocol": "openai", "model": "gpt-4o", "api_key": "sk-xxx"},
        "cheap": {"protocol": "openai", "model": "gpt-4o-mini", "api_key": "sk-xxx"},
        "claude": {"protocol": "anthropic", "model": "claude-sonnet-5", "api_key": "sk-xxx"},
    })


class TestModelHub:
    def test_get_default_returns_client(self, two_model_hub):
        client = two_model_hub.get_default()
        assert isinstance(client, MockClient)

    def test_get_returns_client(self, two_model_hub):
        client = two_model_hub.get("cheap")
        assert isinstance(client, MockClient)

    def test_get_lazy_creation(self, two_model_hub):
        """Client 在第一次 get 时才创建。"""
        assert len(two_model_hub._clients) == 0
        two_model_hub.get("default")
        assert len(two_model_hub._clients) == 1

    def test_get_reuses_client(self, two_model_hub):
        c1 = two_model_hub.get("default")
        c2 = two_model_hub.get("default")
        assert c1 is c2

    def test_get_unknown_raises(self, two_model_hub):
        with pytest.raises(KeyError, match="unknown_model"):
            two_model_hub.get("unknown_model")

    def test_get_default_without_default_config_raises(self):
        hub = ModelHub(config={
            "cheap": {"protocol": "openai", "model": "gpt-4o-mini", "api_key": "sk-xxx"},
        })
        with pytest.raises(KeyError, match="default"):
            hub.get_default()

    def test_unknown_protocol_raises(self, monkeypatch):
        hub = ModelHub(config={
            "default": {"protocol": "nonexistent", "model": "x", "api_key": "sk-xxx"},
        })
        with pytest.raises(ValueError, match="Unsupported protocol.*nonexistent"):
            hub.get_default()


# ── Test DirectExecutor ──────────────────────────────────────────


@pytest.fixture
def executor(mock_hub):
    from agent.memory import NullMemory
    return DirectExecutor(brain=mock_hub, memory=NullMemory())


@pytest.fixture
def mock_hub(monkeypatch):
    _patch_hub(monkeypatch)

    return ModelHub(config={
        "default": {"protocol": "openai", "model": "gpt-4o", "api_key": "sk-xxx"},
    })


class TestDirectExecutor:
    @pytest.mark.asyncio
    async def test_run_returns_response(self, executor, mock_hub):
        ctx = Context(user_input="hello", system_prompt="You are a bot.")
        result = await executor.run(ctx)
        assert result == "mock response for gpt-4o"
        assert ctx.final_output == "mock response for gpt-4o"

    @pytest.mark.asyncio
    async def test_run_builds_messages_correctly(self, executor, mock_hub):
        ctx = Context(user_input="hello", system_prompt="You are a bot.")
        await executor.run(ctx)
        client = mock_hub._clients["default"]
        assert client.last_messages == [
            {"role": "system", "content": "You are a bot."},
            {"role": "user", "content": "hello"},
        ]

    @pytest.mark.asyncio
    async def test_run_without_system_prompt(self, executor, mock_hub):
        ctx = Context(user_input="hello")
        await executor.run(ctx)
        client = mock_hub._clients["default"]
        assert client.last_messages == [
            {"role": "user", "content": "hello"},
        ]

    @pytest.mark.asyncio
    async def test_run_stream_yields_tokens(self, executor, mock_hub):
        ctx = Context(user_input="hello", system_prompt="You are a bot.")
        tokens = [t async for t in executor.run_stream(ctx)]
        assert tokens == ["mock ", "response"]
        assert ctx.final_output == "mock response"

    @pytest.mark.asyncio
    async def test_run_stream_without_system_prompt(self, executor, mock_hub):
        ctx = Context(user_input="hello")
        tokens = [t async for t in executor.run_stream(ctx)]
        assert tokens == ["mock ", "response"]
        client = mock_hub._clients["default"]
        assert client.last_messages == [
            {"role": "user", "content": "hello"},
        ]


# ── Test DefaultAgent ────────────────────────────────────────────


@pytest.fixture
def agent(monkeypatch):
    """返回一个使用 MockClient 的 DefaultAgent。"""
    _patch_hub(monkeypatch)

    return DefaultAgent(
        config={
            "default": {"protocol": "openai", "model": "gpt-4o", "api_key": "sk-xxx"},
        },
        system_prompt="You are a helper.",
    )


class TestDefaultAgent:
    @pytest.mark.asyncio
    async def test_chat_returns_result(self, agent):
        result = await agent.chat("hello")
        assert isinstance(result, AgentResult)
        assert result.content == "mock response for gpt-4o"

    @pytest.mark.asyncio
    async def test_chat_with_custom_model_name(self, agent):
        """未知模型名由 executor 捕获，返回错误文本而非抛出。"""
        result = await agent.chat("hello", model_name="nonexistent")
        assert "Executor 错误" in result.content

    @pytest.mark.asyncio
    async def test_chat_stream_yields_tokens(self, agent):
        tokens = [t async for t in agent.chat_stream("hello")]
        assert tokens == ["mock ", "response"]

    @pytest.mark.asyncio
    async def test_chat_stream_returns_string_when_joined(self, agent):
        tokens = [t async for t in agent.chat_stream("hello")]
        assert "".join(tokens) == "mock response"

    def test_parse_config(self):
        """_parse_config 已移除，config 直接赋值到 _configs。"""
        hub = ModelHub(config={
            "default": {"protocol": "openai", "model": "gpt-4o", "api_key": "sk-xxx"},
        })
        assert hub._config["default"]["model"] == "gpt-4o"

    def test_parse_config_empty(self):
        hub = ModelHub(config={})
        assert hub._config == {}

    def test_default_executor(self, monkeypatch):
        """不传 executor 时自动使用 ReActExecutor。"""
        _patch_hub(monkeypatch)

        agent = DefaultAgent(
            config={
                "default": {"protocol": "openai", "model": "gpt-4o", "api_key": "sk-xxx"},
            },
        )
        assert isinstance(agent.executor, ReActExecutor)

    def test_agent_requires_config(self):
        """config 是必传参数。"""
        with pytest.raises(TypeError):
            DefaultAgent()  # type: ignore


# ── Test Executor ABC ────────────────────────────────────────────


class TestExecutorABC:
    def test_abstract_class_cannot_be_instantiated(self):
        with pytest.raises(TypeError):
            Executor()  # type: ignore



# ═══════════════════════════════════════════════════════════════════
# Test ReActExecutor
# ═══════════════════════════════════════════════════════════════════


@pytest.fixture
def mock_hub_with_tools(monkeypatch):
    """用 MockClient 的 ModelHub，支持工具调用。"""
    _patch_hub(monkeypatch)

    return ModelHub(config={
        "default": {"protocol": "openai", "model": "gpt-4o", "api_key": "sk-xxx"},
    })


class TestReActExecutor:
    @pytest.mark.asyncio
    async def test_run_without_tools(self, mock_hub_with_tools):
        """无工具时直接返回文本。"""
        executor = ReActExecutor(brain=mock_hub_with_tools, memory=NullMemory())
        ctx = Context(user_input="hello", system_prompt="You are a bot.")
        result = await executor.run(ctx)
        assert result == "mock response for gpt-4o"
        assert ctx.step_count == 1

    @pytest.mark.asyncio
    async def test_run_with_tools(self, mock_hub_with_tools):
        """有工具调用时，应执行工具并继续循环。"""
        from agent.tools import Registry, FunctionTool

        registry = Registry()
        registry.add_tool(FunctionTool(
            name="test_tool",
            description="A test tool",
            fn=lambda: "tool result",
            input_schema={},
        ))

        # 设置 MockClient 第一阶段返回工具调用，第二阶段返回文本
        client = mock_hub_with_tools.get_default()
        client.chat = AsyncMock(side_effect=[
            make_ai(content="", tool_calls=[{"name": "test_tool", "args": {}, "id": "call_1"}]),
            make_ai(content="final answer", tool_calls=[]),
        ])

        executor = ReActExecutor(brain=mock_hub_with_tools, memory=NullMemory(), tools=registry, max_steps=3)
        ctx = Context(user_input="hello", system_prompt="You are a bot.")
        result = await executor.run(ctx)

        # 应执行工具并最终返回文本
        assert result == "final answer"
        assert ctx.step_count > 1

    @pytest.mark.asyncio
    async def test_run_with_tools_max_steps(self, mock_hub_with_tools):
        """超过 max_steps 应终止。"""
        from agent.tools import Registry, FunctionTool

        registry = Registry()
        registry.add_tool(FunctionTool(
            name="loop_tool",
            description="Always loops",
            fn=lambda: "still going",
            input_schema={},
        ))

        client = mock_hub_with_tools.get_default()
        client.chat = AsyncMock(return_value=make_ai(
            content="", tool_calls=[{"name": "loop_tool", "args": {}, "id": "c1"}],
        ))

        executor = ReActExecutor(brain=mock_hub_with_tools, memory=NullMemory(), tools=registry, max_steps=2)
        ctx = Context(user_input="loop", system_prompt="You are a bot.")

        await executor.run(ctx)

        # 达到 max_steps，step_count 反映循环次数
        assert ctx.step_count == 2

    @pytest.mark.asyncio
    async def test_run_stream(self, mock_hub_with_tools):
        """流式版本应最终返回结果。"""
        executor = ReActExecutor(brain=mock_hub_with_tools, memory=NullMemory())
        ctx = Context(user_input="hi", system_prompt="You are a bot.")
        tokens = [t async for t in executor.run_stream(ctx)]
        assert "mock" in "".join(tokens)

    @pytest.mark.asyncio
    async def test_run_calls_memory_hooks(self, mock_hub_with_tools):
        """验证 ReActExecutor 调用 memory.save(memory.load 已移至 DefaultAgent)。"""
        from agent.memory import MemoryModule

        memory = AsyncMock(spec=MemoryModule)
        memory.load = AsyncMock(return_value=None)
        memory.save = AsyncMock(return_value=None)
        executor = ReActExecutor(brain=mock_hub_with_tools, memory=memory)
        ctx = Context(user_input="hello", system_prompt="You are a bot.")

        await executor.run(ctx)

        memory.save.assert_awaited_once_with(ctx)

    @pytest.mark.asyncio
    async def test_build_messages_with_history(self):
        """验证 _messages_to_dicts 能正确转换 tool 消息。"""
        from agent.data import Message

        msgs = [
            Message(role="system", content="You are a bot."),
            Message(role="user", content="hello"),
            Message(role="assistant", content=None, tool_calls=[
                {"name": "test", "args": {}, "id": "call_1"},
            ]),
            Message(role="tool", tool_call_id="call_1", content="ok", name="test"),
        ]

        dicts = ReActExecutor._messages_to_dicts(msgs)
        assert len(dicts) == 4
        assert dicts[0] == {"role": "system", "content": "You are a bot."}
        assert dicts[1] == {"role": "user", "content": "hello"}
        # tool_calls 应被转为 API 格式
        assert dicts[2]["role"] == "assistant"
        assert dicts[2]["content"] == ""  # DeepSeek 兼容：content 不能为 None
        assert len(dicts[2]["tool_calls"]) == 1
        assert dicts[2]["tool_calls"][0]["function"]["name"] == "test"
        assert dicts[3] == {"role": "tool", "tool_call_id": "call_1", "content": "ok"}


# ── Test ReActExecutor _prepare + _build_llm_messages (Task 7) ────


@pytest.fixture
def mock_brain():
    """Mock ModelHub for executor tests.

    Self-referential: brain.get() returns brain itself, so brain.chat
    is the client chat method. Tests set chat.return_value / chat.side_effect.
    """
    brain = MagicMock()
    brain.get.return_value = brain
    brain.get_model_version.return_value = "test-model-v1"
    brain.chat = AsyncMock(return_value=make_ai(content="", tool_calls=[]))
    return brain


@pytest.fixture
def mock_tools():
    """Mock tool registry. execute() returns a string; get_schemas() returns None."""
    tools = MagicMock()
    tools.execute = AsyncMock(return_value="result text")
    tools.get_schemas.return_value = None
    return tools


@pytest.mark.asyncio
async def test_prepare_loads_memory_and_assembles_messages(mock_brain):
    memory = NullMemory()
    ex = ReActExecutor(brain=mock_brain, memory=memory)
    ctx = Context(user_input="hi", session_id="s1", system_prompt="sys")
    await ex._prepare(ctx)
    assert len(ctx.messages) == 1
    assert ctx.messages[0].role == "user"
    assert ctx.messages[0].content == "hi"


def test_build_llm_messages_order():
    ex = ReActExecutor(brain=None, memory=NullMemory())
    ctx = Context(system_prompt="S", user_input="U")
    ctx.memories = [
        MemoryBlock(title="知识库", content="K", source="knowledge", order=20),
        MemoryBlock(title="相关记忆", content="M", source="memory", order=10),
    ]
    ctx.messages = [Message(role="user", content="old"), Message(role="user", content="U")]
    msgs = ex._build_llm_messages(ctx)
    # system_prompt -> memories(order 10, 20) -> messages(跨轮累积 + 当前)
    assert msgs[0]["role"] == "system" and msgs[0]["content"] == "S"
    assert "M" in msgs[1]["content"]   # memory order=10 先
    assert "K" in msgs[2]["content"]   # knowledge order=20 后
    assert msgs[3]["content"] == "old"  # messages[0]
    assert msgs[4]["content"] == "U"    # messages[1]


# ── Test ReActExecutor _loop + _execute_tool (Task 8) ─────────────


@pytest.mark.asyncio
async def test_loop_single_text_response(mock_brain):
    mock_brain.chat.return_value = make_ai(content="answer", tool_calls=[])
    ex = ReActExecutor(brain=mock_brain, memory=NullMemory())
    ctx = Context(user_input="hi")
    await ex._prepare(ctx)
    await ex._loop(ctx)
    assert ctx.final_output == "answer"
    assert any(m.role == "assistant" for m in ctx.messages)


@pytest.mark.asyncio
async def test_loop_tool_then_answer(mock_brain, mock_tools):
    mock_brain.chat.side_effect = [
        make_ai(content="", tool_calls=[{"name": "search_news", "args": {}, "id": "c1"}]),
        make_ai(content="final", tool_calls=[]),
    ]
    mock_tools.execute.return_value = "result text"
    ex = ReActExecutor(brain=mock_brain, memory=NullMemory(), tools=mock_tools)
    ctx = Context(user_input="hi")
    await ex._prepare(ctx)
    await ex._loop(ctx)
    assert ctx.final_output == "final"
    tool_msgs = [m for m in ctx.messages if m.role == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0].tool_result.success is True
    assert tool_msgs[0].tool_result.result == "result text"


@pytest.mark.asyncio
async def test_loop_tool_failure_continues(mock_brain, mock_tools):
    mock_brain.chat.side_effect = [
        make_ai(content="", tool_calls=[{"name": "bad", "args": {}, "id": "c1"}]),
        make_ai(content="recovered", tool_calls=[]),
    ]
    mock_tools.execute.side_effect = RuntimeError("boom")
    ex = ReActExecutor(brain=mock_brain, memory=NullMemory(), tools=mock_tools, tool_max_retries=0)
    ctx = Context(user_input="hi")
    await ex._prepare(ctx)
    await ex._loop(ctx)
    assert ctx.final_output == "recovered"
    tool_msgs = [m for m in ctx.messages if m.role == "tool"]
    assert tool_msgs[0].tool_result.success is False
    assert "boom" in tool_msgs[0].tool_result.error


# ── Test ReActExecutor _finalize + run/run_stream (Task 9) ────────


def make_chat_stream(tokens):
    """Create a mock chat_stream async-generator factory.

    ``client.chat_stream(messages=..., tools=...)`` returns an async iterator
    yielding ``AIMessageChunk`` with the given token contents.
    """
    async def _stream(*args, **kwargs):
        for tok in tokens:
            yield AIMessageChunk(content=tok)
    return _stream


def make_chunk_stream(chunks):
    """Create a mock chat_stream yielding arbitrary pre-built chunks.

    用于模拟带 tool_call_chunks / response_metadata 的流式响应。
    """
    async def _stream(*args, **kwargs):
        for c in chunks:
            yield c
    return _stream


def tool_call_chunk(name="loop_tool", args="{}", call_id="c1", index=0):
    """Build a single tool_call_chunk fragment for streaming."""
    return AIMessageChunk(content="", tool_call_chunks=[{
        "name": name, "args": args, "id": call_id,
        "index": index, "type": "tool_call_chunk",
    }])


@pytest.fixture
def mock_memory():
    """Mock MemoryModule -- load/save are awaitable AsyncMocks."""
    memory = MagicMock(spec=MemoryModule)
    memory.load = AsyncMock(return_value=None)
    memory.save = AsyncMock(return_value=None)
    return memory


@pytest.mark.asyncio
async def test_run_normal(mock_brain):
    mock_brain.chat.return_value = make_ai(content="answer", tool_calls=[])
    ex = ReActExecutor(brain=mock_brain, memory=NullMemory())
    ctx = Context(user_input="hi")
    output = await ex.run(ctx)
    assert output == "answer"


@pytest.mark.asyncio
async def test_run_llm_failure_returns_error_text(mock_brain):
    mock_brain.chat.side_effect = RuntimeError("api down")
    ex = ReActExecutor(brain=mock_brain, memory=NullMemory(), llm_max_retries=0)
    ctx = Context(user_input="hi")
    output = await ex.run(ctx)
    assert "Executor 错误" in output
    assert "api down" in output


@pytest.mark.asyncio
async def test_run_calls_memory_save(mock_brain, mock_memory):
    mock_brain.chat.return_value = make_ai(content="a", tool_calls=[])
    ex = ReActExecutor(brain=mock_brain, memory=mock_memory)
    ctx = Context(user_input="hi", session_id="s1")
    await ex.run(ctx)
    mock_memory.save.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_stream_yields_tokens(mock_brain):
    mock_brain.chat.return_value = make_ai(content="hello world", tool_calls=[])
    mock_brain.chat_stream = make_chat_stream(["hello ", "world"])
    ex = ReActExecutor(brain=mock_brain, memory=NullMemory())
    ctx = Context(user_input="hi")
    tokens = [t async for t in ex.run_stream(ctx)]
    assert "".join(tokens).startswith("hello")


@pytest.mark.asyncio
async def test_run_stream_max_steps_yields_fallback(mock_brain, mock_tools):
    """max_steps 耗尽时 _loop_stream 应 yield fallback 文本(非空)。"""
    mock_brain.chat_stream = make_chunk_stream([tool_call_chunk()])
    ex = ReActExecutor(
        brain=mock_brain, memory=NullMemory(), tools=mock_tools, max_steps=2,
    )
    ctx = Context(user_input="hi")
    tokens = [t async for t in ex.run_stream(ctx)]
    assert tokens  # 非空 -- fallback 已 yield


@pytest.mark.asyncio
async def test_run_stream_truncated_yields_content(mock_brain, mock_tools):
    """finish_reason=length 截断时流出的 partial content 应保留(不丢)。"""
    chunks = [
        AIMessageChunk(content="partial "),
        AIMessageChunk(content="answer", tool_call_chunks=[{
            "name": "search_news", "args": "{}", "id": "c1",
            "index": 0, "type": "tool_call_chunk",
        }], response_metadata={"finish_reason": "length"}),
    ]
    mock_brain.chat_stream = make_chunk_stream(chunks)
    ex = ReActExecutor(brain=mock_brain, memory=NullMemory(), tools=mock_tools)
    ctx = Context(user_input="hi")
    tokens = [t async for t in ex.run_stream(ctx)]
    assert tokens  # 非空 -- 截断 content 已流出
    assert "partial" in "".join(tokens)


# ── 真流式 ReAct 循环(DSML 回归) ────────────────────────────────


class TestChunkText:
    """_chunk_text: 兼容 str 与 Anthropic content block 列表。"""

    def test_str_content(self):
        assert _chunk_text(AIMessageChunk(content="hello")) == "hello"

    def test_empty_content(self):
        assert _chunk_text(AIMessageChunk(content="")) == ""

    def test_block_list_skips_thinking_and_tool_use(self):
        chunk = AIMessageChunk(content=[
            {"type": "thinking", "thinking": "内部思考", "index": 0},
            {"type": "text", "text": "回答文本"},
            {"type": "tool_use", "id": "t1", "name": "f", "input": {}},
        ])
        assert _chunk_text(chunk) == "回答文本"

    def test_non_message_object(self):
        assert _chunk_text(object()) == ""


@pytest.mark.asyncio
async def test_run_stream_binds_tools_every_step(mock_brain, mock_tools):
    """DSML 回归:流式每一步都必须把 tool schemas 传给 chat_stream。

    请求缺 tools 时 DeepSeek 会把工具调用意图写成 DSML 标记纯文本
    (session 44 事故根因),故此处断言每次调用都带 schemas。
    """
    mock_tools.get_schemas.return_value = [{"type": "function", "function": {"name": "f"}}]
    calls: list[dict] = []

    async def _stream(*args, **kwargs):
        calls.append(kwargs)
        yield tool_call_chunk(name="loop_tool", args="{}")

    mock_brain.chat_stream = _stream
    ex = ReActExecutor(brain=mock_brain, memory=NullMemory(),
                       tools=mock_tools, max_steps=2)
    ctx = Context(user_input="hi")
    [t async for t in ex.run_stream(ctx)]
    assert len(calls) == 2
    assert all(c.get("tools") == mock_tools.get_schemas.return_value for c in calls)


@pytest.mark.asyncio
async def test_run_stream_tool_round_then_final_answer(mock_brain, mock_tools):
    """工具轮 -> 最终回答:chunk 聚合还原 tool_calls,消息结构与非流式一致。"""
    streams = [
        # 第一步:intro 文本 + 工具调用
        make_chunk_stream([
            AIMessageChunk(content="让我查一下。"),
            tool_call_chunk(name="test_tool", args='{"q": "热点"}', call_id="call_1"),
        ]),
        # 第二步:最终回答
        make_chat_stream(["最终", "回答"]),
    ]
    calls = iter(streams)
    mock_brain.chat_stream = lambda *a, **kw: next(calls)(*a, **kw)
    mock_tools.execute = AsyncMock(return_value="tool result")

    ex = ReActExecutor(brain=mock_brain, memory=NullMemory(), tools=mock_tools)
    ctx = Context(user_input="hi")
    tokens = [t async for t in ex.run_stream(ctx)]

    # 流式输出含 intro + 最终回答(全程流式,不再二次生成)
    assert "".join(tokens) == "让我查一下。最终回答"
    roles = [(m.role, bool(m.tool_calls)) for m in ctx.messages]
    assert roles == [
        ("user", False),
        ("assistant", True),   # intro + tool_calls
        ("tool", False),
        ("assistant", False),  # 最终回答
    ]
    assistant_with_calls = ctx.messages[1]
    assert assistant_with_calls.content == "让我查一下。"
    assert assistant_with_calls.tool_calls[0] == {
        "name": "test_tool", "args": {"q": "热点"}, "id": "call_1", "type": "tool_call",
    }
    mock_tools.execute.assert_awaited_once_with("test_tool", {"q": "热点"})
    assert ctx.final_output == "最终回答"


@pytest.mark.asyncio
async def test_run_stream_retries_clean_failure(mock_brain):
    """首个 chunk 前的流式失败可重试(重放不会重复输出)。"""
    attempts = {"n": 0}

    async def _stream(*args, **kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("connection reset")
        for tok in ["ok ", "answer"]:
            yield AIMessageChunk(content=tok)

    mock_brain.chat_stream = _stream
    ex = ReActExecutor(brain=mock_brain, memory=NullMemory(), llm_max_retries=1)
    ctx = Context(user_input="hi")
    tokens = [t async for t in ex.run_stream(ctx)]
    assert "".join(tokens) == "ok answer"
    assert attempts["n"] == 2


@pytest.mark.asyncio
async def test_run_stream_no_retry_after_partial_output(mock_brain):
    """已流出 token 后的失败不重试(避免重复输出),直接抛给 run_stream 兜底。"""
    async def _stream(*args, **kwargs):
        yield AIMessageChunk(content="partial ")
        raise RuntimeError("mid-stream failure")

    mock_brain.chat_stream = _stream
    ex = ReActExecutor(brain=mock_brain, memory=NullMemory(), llm_max_retries=2)
    ctx = Context(user_input="hi")
    tokens = [t async for t in ex.run_stream(ctx)]
    # run_stream 兜底:partial 已流出 + 错误文本
    assert "".join(tokens).startswith("partial ")
    assert "Executor 错误" in "".join(tokens)


@pytest.mark.asyncio
async def test_run_stream_empty_stream_finalizes(mock_brain):
    """流式响应完全为空(agg=None):按空最终回答收尾,不抛 AttributeError。"""
    async def _stream(*args, **kwargs):
        return
        yield  # pragma: no cover - 使其成为空 async generator

    mock_brain.chat_stream = _stream
    ex = ReActExecutor(brain=mock_brain, memory=NullMemory())
    ctx = Context(user_input="hi")
    tokens = [t async for t in ex.run_stream(ctx)]
    assert tokens == []
    assert ctx.messages[-1].role == "assistant"
    assert ctx.messages[-1].content is None


@pytest.mark.asyncio
async def test_run_stream_tool_cap_guidance_merged_into_head_system(mock_brain, mock_tools):
    """max_tool_rounds 打满后:摘 schemas + 引导并入头部 system 块。

    引导不能作为尾部 system 消息追加 -- langchain-anthropic 拒绝非连续
    system 消息(anthropic 协议请求直接 400)。且引导不得进 ctx.messages。
    """
    mock_tools.get_schemas.return_value = [{"type": "function", "function": {"name": "f"}}]
    seen_messages: list[list[dict]] = []

    async def _stream(*args, **kwargs):
        seen_messages.append(list(kwargs["messages"]))
        yield tool_call_chunk(name="loop_tool", args="{}")

    mock_brain.chat_stream = _stream
    mock_tools.execute = AsyncMock(return_value="ok")
    ex = ReActExecutor(brain=mock_brain, memory=NullMemory(),
                       tools=mock_tools, max_tool_rounds=1, max_steps=3)
    ctx = Context(user_input="hi", system_prompt="你是助手")
    [t async for t in ex.run_stream(ctx)]

    # 第二次调用起:无 tools(已摘)+ 引导并入首条 system
    assert seen_messages[0][0]["role"] == "system"
    assert "工具调用轮次已达上限" not in seen_messages[0][0]["content"]
    for msgs in seen_messages[1:]:
        assert "工具调用轮次已达上限" in msgs[0]["content"]
        # 关键回归:不得出现非连续/尾部 system 消息
        assert msgs[-1]["role"] != "system"
        assert [m["role"] for m in msgs].count("system") == 1
    # 引导不进对话历史
    assert all("工具调用轮次已达上限" not in (m.content or "")
               for m in ctx.messages)


@pytest.mark.asyncio
async def test_run_stream_cancel_during_tool_execution(mock_brain, mock_tools):
    """工具执行中用户 stop:为未回填的 tool_call 补中断占位,避免下轮 400。"""
    mock_tools.get_schemas.return_value = [{"type": "function", "function": {"name": "f"}}]

    async def _stream(*args, **kwargs):
        yield tool_call_chunk(name="slow_tool", args="{}", call_id="c1")

    async def _slow_execute(name, args):
        await asyncio.sleep(10)
        return "never"

    mock_brain.chat_stream = _stream
    mock_tools.execute = _slow_execute
    ex = ReActExecutor(brain=mock_brain, memory=NullMemory(), tools=mock_tools)
    ctx = Context(user_input="hi")

    gen = ex.run_stream(ctx)

    async def _consume():
        return [t async for t in gen]

    task = asyncio.create_task(_consume())
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    roles = [(m.role, m.tool_call_id) for m in ctx.messages]
    assert roles == [
        ("user", None),
        ("assistant", None),  # 含 tool_calls
        ("tool", "c1"),       # 中断占位
    ]
    assert "中断" in ctx.messages[-1].content


# ── Test DirectExecutor 单次 chat (Task 10) ──────────────────────


@pytest.mark.asyncio
async def test_direct_executor_single_chat(mock_brain):
    mock_brain.chat.return_value = make_ai(content="direct answer", tool_calls=[])
    ex = DirectExecutor(brain=mock_brain, memory=NullMemory())
    ctx = Context(user_input="hi", system_prompt="sys")
    output = await ex.run(ctx)
    assert output == "direct answer"
    # 无工具调用,单次
    mock_brain.chat.assert_awaited_once()


# ── Test DefaultAgent 跨轮复用 Context (Task 1) ─────────────────


@pytest.mark.asyncio
async def test_agent_reuses_ctx_across_turns(monkeypatch):
    """跨轮 chat 复用同一 Context,memory.load 只调一次,messages 累积 user。"""
    _patch_hub(monkeypatch)
    load_count = {"n": 0}

    class CountingMemory(NullMemory):
        async def load(self, ctx):
            load_count["n"] += 1

    agent = DefaultAgent(
        {"default": {"protocol": "openai", "model": "x", "api_key": "k"}},
        memory=CountingMemory(),
    )
    await agent.chat("第一轮", session_id="1")
    await agent.chat("第二轮", session_id="1")

    assert load_count["n"] == 1  # memory.load 只首次调一次
    assert agent._ctx is not None
    user_msgs = [m for m in agent._ctx.messages if m.role == "user"]
    assert len(user_msgs) == 2  # 两轮 user 累积


# ── Test DefaultAgent activate / freeze / get_conversation ────────


class _CapMemory(NullMemory):
    """记录 load/save 调用的 NullMemory。"""

    def __init__(self):
        self.loads = 0
        self.saves: list = []

    async def load(self, ctx):
        self.loads += 1

    async def save(self, ctx):
        self.saves.append(ctx)


def _make_agent(monkeypatch, memory=None) -> DefaultAgent:
    _patch_hub(monkeypatch)
    return DefaultAgent(
        {"default": {"protocol": "openai", "model": "x", "api_key": "k"}},
        memory=memory,
    )


@pytest.mark.asyncio
async def test_agent_activate_reloads_ctx(monkeypatch):
    """activate: 重置 ctx 并 memory.load 全量(切换回来时的手动 reload)。"""
    mem = _CapMemory()
    agent = _make_agent(monkeypatch, memory=mem)
    await agent.chat("第一轮", session_id="1")
    old_ctx = agent._ctx
    assert mem.loads == 1  # 首轮 chat 触发

    await agent.activate("1")
    assert agent._ctx is not old_ctx          # ctx 已重置
    assert mem.loads == 2                     # activate 主动 load
    assert agent._ctx.session_id == "1"

    # activate 后继续 chat 复用新 ctx,不再 load
    await agent.chat("第二轮", session_id="1")
    assert mem.loads == 2
    user_msgs = [m for m in agent._ctx.messages if m.role == "user"]
    assert len(user_msgs) == 1  # 新 ctx 从第二轮 user 开始累积


@pytest.mark.asyncio
async def test_agent_activate_without_session_id_resets_only(monkeypatch):
    """无 session_id 且无既有 ctx 时仅重置 ctx(下次 chat 懒加载)。"""
    mem = _CapMemory()
    agent = _make_agent(monkeypatch, memory=mem)
    agent._ctx = Context(user_input="x", session_id="7")
    await agent.activate()  # 复用既有 ctx 的 session_id
    assert agent._ctx.session_id == "7"
    assert mem.loads == 1

    agent2 = _make_agent(monkeypatch, memory=_CapMemory())
    await agent2.activate("")  # 无 sid 无 ctx
    assert agent2._ctx is None  # 仅重置,懒加载


@pytest.mark.asyncio
async def test_agent_freeze_delegates_to_memory(monkeypatch):
    """freeze: ctx 存在时调 memory.save;无 ctx 跳过。"""
    mem = _CapMemory()
    agent = _make_agent(monkeypatch, memory=mem)
    await agent.freeze()           # 无 ctx:跳过
    assert mem.saves == []
    await agent.chat("hi", session_id="1")
    assert len(mem.saves) == 1     # _finalize 已 save 一次
    await agent.freeze()           # freeze 委托 memory.save
    assert len(mem.saves) == 2
    assert mem.saves[-1] is agent._ctx


@pytest.mark.asyncio
async def test_agent_freeze_degrades_on_memory_error(monkeypatch):
    """freeze 的 memory.save 抛异常时不向调用方传播(降级 log)。"""
    class BoomMemory(NullMemory):
        async def save(self, ctx):
            raise RuntimeError("db down")

    agent = _make_agent(monkeypatch, memory=BoomMemory())
    await agent.chat("hi", session_id="1")  # _finalize 同样降级
    await agent.freeze()                    # 不抛
    assert agent._ctx is not None


@pytest.mark.asyncio
async def test_agent_get_conversation_projection(monkeypatch):
    """get_conversation: 有序 [{role,content}],滤掉 tool 与纯 tool_call assistant。"""
    agent = _make_agent(monkeypatch)
    assert agent.get_conversation() == []   # 无 ctx

    await agent.chat("你好", session_id="1")
    conv = agent.get_conversation()
    assert conv == [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "mock response for gpt-4o"},
    ]

    # 构造含工具中间步的 messages,验证投影过滤且保序
    agent._ctx.messages.insert(0, Message(
        role="tool", tool_call_id="c1", content="res", name="search_news",
    ))
    agent._ctx.messages.insert(0, Message(
        role="assistant", content=None,
        tool_calls=[{"name": "search_news", "args": {}, "id": "c1"}],
    ))
    conv = agent.get_conversation()
    assert all(m["role"] in ("user", "assistant") for m in conv)
    assert len(conv) == 2


@pytest.mark.asyncio
async def test_run_stream_cancel_appends_partial_assistant(mock_brain):
    """cancel 中断流式:已流出 partial 先 append 为 assistant,_finalize 落库不丢。"""
    mock_brain.chat.return_value = make_ai(content="", tool_calls=[])

    async def _stream(*args, **kwargs):
        yield AIMessageChunk(content="par")
        await asyncio.sleep(10)          # 挂起 -> cancel 落点
        yield AIMessageChunk(content="tial")

    mock_brain.chat_stream = _stream

    saved: list[str] = []

    class CapSaveMemory(NullMemory):
        async def save(self, ctx):
            saved.append(ctx.final_output)

    ex = ReActExecutor(brain=mock_brain, memory=CapSaveMemory())
    ctx = Context(user_input="hi", session_id="1")

    async def consume():
        async for _ in ex.run_stream(ctx):
            pass

    t = asyncio.create_task(consume())
    await asyncio.sleep(0.05)             # 消费 "par" 后挂在 sleep
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t

    assert ctx.final_output == "par"     # partial 已入 messages
    assert saved == ["par"]              # _finalize 存的是 partial