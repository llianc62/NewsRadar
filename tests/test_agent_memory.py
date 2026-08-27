"""Tests for agent/memory.py - MemoryModule hierarchy."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.data import Context
from agent.memory import LongTermMemory, MemoryModule, NullMemory, ShortTermMemory


# ── Fixtures ──────────────────────────────────────────────────


@pytest.fixture
def mock_db():
    """Mock db with get_agent_messages / save_agent_message."""
    db = MagicMock()
    db.get_agent_messages.return_value = []
    return db


@pytest.fixture
def mock_mem_storage():
    """Mock MemoryStorage with async search/save."""
    storage = MagicMock()
    storage.search = AsyncMock(return_value=[])
    storage.save = AsyncMock(return_value=None)
    return storage


# ── Tests ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_null_memory_noop():
    ctx = Context(user_input="hi", session_id="1")
    m = NullMemory()
    await m.load(ctx)
    assert ctx.messages == []
    await m.save(ctx)  # 不抛


@pytest.mark.asyncio
async def test_short_term_load_from_db(mock_db):
    mock_db.get_agent_messages.return_value = [
        {"role": "user", "content": "old q"},
        {"role": "assistant", "content": "old a"},
    ]
    m = ShortTermMemory(mock_db, window_size=20)
    ctx = Context(session_id="1")
    await m.load(ctx)
    assert len(ctx.messages) == 2
    assert ctx.messages[0].role == "user"
    assert ctx.messages[0].content == "old q"


@pytest.mark.asyncio
async def test_short_term_load_no_session():
    m = ShortTermMemory(db=None)
    ctx = Context(session_id="")
    await m.load(ctx)
    assert ctx.messages == []


@pytest.mark.asyncio
async def test_short_term_save(mock_db):
    """save 只存 assistant(user 由接口层落库),返回新存条数。"""
    from agent.data import Message

    m = ShortTermMemory(mock_db)
    ctx = Context(session_id="1", user_input="q")
    ctx.messages = [Message(role="assistant", content="a")]
    n = await m.save(ctx)
    assert n == 1
    mock_db.save_agent_message.assert_called_once_with(1, "assistant", "a")


@pytest.mark.asyncio
async def test_short_term_save_incremental_idempotent(mock_db):
    """增量水位:重复 save 无 delta 即 no-op;新消息追加后只写新增 assistant。

    水位是 memory 的私有实例状态(load 建立基线、save 逐条推进),
    freeze 与 _finalize 任意重复调用不重复 INSERT。
    """
    from agent.data import Message

    m = ShortTermMemory(mock_db)
    ctx = Context(session_id="1", user_input="q")
    ctx.messages = [Message(role="user", content="q"),
                    Message(role="assistant", content="a1")]
    n = await m.save(ctx)            # _finalize:写 a1(跳过 user)
    assert n == 1
    assert await m.save(ctx) == 0    # freeze 重调:无 delta,no-op
    mock_db.save_agent_message.assert_called_once_with(1, "assistant", "a1")

    # 新一轮消息追加后,只写新增 assistant(跳过 user/tool)
    ctx.messages += [
        Message(role="user", content="q2"),
        Message(role="tool", tool_call_id="c1", content="res", name="t"),
        Message(role="assistant", content="a2"),
    ]
    assert await m.save(ctx) == 1
    assert mock_db.save_agent_message.call_count == 2
    mock_db.save_agent_message.assert_called_with(1, "assistant", "a2")


@pytest.mark.asyncio
async def test_short_term_load_initializes_watermark(mock_db):
    """load 建立水位基线:已加载的历史不被 save 重复写回。"""
    mock_db.get_agent_messages.return_value = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "a"},
    ]
    m = ShortTermMemory(mock_db)
    ctx = Context(session_id="1")
    await m.load(ctx)
    assert m._saved_up_to == 2           # 已加载 = 已在库
    assert await m.save(ctx) == 0
    mock_db.save_agent_message.assert_not_called()


@pytest.mark.asyncio
async def test_short_term_save_partial_failure_retries_remaining(mock_db):
    """多条增量中一条失败:水位停在失败处,重试只补剩余。"""
    from agent.data import Message

    calls = {"n": 0}

    def flaky(sid, role, content, agent_id="0", model_version=""):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("db hiccup")

    mock_db.save_agent_message.side_effect = flaky
    m = ShortTermMemory(mock_db)
    ctx = Context(session_id="1")
    ctx.messages = [
        Message(role="assistant", content="a1"),
        Message(role="assistant", content="a2"),
        Message(role="assistant", content="a3"),
    ]
    with pytest.raises(RuntimeError):
        await m.save(ctx)
    assert m._saved_up_to == 1            # a1 已写,水位停在 a2
    assert await m.save(ctx) == 2         # 重试只补 a2/a3
    assert mock_db.save_agent_message.call_count == 4


@pytest.mark.asyncio
async def test_short_term_save_skips_empty_assistant(mock_db):
    """final_output 为空(如异常路径)时跳过,不触发 DB 非空校验。"""
    m = ShortTermMemory(mock_db)
    ctx = Context(session_id="1", user_input="q")
    n = await m.save(ctx)
    assert n == 0
    mock_db.save_agent_message.assert_not_called()


# ── session_id 类型边界（regression: 会话 title 不更新） ─────────


@pytest.mark.asyncio
async def test_short_term_save_converts_str_session_id_to_int(mock_db):
    """str session_id（来自 WebSocket/Context）应转为 int 传给 DB。

    Regression: ``Context.session_id`` 是 str（WebSocket 调 ``chat_stream``
    时传 ``session_id=str(...)``），但 ``save_agent_message`` 期望 int。
    未转换时 ``session_id < 1`` 对 str 抛 TypeError，被
    ``executor._finalize`` 的 try/except 吞掉，导致 user 消息未落库、
    会话 title 永远停留在"新会话"。
    """
    from agent.data import Message

    m = ShortTermMemory(mock_db)
    ctx = Context(session_id="5", user_input="你好")
    ctx.messages = [Message(role="assistant", content="回复")]
    await m.save(ctx)
    assert mock_db.save_agent_message.call_count == 1  # 仅 assistant
    for call in mock_db.save_agent_message.call_args_list:
        sid = call.args[0]
        assert sid == 5
        assert isinstance(sid, int), f"session_id 应为 int，实际 {type(sid).__name__}"


@pytest.mark.asyncio
async def test_short_term_load_converts_str_session_id_to_int(mock_db):
    """load 同样需把 str session_id 转为 int 传给 get_agent_messages。"""
    mock_db.get_agent_messages.return_value = []
    m = ShortTermMemory(mock_db)
    ctx = Context(session_id="5")
    await m.load(ctx)
    mock_db.get_agent_messages.assert_called_once_with(5, 20)
    sid = mock_db.get_agent_messages.call_args.args[0]
    assert isinstance(sid, int)


@pytest.mark.asyncio
async def test_short_term_save_skips_non_numeric_session_id(mock_db):
    """非数字 session_id（无法对应 PG SERIAL）时静默降级，不调 DB、不抛。"""
    from agent.data import Message

    m = ShortTermMemory(mock_db)
    ctx = Context(session_id="abc", user_input="你好")
    ctx.messages = [Message(role="assistant", content="回复")]
    await m.save(ctx)  # 不抛
    mock_db.save_agent_message.assert_not_called()


@pytest.mark.asyncio
async def test_short_term_load_skips_non_numeric_session_id(mock_db):
    """非数字 session_id 时 load 静默降级，不调 DB、不抛。"""
    m = ShortTermMemory(mock_db)
    ctx = Context(session_id="abc")
    await m.load(ctx)  # 不抛
    mock_db.get_agent_messages.assert_not_called()


# ── LongTermMemory tests ──────────────────────────────────────


@pytest.mark.asyncio
async def test_long_term_load_history_and_memories(mock_db, mock_mem_storage):
    mock_db.get_agent_messages.return_value = [{"role": "user", "content": "q"}]
    mock_mem_storage.search.return_value = [{"memory_type": "fact", "content": "remembered"}]
    m = LongTermMemory(mock_db, mock_mem_storage)
    ctx = Context(session_id="1", user_input="hi")
    await m.load(ctx)
    assert len(ctx.messages) == 1
    assert len(ctx.memories) == 1
    assert ctx.memories[0].source == "memory"
    assert ctx.memories[0].order == 10


@pytest.mark.asyncio
async def test_long_term_load_no_user_input(mock_db, mock_mem_storage):
    mock_db.get_agent_messages.return_value = []
    m = LongTermMemory(mock_db, mock_mem_storage)
    ctx = Context(session_id="1", user_input="")
    await m.load(ctx)
    assert ctx.memories == []


# ── LongTermMemory.save extraction path (C-1 regression) ──────


@pytest.mark.asyncio
async def test_long_term_save_extracts_on_long_output(mock_db, mock_mem_storage):
    """save() with long assistant output triggers _extract_and_store (fact).

    Regression: ctx.assistant_output was deleted (Task 2), replaced by
    ctx.final_output property.  _should_extract / _extract_and_store /
    _batch_merge must use final_output, not assistant_output.
    """
    from agent.data import Message

    m = LongTermMemory(mock_db, mock_mem_storage)
    ctx = Context(session_id="1", user_input="普通问题")
    ctx.messages = [
        Message(role="assistant", content="A" * 101),  # >100 chars -> _should_extract True
    ]
    await m.save(ctx)
    # _extract_and_store called -> mem_storage.save with memory_type="fact"
    mock_mem_storage.save.assert_awaited_once()
    args = mock_mem_storage.save.call_args
    assert args.kwargs.get("memory_type") == "fact"
    assert args.kwargs.get("session_id") == "1"


@pytest.mark.asyncio
async def test_long_term_save_extracts_on_long_output_no_entity(mock_db, mock_mem_storage):
    """save() with long output triggers extraction even without named entities."""
    from agent.data import Message

    m = LongTermMemory(mock_db, mock_mem_storage)
    ctx = Context(session_id="1", user_input="普通问题没有命名实体")
    ctx.messages = [Message(role="assistant", content="X" * 101)]  # >100 chars
    await m.save(ctx)
    mock_mem_storage.save.assert_awaited_once()
    assert mock_mem_storage.save.call_args.kwargs.get("memory_type") == "fact"


@pytest.mark.asyncio
async def test_long_term_save_batch_merge_on_interval(mock_db, mock_mem_storage):
    """save() on extract_interval turn without extraction triggers _batch_merge (summary).

    每轮模拟需追加新消息(增量水位按消息推进),同一批消息重复 save
    第二次会因无 delta 跳过。
    """
    from agent.data import Message

    m = LongTermMemory(mock_db, mock_mem_storage, extract_interval=2)
    ctx = Context(session_id="1", user_input="普通文本无实体")
    ctx.messages = [Message(role="assistant", content="短回复")]

    # Turn 1: no extraction (short output, no entity), not interval -> no save
    await m.save(ctx)
    mock_mem_storage.save.assert_not_awaited()

    # Turn 2: append new turn messages, no extraction but _turn_count % 2 == 0 -> _batch_merge
    ctx.messages += [Message(role="user", content="q2"),
                     Message(role="assistant", content="短回复2")]
    await m.save(ctx)
    mock_mem_storage.save.assert_awaited_once()
    assert mock_mem_storage.save.call_args.kwargs.get("memory_type") == "summary"


@pytest.mark.asyncio
async def test_long_term_save_no_reextract_when_already_saved(mock_db, mock_mem_storage):
    """freeze 重调(本轮已存)不重复落库、不重复提炼长期记忆。"""
    from agent.data import Message

    m = LongTermMemory(mock_db, mock_mem_storage)
    ctx = Context(session_id="1", user_input="普通问题")
    ctx.messages = [Message(role="assistant", content="A" * 150)]
    await m.save(ctx)   # _finalize: 存 assistant + 提炼
    await m.save(ctx)   # freeze 重调: 幂等跳过,不重复提炼
    assert mock_db.save_agent_message.call_count == 1
    assert mock_mem_storage.save.await_count == 1


@pytest.mark.asyncio
async def test_long_term_save_no_attribute_error(mock_db, mock_mem_storage):
    """Regression: save() must not raise AttributeError for ctx.assistant_output.

    ctx.final_output is a property reading ctx.messages; if _should_extract /
    _extract_and_store / _batch_merge referenced the deleted assistant_output
    field, the error would be swallowed by executor._finalize try/except,
    silently breaking long-term memory extraction.
    """
    from agent.data import Message

    m = LongTermMemory(mock_db, mock_mem_storage)
    ctx = Context(session_id="1", user_input="巴菲特投资苹果")
    ctx.messages = [Message(role="assistant", content="B" * 150)]
    # Must not raise AttributeError
    await m.save(ctx)
    assert not hasattr(ctx, "assistant_output")  # field deleted (spec 2.2)
