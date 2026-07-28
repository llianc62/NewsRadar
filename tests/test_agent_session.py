"""ChatTask / ChatSession / session 表单元测试。"""
import asyncio

import pytest

from web.agent import ChatTask


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
