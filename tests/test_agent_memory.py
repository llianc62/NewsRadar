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
    ctx = Context(user_input="hi", session_id="s1")
    m = NullMemory()
    await m.load(ctx)
    assert ctx.history_messages == []
    await m.save(ctx)  # 不抛


@pytest.mark.asyncio
async def test_short_term_load_from_db(mock_db):
    mock_db.get_agent_messages.return_value = [
        {"role": "user", "content": "old q"},
        {"role": "assistant", "content": "old a"},
    ]
    m = ShortTermMemory(mock_db, window_size=20)
    ctx = Context(session_id="s1")
    await m.load(ctx)
    assert len(ctx.history_messages) == 2
    assert ctx.history_messages[0].role == "user"
    assert ctx.history_messages[0].content == "old q"


@pytest.mark.asyncio
async def test_short_term_load_no_session():
    m = ShortTermMemory(db=None)
    ctx = Context(session_id="")
    await m.load(ctx)
    assert ctx.history_messages == []


@pytest.mark.asyncio
async def test_short_term_save(mock_db):
    m = ShortTermMemory(mock_db)
    ctx = Context(session_id="s1", user_input="q")
    ctx.messages = [__import__("agent.data", fromlist=["Message"]).Message(role="assistant", content="a")]
    await m.save(ctx)
    assert mock_db.save_agent_message.call_count == 2  # user + assistant


# ── LongTermMemory tests ──────────────────────────────────────


@pytest.mark.asyncio
async def test_long_term_load_history_and_memories(mock_db, mock_mem_storage):
    mock_db.get_agent_messages.return_value = [{"role": "user", "content": "q"}]
    mock_mem_storage.search.return_value = [{"memory_type": "fact", "content": "remembered"}]
    m = LongTermMemory(mock_db, mock_mem_storage)
    ctx = Context(session_id="s1", user_input="hi")
    await m.load(ctx)
    assert len(ctx.history_messages) == 1
    assert len(ctx.memories) == 1
    assert ctx.memories[0].source == "memory"
    assert ctx.memories[0].order == 10


@pytest.mark.asyncio
async def test_long_term_load_no_user_input(mock_db, mock_mem_storage):
    mock_db.get_agent_messages.return_value = []
    m = LongTermMemory(mock_db, mock_mem_storage)
    ctx = Context(session_id="s1", user_input="")
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
    ctx = Context(session_id="s1", user_input="普通问题")
    ctx.messages = [
        Message(role="assistant", content="A" * 101),  # >100 chars -> _should_extract True
    ]
    await m.save(ctx)
    # _extract_and_store called -> mem_storage.save with memory_type="fact"
    mock_mem_storage.save.assert_awaited_once()
    args = mock_mem_storage.save.call_args
    assert args.kwargs.get("memory_type") == "fact"
    assert args.kwargs.get("session_id") == "s1"


@pytest.mark.asyncio
async def test_long_term_save_extracts_on_long_output_no_entity(mock_db, mock_mem_storage):
    """save() with long output triggers extraction even without named entities."""
    from agent.data import Message

    m = LongTermMemory(mock_db, mock_mem_storage)
    ctx = Context(session_id="s1", user_input="普通问题没有命名实体")
    ctx.messages = [Message(role="assistant", content="X" * 101)]  # >100 chars
    await m.save(ctx)
    mock_mem_storage.save.assert_awaited_once()
    assert mock_mem_storage.save.call_args.kwargs.get("memory_type") == "fact"


@pytest.mark.asyncio
async def test_long_term_save_batch_merge_on_interval(mock_db, mock_mem_storage):
    """save() on extract_interval turn without extraction triggers _batch_merge (summary)."""
    from agent.data import Message

    m = LongTermMemory(mock_db, mock_mem_storage, extract_interval=2)
    ctx = Context(session_id="s1", user_input="普通文本无实体")
    ctx.messages = [Message(role="assistant", content="短回复")]

    # Turn 1: no extraction (short output, no entity), not interval -> no save
    await m.save(ctx)
    mock_mem_storage.save.assert_not_awaited()

    # Turn 2: no extraction but _turn_count % 2 == 0 -> _batch_merge
    await m.save(ctx)
    mock_mem_storage.save.assert_awaited_once()
    assert mock_mem_storage.save.call_args.kwargs.get("memory_type") == "summary"


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
    ctx = Context(session_id="s1", user_input="巴菲特投资苹果")
    ctx.messages = [Message(role="assistant", content="B" * 150)]
    # Must not raise AttributeError
    await m.save(ctx)
    assert not hasattr(ctx, "assistant_output")  # field deleted (spec 2.2)
