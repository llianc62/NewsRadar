"""Tests for agent/memory.py - MemoryModule hierarchy."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from agent.data import Context
from agent.memory import MemoryModule, NullMemory, ShortTermMemory


# ── Fixtures ──────────────────────────────────────────────────


@pytest.fixture
def mock_db():
    """Mock db with get_agent_messages / save_agent_message."""
    db = MagicMock()
    db.get_agent_messages.return_value = []
    return db


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
