"""ChatTask / ChatSession / _drive / _forward / _start_chat / switch 单元测试。"""
import asyncio
import time

import pytest

from web.agent import (
    ChatSession,
    ChatTask,
    _drive,
    _forward,
    _start_chat,
    _switch_agent,
    _sessions,
    destroy_session,
    get_session,
    _parse_int_sid,
)


# ── ChatTask: 订阅/广播/审批 ───────────────────────────────────────


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

    async def responder():
        item = await q.get()
        assert item["type"] == "tool_approval_request"
        ct.respond_approval(item["request_id"], True, "approved")

    asyncio.create_task(responder())
    result = await ct.request_approval({"name": "search_news"}, {"q": "test"})
    assert result == {"approved": True, "reason": "approved"}


# ── ChatSession: 注册表 / 切换 / 快照 ──────────────────────────────


def test_chat_session_get_agent_builds_once(monkeypatch):
    monkeypatch.setattr("web.agent._sessions", {})
    sess = ChatSession(session_id=1)
    calls = {"n": 0}

    def build():
        calls["n"] += 1
        return "agent_obj"

    a1 = sess.get_agent("chat", build)
    a2 = sess.get_agent("chat", build)
    assert a1 == "agent_obj" and a2 == "agent_obj"
    assert calls["n"] == 1  # 只构建一次


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
    assert _parse_int_sid("42") == 42
    assert _parse_int_sid("abc") is None
    assert _parse_int_sid("0") is None
    assert _parse_int_sid("") is None


# ── 快照: 就近读取 agent,兜底 DB ──────────────────────────────────


class _FakeAgentBase:
    """假 agent:承载对话记录 + freeze/activate 计数。"""

    def __init__(self, conversation=None):
        self._conversation = conversation if conversation is not None else []
        self.freeze_calls = 0
        self.activate_calls = 0

    def get_conversation(self):
        return list(self._conversation)

    async def freeze(self):
        self.freeze_calls += 1

    async def activate(self, session_id=""):
        self.activate_calls += 1


@pytest.mark.asyncio
async def test_history_prefers_agent_over_db(monkeypatch):
    """就近读取:current_agent 有记录时不查 DB。"""
    monkeypatch.setattr("web.agent._sessions", {})

    class _DB:
        def __init__(self):
            self.queries = 0

        def get_agent_messages(self, sid, limit=50):
            self.queries += 1
            return [{"role": "user", "content": "db"}]

    db = _DB()
    sess = get_session(1, db=db)
    sess.current_agent = _FakeAgentBase([{"role": "user", "content": "agent"}])
    msgs = await sess.history_messages()
    assert msgs == [{"role": "user", "content": "agent"}]
    assert db.queries == 0  # 未兜底


@pytest.mark.asyncio
async def test_history_falls_back_to_db(monkeypatch):
    """无 current_agent / 记录为空 -> 兜底 DB。"""
    monkeypatch.setattr("web.agent._sessions", {})

    class _DB:
        def get_agent_messages(self, sid, limit=50):
            assert sid == 7
            return [{"role": "user", "content": "db-q"},
                    {"role": "assistant", "content": "db-a"}]

    sess = get_session(7, db=_DB())
    msgs = await sess.history_messages()          # 无 agent
    assert msgs == [{"role": "user", "content": "db-q"},
                    {"role": "assistant", "content": "db-a"}]

    sess.current_agent = _FakeAgentBase([])       # agent 记录为空 -> 仍兜底
    msgs = await sess.history_messages()
    assert msgs[0]["content"] == "db-q"


@pytest.mark.asyncio
async def test_history_agent_error_falls_back(monkeypatch):
    """agent 读取异常 -> 兜底 DB。"""
    monkeypatch.setattr("web.agent._sessions", {})

    class BoomAgent(_FakeAgentBase):
        def get_conversation(self):
            raise RuntimeError("ctx broken")

    class _DB:
        def get_agent_messages(self, sid, limit=50):
            return [{"role": "user", "content": "db"}]

    sess = get_session(1, db=_DB())
    sess.current_agent = BoomAgent()
    assert (await sess.history_messages())[0]["content"] == "db"


# ── _drive: 泵 chat_stream,buffer 累积 + 广播 ─────────────────────


class _FakeExecutor:
    """假 executor:仅承载 _approval_callback(_start_chat 绑定用)。"""
    _approval_callback = None


class _FakeAgent(_FakeAgentBase):
    """假 agent:按预设 token 列表流式产出。"""

    def __init__(self, tokens, conversation=None):
        super().__init__(conversation=conversation)
        self._tokens = tokens
        self.executor = _FakeExecutor()
        self.running_mode = "normal"

    async def chat_stream(self, message, session_id="", model_name=""):
        for t in self._tokens:
            yield t


@pytest.mark.asyncio
async def test_drive_broadcasts_tokens_and_done(monkeypatch):
    monkeypatch.setattr("web.agent._sessions", {})
    sess = ChatSession(session_id=1)
    ct = ChatTask(session_id=1)
    q = ct.subscribe()
    agent = _FakeAgent(["Hello", " World"])
    await _drive(sess, ct, agent, "hi", 1, "quick")
    items = []
    while not q.empty():
        items.append(q.get_nowait())
    assert {"type": "token", "content": "Hello"} in items
    assert {"type": "token", "content": " World"} in items
    assert any(it.get("type") == "done" for it in items)
    assert ct.buffer == "Hello World"
    assert ct.done is True


@pytest.mark.asyncio
async def test_drive_on_error_broadcasts_error(monkeypatch):
    monkeypatch.setattr("web.agent._sessions", {})

    class _BoomAgent(_FakeAgentBase):
        async def chat_stream(self, message, session_id="", model_name=""):
            raise RuntimeError("boom")
            yield  # noqa: never reached

    sess = ChatSession(session_id=1)
    ct = ChatTask(session_id=1)
    q = ct.subscribe()
    await _drive(sess, ct, _BoomAgent(), "hi", 1, "quick")
    items = []
    while not q.empty():
        items.append(q.get_nowait())
    assert any(it.get("type") == "error" for it in items)
    assert "boom" in ct.error
    assert ct.done is True


@pytest.mark.asyncio
async def test_drive_cancel_broadcasts_stopped(monkeypatch):
    """用户 stop:cancel 落入 _drive 的 CancelledError 分支 -> done(stopped)。"""
    monkeypatch.setattr("web.agent._sessions", {})
    sess = ChatSession(session_id=1)
    ct = ChatTask(session_id=1)
    q = ct.subscribe()
    agent = _SlowFakeAgent(["x", "y", "z"], delay=0.05)

    t = asyncio.create_task(_drive(sess, ct, agent, "hi", 1, "quick"))
    await asyncio.sleep(0.08)            # 消费部分 token
    t.cancel()
    await t                              # _drive catch CancelledError 不 re-raise
    assert ct.stopped is True
    assert ct.done is True
    assert ct.buffer                      # 已流出部分仍在 buffer
    items = []
    while not q.empty():
        items.append(q.get_nowait())
    assert any(it.get("type") == "token" for it in items)
    assert items[-1]["type"] == "done" and items[-1].get("stopped") is True


# ── _forward: snapshot + 续推 ─────────────────────────────────────


class _FakeWS:
    """假 WebSocket:收集 send_json 的消息。"""

    def __init__(self):
        self.sent = []

    async def send_json(self, item):
        self.sent.append(item)


@pytest.mark.asyncio
async def test_forward_running_sends_snapshot_then_tail(monkeypatch):
    """运行中任务:先订阅后快照,token 不重不漏,续推至 done。"""
    monkeypatch.setattr("web.agent._sessions", {})

    class _DB:
        def get_agent_messages(self, sid, limit=50):
            return [{"role": "user", "content": "old-q"},
                    {"role": "assistant", "content": "old-a"}]

    sess = get_session(1, db=_DB())
    sess.current_agent = _FakeAgentBase([
        {"role": "user", "content": "old-q"},
        {"role": "assistant", "content": "old-a"},
    ])
    ct = ChatTask(session_id=1)
    ct.buffer = "par"                    # 跳转期间已累积的 partial
    sess.chat_task = ct
    q = ct.subscribe()

    ws = _FakeWS()

    async def produce():
        await asyncio.sleep(0.01)
        ct.buffer += "!"
        ct.broadcast({"type": "token", "content": "!"})
        ct.done = True
        ct.broadcast({"type": "done", "session_id": 1, "full_reply": ct.buffer})

    asyncio.create_task(produce())
    await _forward(ws, sess, 1, snapshot=True)
    types = [m["type"] for m in ws.sent]
    # 顺序: snapshot(先订阅后读 buffer -> partial 含已产出的全部) -> token -> done
    assert types[0] == "snapshot"
    snap = ws.sent[0]
    assert snap["running"] is True
    assert snap["partial"] == "par"
    assert snap["messages"] == [{"role": "user", "content": "old-q"},
                                {"role": "assistant", "content": "old-a"}]
    assert types[1:] == ["token", "done"]
    assert ws.sent[-1]["type"] == "done"


@pytest.mark.asyncio
async def test_forward_done_task_snapshot_only(monkeypatch):
    """已完成任务:只发 snapshot(全量历史),不 replay resume/done。"""
    monkeypatch.setattr("web.agent._sessions", {})
    sess = get_session(1, db=None)
    sess.current_agent = _FakeAgentBase([
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "complete answer"},
    ])
    ct = ChatTask(session_id=1)
    ct.done = True
    sess.chat_task = ct
    ws = _FakeWS()
    await _forward(ws, sess, 1, snapshot=True)
    assert len(ws.sent) == 1
    assert ws.sent[0]["type"] == "snapshot"
    assert ws.sent[0]["running"] is False
    assert ws.sent[0]["partial"] == ""
    assert ws.sent[0]["messages"][-1]["content"] == "complete answer"


@pytest.mark.asyncio
async def test_forward_without_snapshot_only_tails(monkeypatch):
    """新 chat(snapshot=False):仅订阅转发队列,不发 snapshot。"""
    monkeypatch.setattr("web.agent._sessions", {})
    sess = ChatSession(session_id=1)
    ct = ChatTask(session_id=1)
    sess.chat_task = ct
    ws = _FakeWS()

    async def run():
        await _forward(ws, sess, 1, snapshot=False)

    t = asyncio.create_task(run())
    await asyncio.sleep(0.01)            # 让 _forward 完成 subscribe
    ct.broadcast({"type": "token", "content": "a"})
    ct.done = True
    ct.broadcast({"type": "done", "session_id": 1, "full_reply": "a"})
    await t
    types = [m["type"] for m in ws.sent]
    assert "snapshot" not in types
    assert types == ["token", "done"]


# ── _start_chat: 防重入 / 惰性构建 / user 落库 ─────────────────────


class _FakeAppState:
    """模拟 app.state。"""

    def __init__(self, agent_factory=None, db=None, agent_config=None,
                 base_prompt=""):
        self.agent_factory = agent_factory
        self.db = db
        self.agent_config = agent_config or {}
        self.base_prompt = base_prompt


class _CaptureDB:
    """记录 save_agent_message / get_agent_messages 调用。"""

    def __init__(self):
        self.saved = []
        self.messages = []

    def save_agent_message(self, sid, role, content, agent_id="0",
                           model_version=""):
        self.saved.append((sid, role, content))

    def get_agent_messages(self, sid, limit=50):
        return list(self.messages)


@pytest.mark.asyncio
async def test_start_chat_builds_default_and_saves_user(monkeypatch):
    """首条消息:惰性构建默认 agent + activate + 接口层落库 user。"""
    from web.agent import _build_chat_agent

    monkeypatch.setattr("web.agent._sessions", {})
    db = _CaptureDB()
    sess = get_session(1, db=db)
    fake_agent = _FakeAgent(["hi"])

    async def fake_build(state):
        return fake_agent

    monkeypatch.setattr("web.agent._build_chat_agent", fake_build)
    ct = await _start_chat(sess, "hello", model_name="quick",
                           running_mode="normal", app_state=_FakeAppState())
    await ct.task
    assert sess.current_agent is fake_agent
    assert sess.current_key == "chat"
    assert sess.agents["chat"] is fake_agent
    assert db.saved == [(1, "user", "hello")]     # 接口层接收即存
    assert fake_agent.activate_calls == 1         # 构建后 activate
    assert ct.buffer == "hi"
    assert ct.done is True


@pytest.mark.asyncio
async def test_start_chat_reuses_running_task(monkeypatch):
    """防重入:已有进行中任务则返回它,不落库新 user、不起任务。"""
    monkeypatch.setattr("web.agent._sessions", {})
    db = _CaptureDB()
    sess = get_session(3, db=db)
    fake_agent = _SlowFakeAgent(["a", "b", "c"])

    async def fake_build(state):
        return fake_agent

    monkeypatch.setattr("web.agent._build_chat_agent", fake_build)
    ct1 = await _start_chat(sess, "hi", model_name="quick",
                            running_mode="normal", app_state=_FakeAppState())
    ct2 = await _start_chat(sess, "hi2", model_name="quick",
                            running_mode="normal", app_state=_FakeAppState())
    assert ct1 is ct2
    assert db.saved == [(3, "user", "hi")]        # 第二条未落库
    ct1.task.cancel()
    await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_start_chat_uses_current_agent_after_switch(monkeypatch):
    """switch 之后 start_chat 直接用 current_agent,不再构建。"""
    monkeypatch.setattr("web.agent._sessions", {})
    db = _CaptureDB()
    sess = get_session(1, db=db)
    switched = _FakeAgent(["from-b"])
    sess.current_agent = switched
    sess.current_key = "agent:b"
    sess.agents["agent:b"] = switched

    async def fail_build(state):
        raise AssertionError("不应重新构建")

    monkeypatch.setattr("web.agent._build_chat_agent", fail_build)
    ct = await _start_chat(sess, "hi", model_name="quick",
                           running_mode="normal", app_state=_FakeAppState())
    await ct.task
    assert ct.buffer == "from-b"


# ── _switch_agent: freeze 旧 -> build/复用 -> activate 新 ─────────


class _FakeFactory:
    def __init__(self):
        self.built = 0

    def build(self, defn):
        self.built += 1
        return _FakeAgent(["factory"], conversation=[])


class _FakeDefn:
    pass


@pytest.mark.asyncio
async def test_switch_agent_freezes_old_activates_new(monkeypatch):
    monkeypatch.setattr("web.agent._sessions", {})
    sess = get_session(2, db=_CaptureDB())
    old = _FakeAgent(["old"])
    sess.current_agent = old
    sess.current_key = "chat"
    sess.agents["chat"] = old

    factory = _FakeFactory()
    state = _FakeAppState(agent_factory=factory, db=_CaptureDB())
    state.db.get_agent_definition = lambda aid: _FakeDefn()

    await _switch_agent(sess, "abc-123", state)
    assert old.freeze_calls == 1                    # 旧 agent freeze
    assert sess.current_key == "agent:abc-123"
    assert sess.current_agent.activate_calls == 1   # 新 agent activate
    assert factory.built == 1
    assert sess.agents["agent:abc-123"] is sess.current_agent

    # 再切回默认:build 走 _build_chat_agent
    async def fake_build(st):
        return old

    monkeypatch.setattr("web.agent._build_chat_agent", fake_build)
    await _switch_agent(sess, "", state)
    assert sess.current_agent is old
    assert sess.current_key == "chat"
    assert old.activate_calls == 1                  # 切回时 activate


@pytest.mark.asyncio
async def test_switch_agent_reuses_cached(monkeypatch):
    """切回已缓存的 agent:不重复 build,但重新 activate。"""
    monkeypatch.setattr("web.agent._sessions", {})
    sess = get_session(2, db=_CaptureDB())
    a = _FakeAgent(["a"])
    b = _FakeAgent(["b"])
    sess.agents["chat"] = a
    sess.agents["agent:x"] = b
    sess.current_agent = a
    sess.current_key = "chat"

    factory = _FakeFactory()
    state = _FakeAppState(agent_factory=factory, db=_CaptureDB())
    state.db.get_agent_definition = lambda aid: _FakeDefn()

    await _switch_agent(sess, "x", state)
    assert factory.built == 0                       # 缓存命中
    assert b.activate_calls == 1


@pytest.mark.asyncio
async def test_switch_agent_rejected_while_generating(monkeypatch):
    monkeypatch.setattr("web.agent._sessions", {})
    sess = get_session(2, db=_CaptureDB())
    ct = ChatTask(session_id=2)
    ct.task = asyncio.create_task(asyncio.sleep(100))
    sess.chat_task = ct
    with pytest.raises(RuntimeError, match="生成中"):
        await _switch_agent(sess, "x", _FakeAppState())
    ct.task.cancel()


# ── WS 集成测试(TestClient + mock agent) ──────────────────────────
#
# 注意:TestClient.websocket_connect 是同步的,跑在 anyio portal 自己的
# event loop 上。chat_task.task 也创建在 portal loop 上,pytest-asyncio 的
# loop 无法 await 它。所以这些测试用轮询 sess.chat_task.done 代替 await。


class _MockDB(_CaptureDB):
    """Mock db:is_connected=True 跳过 lifespan connect;delete 成功。"""

    def __init__(self):
        super().__init__()
        self.messages = [{"role": "user", "content": "历史问题"}]

    def delete_agent_session(self, sid):
        return True

    def is_connected(self):
        return True

    def connect(self): pass

    def init_schema(self): pass

    def close(self): pass


def _make_app_with_fake_agent(fake_agent, monkeypatch=None):
    """构建带 mock 默认 agent 的 FastAPI app。"""
    from web.app import create_app

    config = {
        "agent": {
            "default_model": "quick",
            "models": {"quick": {"protocol": "openai", "model": "x", "api_key": "k"}},
        },
    }
    app = create_app(_MockDB(), {}, agent_config=config, base_prompt="test")

    async def _fake_build(state):
        return fake_agent

    if monkeypatch is not None:
        monkeypatch.setattr("web.agent._build_chat_agent", _fake_build)
    return app


class _SlowFakeAgent(_FakeAgent):
    """带延迟的假 agent,模拟流式产出。"""

    def __init__(self, tokens, delay=0.0, conversation=None):
        super().__init__(tokens, conversation=conversation)
        self._delay = delay

    async def chat_stream(self, message, session_id="", model_name=""):
        for t in self._tokens:
            if self._delay:
                await asyncio.sleep(self._delay)
            yield t


def test_ws_disconnect_does_not_cancel_task(monkeypatch):
    """跳转走:WS 断开后 agent 任务继续跑完,buffer 完整。"""
    monkeypatch.setattr("web.agent._sessions", {})
    from fastapi.testclient import TestClient

    fake_agent = _SlowFakeAgent(["a", "b", "c"], delay=0.05,
                                conversation=[{"role": "user", "content": "历史问题"}])
    app = _make_app_with_fake_agent(fake_agent, monkeypatch)
    with TestClient(app) as client:
        with client.websocket_connect("/api/agent/ws?session_id=10") as ws:
            ws.send_json({"type": "chat", "session_id": 10, "message": "hi",
                          "model": "quick"})
            ws.receive_json()  # 收到首条事件后断开(模拟跳转)

        sess = get_session(10)
        assert sess and sess.chat_task
        for _ in range(200):
            if sess.chat_task.done:
                break
            time.sleep(0.01)
        assert sess.chat_task.buffer == "abc"
        assert sess.chat_task.done is True


def test_ws_reconnect_after_done_sends_snapshot(monkeypatch):
    """任务完成后跳回:发 snapshot(全量历史),不 replay resume/done。"""
    monkeypatch.setattr("web.agent._sessions", {})
    from fastapi.testclient import TestClient

    fake_agent = _SlowFakeAgent(["x", "y", "z"], delay=0.05,
                                conversation=[{"role": "user", "content": "hi"}])
    app = _make_app_with_fake_agent(fake_agent, monkeypatch)
    with TestClient(app) as client:
        with client.websocket_connect("/api/agent/ws?session_id=11") as ws:
            ws.send_json({"type": "chat", "session_id": 11, "message": "hi",
                          "model": "quick"})
            ws.receive_json()  # snapshot

        sess = get_session(11)
        for _ in range(200):
            if sess.chat_task.done:
                break
            time.sleep(0.01)
        # 跑完后 agent 对话记录(热源)含完整轮次
        assert fake_agent.get_conversation() == [{"role": "user", "content": "hi"}]

        # 重连:只收 snapshot(就近 agent),不 replay resume/done
        with client.websocket_connect("/api/agent/ws?session_id=11") as ws2:
            msg = ws2.receive_json()
            assert msg["type"] == "snapshot"
            assert msg["running"] is False
            assert msg["partial"] == ""
            assert msg["messages"] == [{"role": "user", "content": "hi"}]


def test_ws_switch_message(monkeypatch):
    """switch 消息:切换执行体并回 switch_ack。"""
    monkeypatch.setattr("web.agent._sessions", {})
    from fastapi.testclient import TestClient

    fake_agent = _SlowFakeAgent(["x"])
    app = _make_app_with_fake_agent(fake_agent, monkeypatch)

    class _Factory:
        def build(self, defn):
            return _FakeAgent(["role-agent"],
                              conversation=[{"role": "user", "content": "c"}])

    class _DefnDB(_MockDB):
        def get_agent_definition(self, aid):
            return object()

    app.state.agent_factory = _Factory()
    app.state.db = _DefnDB()

    with TestClient(app) as client:
        with client.websocket_connect("/api/agent/ws?session_id=12") as ws:
            first = ws.receive_json()      # 连接即发 snapshot
            assert first["type"] == "snapshot"
            ws.send_json({"type": "switch", "session_id": 12,
                          "agent_id": "abc"})
            ack = ws.receive_json()
            assert ack["type"] == "switch_ack"
            assert ack["agent_id"] == "abc"

            sess = get_session(12)
            assert sess.current_key == "agent:abc"


def test_delete_session_route_calls_destroy(monkeypatch):
    """DELETE /api/agent/sessions/glm-5.2_ark_toC 应触发 destroy_session。"""
    import web.agent
    monkeypatch.setattr("web.agent._sessions", {})
    from fastapi.testclient import TestClient

    app = _make_app_with_fake_agent(_FakeAgent([]), monkeypatch)
    get_session(99, db=_MockDB())
    client = TestClient(app)
    resp = client.delete("/api/agent/sessions/99")
    assert resp.json()["ok"] is True
    assert 99 not in web.agent._sessions


def test_create_app_stores_base_prompt():
    """create_app 接收 base_prompt 并挂 app.state。"""
    from web.app import create_app

    class _DB(_MockDB):
        pass

    app = create_app(_DB(), {}, agent_config={}, base_prompt="你是助手")
    assert app.state.base_prompt == "你是助手"
