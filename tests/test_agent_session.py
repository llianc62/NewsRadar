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
    """模拟 app.state，持有 agent_config / db / base_prompt。"""
    def __init__(self, agent_instance=None, agent_factory=None, db=None,
                 agent_config=None, base_prompt=""):
        self.agent_instance = agent_instance
        self.agent_factory = agent_factory
        self.db = db
        self.agent_config = agent_config or {}
        self.base_prompt = base_prompt


@pytest.mark.asyncio
async def test_start_chat_uses_default_agent_and_caches(monkeypatch):
    from web.agent import _start_chat
    monkeypatch.setattr("web.agent._sessions", {})
    sess = get_session(1)
    fake_agent = _FakeAgent(["hi"])

    async def fake_build(state):
        return fake_agent

    monkeypatch.setattr("web.agent._build_chat_agent", fake_build)
    state = _FakeAppState()
    ct = await _start_chat(
        sess, "hello", session_id=1, agent_id="",
        model_name="quick", running_mode="normal", app_state=state,
    )
    # 默认路径 per-session build,缓存到 sess.agents["chat"]
    assert sess.agents["chat"] is fake_agent
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

    async def fake_build(state):
        return fake_agent

    monkeypatch.setattr("web.agent._build_chat_agent", fake_build)
    state = _FakeAppState()
    ct1 = await _start_chat(
        sess, "hi", session_id=3, agent_id="",
        model_name="quick", running_mode="normal", app_state=state,
    )
    ct2 = await _start_chat(
        sess, "hi2", session_id=3, agent_id="",
        model_name="quick", running_mode="normal", app_state=state,
    )
    assert ct1 is ct2  # 同一任务


@pytest.mark.asyncio
async def test_start_chat_default_builds_per_session(monkeypatch):
    """默认路径 per-session build(_build_chat_agent),不用 agent_instance。"""
    from web.agent import _start_chat, get_session
    monkeypatch.setattr("web.agent._sessions", {})
    sess = get_session(1)
    fake_agent = _FakeAgent(["hi"])
    built = {"n": 0}

    async def fake_build(state):
        built["n"] += 1
        return fake_agent

    monkeypatch.setattr("web.agent._build_chat_agent", fake_build)
    state = _FakeAppState(agent_config={"models": {"quick": {"protocol": "openai", "model": "x", "api_key": "k"}}}, base_prompt="hi")
    ct = await _start_chat(
        sess, "hello", session_id=1, agent_id="",
        model_name="quick", running_mode="normal", app_state=state,
    )
    await ct.task
    assert built["n"] == 1
    assert sess.agents["chat"] is fake_agent
    # 第二次命中缓存(不重复 build)
    ct2 = await _start_chat(
        sess, "hi2", session_id=1, agent_id="",
        model_name="quick", running_mode="normal", app_state=state,
    )
    await ct2.task
    assert built["n"] == 1


# ── WS 集成测试（TestClient + mock agent） ──────────────────────────
#
# 注意：TestClient.websocket_connect 是同步的，跑在 anyio portal 自己的
# event loop 上。chat_task.task 也创建在 portal loop 上，pytest-asyncio 的
# loop 无法 await 它（"Task belongs to a different loop"）。所以这些测试
# 改为同步：用轮询 sess.chat_task.done 代替 await sess.chat_task.task。


class _MockDB:
    """Mock db：is_connected=True 跳过 lifespan connect；delete_agent_session 成功。"""
    def delete_agent_session(self, sid):
        return True
    def is_connected(self):
        return True
    def connect(self): pass
    def init_schema(self): pass
    def close(self): pass


def _make_app_with_fake_agent(fake_agent, monkeypatch=None):
    """构建带 mock agent 的 FastAPI app。

    默认聊天路径走 _build_chat_agent,patch 为返回 fake_agent(避免真 create_agent)。
    """
    from web.app import create_app

    config = {
        "models": {"quick": {"protocol": "openai", "model": "x", "api_key": "k"}},
        "agent": {"default_model": "quick"},
    }
    app = create_app(_MockDB(), {}, agent_config=config)

    async def _fake_build(state):
        return fake_agent

    if monkeypatch is not None:
        monkeypatch.setattr("web.agent._build_chat_agent", _fake_build)
    return app


class _SlowFakeAgent:
    """带延迟的假 agent，模拟流式产出。"""
    def __init__(self, tokens, delay=0.0):
        self._tokens = tokens
        self._delay = delay
        self.executor = _FakeExecutor()
        self.running_mode = "normal"

    async def chat_stream(self, message, session_id="", model_name=""):
        for t in self._tokens:
            if self._delay:
                await asyncio.sleep(self._delay)
            yield t


def test_ws_disconnect_does_not_cancel_task(monkeypatch):
    """跳转走：WS 断开后 agent 任务继续跑完，full_reply 完整。"""
    import time
    monkeypatch.setattr("web.agent._sessions", {})
    from fastapi.testclient import TestClient

    fake_agent = _SlowFakeAgent(["a", "b", "c"], delay=0.05)
    app = _make_app_with_fake_agent(fake_agent, monkeypatch)
    # 用 with TestClient 保持 portal 跨 WS session 存活，否则 WS 断开时
    # portal 关闭会连带 cancel 掉 chat_task
    with TestClient(app) as client:
        with client.websocket_connect("/api/agent/ws?session_id=10") as ws:
            ws.send_json({"type": "chat", "session_id": 10, "message": "hi", "model": "quick"})
            ws.receive_json()  # 收到 resume 后断开（模拟跳转）

        # WS 断开后，任务应继续（portal 仍活着）
        sess = get_session(10)
        assert sess and sess.chat_task
        for _ in range(200):
            if sess.chat_task.done:
                break
            time.sleep(0.01)
        assert sess.chat_task.full_reply == "abc"
        assert sess.chat_task.done is True


def test_ws_reconnect_resumes(monkeypatch):
    """跳转回：重连后补发 resume + 后续 token + done。"""
    import time
    monkeypatch.setattr("web.agent._sessions", {})
    from fastapi.testclient import TestClient

    fake_agent = _SlowFakeAgent(["x", "y", "z"], delay=0.05)
    app = _make_app_with_fake_agent(fake_agent, monkeypatch)
    with TestClient(app) as client:
        with client.websocket_connect("/api/agent/ws?session_id=11") as ws:
            ws.send_json({"type": "chat", "session_id": 11, "message": "hi", "model": "quick"})
            ws.receive_json()  # resume

        # 等任务跑完（WS 断开后 portal 仍活着，任务继续）
        sess = get_session(11)
        assert sess and sess.chat_task
        for _ in range(200):
            if sess.chat_task.done:
                break
            time.sleep(0.01)
        assert sess.chat_task.done is True

        # 重连，应补发 resume + done
        with client.websocket_connect("/api/agent/ws?session_id=11") as ws2:
            msg1 = ws2.receive_json()
            assert msg1["type"] == "resume"
            assert msg1["full_reply"] == "xyz"
            msg2 = ws2.receive_json()
            assert msg2["type"] == "done"


def test_delete_session_route_calls_destroy(monkeypatch):
    """DELETE /api/agent/sessions/{id} 应触发 destroy_session 清理内存。"""
    import web.agent
    monkeypatch.setattr("web.agent._sessions", {})
    from fastapi.testclient import TestClient

    app = _make_app_with_fake_agent(_FakeAgent([]))
    # 预置一个 session
    get_session(99)
    client = TestClient(app)
    resp = client.delete("/api/agent/sessions/99")
    assert resp.json()["ok"] is True
    assert 99 not in web.agent._sessions
