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


class _FakeExecutor:
    """假 executor：仅承载 _approval_callback（_start_chat 绑定用）。"""
    _approval_callback = None


class _FakeAgent:
    """假 agent：按预设 token 列表流式产出。"""
    def __init__(self, tokens):
        self._tokens = tokens
        self.executor = _FakeExecutor()
        self.running_mode = "normal"

    async def chat_stream(self, message, session_id="", model_name=""):
        for t in self._tokens:
            yield t


@pytest.mark.asyncio
async def test_run_chat_broadcasts_tokens_and_done(monkeypatch):
    monkeypatch.setattr("web.agent._sessions", {})
    sess = ChatSession(session_id=1)
    ct = ChatTask(session_id=1)
    q = ct.subscribe()
    agent = _FakeAgent(["Hello", " World"])
    from web.agent import _run_chat
    await _run_chat(sess, ct, agent, "hi", 1, "quick")
    items = []
    while not q.empty():
        items.append(q.get_nowait())
    assert {"type": "token", "content": "Hello"} in items
    assert {"type": "token", "content": " World"} in items
    assert any(it.get("type") == "done" for it in items)
    assert ct.full_reply == "Hello World"
    assert ct.done is True


@pytest.mark.asyncio
async def test_run_chat_on_error_broadcasts_error(monkeypatch):
    monkeypatch.setattr("web.agent._sessions", {})
    sess = ChatSession(session_id=1)
    ct = ChatTask(session_id=1)
    q = ct.subscribe()

    class _BoomAgent:
        async def chat_stream(self, message, session_id="", model_name=""):
            raise RuntimeError("boom")
            yield  # noqa: never reached

    from web.agent import _run_chat
    await _run_chat(sess, ct, _BoomAgent(), "hi", 1, "quick")
    items = []
    while not q.empty():
        items.append(q.get_nowait())
    assert any(it.get("type") == "error" for it in items)
    assert "boom" in ct.error
    assert ct.done is True


class _FakeWS:
    """假 WebSocket：收集 send_json 的消息。"""
    def __init__(self):
        self.sent = []

    async def send_json(self, item):
        self.sent.append(item)


@pytest.mark.asyncio
async def test_forward_replays_full_reply_then_live_tokens(monkeypatch):
    """重连场景：ct 已累积 full_reply，_forward 应补发 resume + 后续 token + done。"""
    monkeypatch.setattr("web.agent._sessions", {})
    ct = ChatTask(session_id=1)
    ct.full_reply = "already"  # 模拟跳转期间已生成的内容
    q = ct.subscribe()
    # 模拟后台继续产出
    await asyncio.sleep(0)  # 让 subscribe 生效
    ws = _FakeWS()

    async def produce():
        await asyncio.sleep(0.01)
        ct.full_reply += "!"
        ct.broadcast({"type": "token", "content": "!"})
        ct.done = True
        ct.broadcast({"type": "done", "session_id": 1, "full_reply": ct.full_reply})

    asyncio.create_task(produce())
    from web.agent import _forward
    await _forward(ws, ct, 1)
    types = [m["type"] for m in ws.sent]
    assert types[0] == "resume"
    assert ws.sent[0]["full_reply"] == "already"  # subscribe 时刻快照
    assert "token" in types and "done" in types
    assert ws.sent[-1]["type"] == "done"


@pytest.mark.asyncio
async def test_forward_done_task_sends_resume_and_done(monkeypatch):
    """任务已完成的重连：一次性补发 resume + done。"""
    monkeypatch.setattr("web.agent._sessions", {})
    ct = ChatTask(session_id=1)
    ct.full_reply = "complete answer"
    ct.done = True
    ws = _FakeWS()
    from web.agent import _forward
    await _forward(ws, ct, 1)
    assert ws.sent[0] == {"type": "resume", "full_reply": "complete answer"}
    assert ws.sent[1]["type"] == "done"


class _FakeAppState:
    """模拟 app.state，持有 agent_instance / agent_factory / db。"""
    def __init__(self, agent_instance=None, agent_factory=None, db=None):
        self.agent_instance = agent_instance
        self.agent_factory = agent_factory
        self.db = db


@pytest.mark.asyncio
async def test_start_chat_uses_default_agent_and_caches(monkeypatch):
    from web.agent import _start_chat
    monkeypatch.setattr("web.agent._sessions", {})
    sess = get_session(1)
    fake_agent = _FakeAgent(["hi"])
    state = _FakeAppState(agent_instance=fake_agent)
    ct = await _start_chat(
        sess, "hello", session_id=1, agent_id="",
        model_name="quick", running_mode="normal", app_state=state,
    )
    # 第二次取 default 应命中缓存（agent_instance 同一对象）
    assert sess.agents["default"] is fake_agent
    assert ct.task is not None
    await ct.task  # 跑完
    assert ct.full_reply == "hi"
    assert ct.done is True


@pytest.mark.asyncio
async def test_start_chat_agent_id_builds_via_factory(monkeypatch):
    from web.agent import _start_chat
    monkeypatch.setattr("web.agent._sessions", {})
    sess = get_session(2)
    fake_agent = _FakeAgent(["x"])
    builds = {"n": 0}

    class _FakeFactory:
        def build(self, defn):
            builds["n"] += 1
            return fake_agent

    class _FakeDefn:
        pass

    class _FakeDB:
        pass

    state = _FakeAppState(agent_factory=_FakeFactory(), db=_FakeDB())
    # mock db.get_agent_definition 返回非 None
    state.db.get_agent_definition = lambda aid: _FakeDefn()
    ct = await _start_chat(
        sess, "hi", session_id=2, agent_id="abc-123",
        model_name="quick", running_mode="normal", app_state=state,
    )
    await ct.task
    assert builds["n"] == 1
    assert sess.agents["agent:abc-123"] is fake_agent
    # 再次 start_chat 同 agent_id 命中缓存（不重复 build）
    ct2 = await _start_chat(
        sess, "hi2", session_id=2, agent_id="abc-123",
        model_name="quick", running_mode="normal", app_state=state,
    )
    await ct2.task
    assert builds["n"] == 1


@pytest.mark.asyncio
async def test_start_chat_reuses_running_task(monkeypatch):
    """防重入：已有进行中任务则返回它，不启新任务。"""
    from web.agent import _start_chat
    monkeypatch.setattr("web.agent._sessions", {})
    sess = get_session(3)
    fake_agent = _FakeAgent(["a", "b", "c"])
    state = _FakeAppState(agent_instance=fake_agent)
    ct1 = await _start_chat(
        sess, "hi", session_id=3, agent_id="",
        model_name="quick", running_mode="normal", app_state=state,
    )
    ct2 = await _start_chat(
        sess, "hi2", session_id=3, agent_id="",
        model_name="quick", running_mode="normal", app_state=state,
    )
    assert ct1 is ct2  # 同一任务
