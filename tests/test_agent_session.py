"""ChatTask / ChatSession / session 表单元测试。"""
import asyncio

import pytest

from web.agent import (
    ChatSession,
    ChatTask,
    _sessions,
    destroy_session,
    get_session,
)


@pytest.mark.asyncio
async def test_chat_task_subscribe_and_broadcast():
    ct = ChatTask(session_id=1)
    q = ct.subscribe()
    ct.broadcast({"type": "token", "content": "hello"})
    item = await asyncio.wait_for(q.get(), timeout=1.0)
    assert item == {"type": "token", "content": "hello"}


@pytest.mark.asyncio
async def test_chat_task_unsubscribe_stops_receiving():
    ct = ChatTask(session_id=1)
    q = ct.subscribe()
    ct.unsubscribe(q)
    ct.broadcast({"type": "token", "content": "x"})
    assert q.empty()


@pytest.mark.asyncio
async def test_chat_task_broadcast_to_multiple_subscribers():
    ct = ChatTask(session_id=1)
    q1, q2 = ct.subscribe(), ct.subscribe()
    ct.broadcast({"type": "done"})
    assert await q1.get() == {"type": "done"}
    assert await q2.get() == {"type": "done"}


@pytest.mark.asyncio
async def test_chat_task_respond_approval_resolves_future():
    ct = ChatTask(session_id=1)
    future = asyncio.get_event_loop().create_future()
    ct.pending_approvals["req1"] = future
    ct.respond_approval("req1", True, "ok")
    result = await future
    assert result == {"approved": True, "reason": "ok"}
    assert "req1" not in ct.pending_approvals


@pytest.mark.asyncio
async def test_chat_task_request_approval_broadcasts_and_resolves():
    ct = ChatTask(session_id=1)
    q = ct.subscribe()
    # 在另一个任务里响应审批
    async def responder():
        item = await q.get()
        assert item["type"] == "tool_approval_request"
        ct.respond_approval(item["request_id"], True, "approved")
    asyncio.create_task(responder())
    result = await ct.request_approval({"name": "search_news"}, {"q": "test"})
    assert result == {"approved": True, "reason": "approved"}


def test_chat_session_get_agent_builds_once(monkeypatch):
    monkeypatch.setattr("web.agent._sessions", {})
    sess = ChatSession(session_id=1)
    calls = {"n": 0}

    def build():
        calls["n"] += 1
        return "agent_obj"

    a1 = sess.get_agent("default", build)
    a2 = sess.get_agent("default", build)
    assert a1 == "agent_obj" and a2 == "agent_obj"
    assert calls["n"] == 1  # 只构建一次


def test_chat_session_get_agent_multiple_keys(monkeypatch):
    monkeypatch.setattr("web.agent._sessions", {})
    sess = ChatSession(session_id=1)
    a1 = sess.get_agent("agent:x", lambda: "x")
    a2 = sess.get_agent("agent:y", lambda: "y")
    assert a1 == "x" and a2 == "y"


def test_get_session_creates_and_caches(monkeypatch):
    monkeypatch.setattr("web.agent._sessions", {})
    s1 = get_session(1)
    s2 = get_session(1)
    assert s1 is s2
    assert s1.session_id == 1


@pytest.mark.asyncio
async def test_destroy_session_cancels_running_task(monkeypatch):
    monkeypatch.setattr("web.agent._sessions", {})
    sess = get_session(5)
    ct = ChatTask(session_id=5)
    ct.task = asyncio.create_task(asyncio.sleep(100))
    sess.chat_task = ct
    destroy_session(5)
    assert 5 not in _sessions
    await asyncio.sleep(0.01)
    assert ct.task.done() or ct.task.cancelled()


def test_parse_int_sid():
    from web.agent import _parse_int_sid
    assert _parse_int_sid("42") == 42
    assert _parse_int_sid("abc") is None
    assert _parse_int_sid("0") is None
    assert _parse_int_sid("") is None
