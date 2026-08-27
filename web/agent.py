# coding=utf-8
"""NewsRadar agent routes — APIRouter for Agent chat page, session REST, WebSocket.

All routes resolve dependencies from ``request.app.state`` (or ``ws.app.state``
for the WebSocket endpoint), matching the pattern used by ``web/news.py``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json as _json
import os
import uuid

from uuid import uuid4
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse

from agent.data import AgentDefinition, AgentKnowledge
from web.config import render_template

router = APIRouter()

# ── WebSocket connection pool ──
_ws_clients: dict[int, WebSocket] = {}

_sessions: dict[int, ChatSession] = {}

SESSION_COOKIE = "newsradar_session"


@dataclasses.dataclass
class ChatTask:
    """单轮对话任务运行时 -- 与 WebSocket 解耦。

    ``buffer`` 累积本轮 partial(消费 chat_stream 时先累积后广播,轮结束作废;
    完成态权威在 agent ctx / DB)。token/done/error 通过 broadcast 推给当前
    在线的 WS 订阅者;WS 断开只退订,不取消任务。审批也走订阅通道,不绑 WS。
    """
    session_id: int
    task: "asyncio.Task | None" = None
    buffer: str = ""
    error: str = ""
    done: bool = False
    stopped: bool = False
    _subscribers: list = dataclasses.field(default_factory=list)
    pending_approvals: dict = dataclasses.field(default_factory=dict)

    def subscribe(self) -> "asyncio.Queue":
        """注册一个订阅者队列，后续 broadcast 会 put 到该队列。"""
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: "asyncio.Queue") -> None:
        if q in self._subscribers:
            self._subscribers.remove(q)

    def broadcast(self, item: dict) -> None:
        """向所有在线订阅者推送一个事件（token/done/error/approval）。"""
        for q in list(self._subscribers):
            q.put_nowait(item)

    async def request_approval(self, tool_def, args: dict) -> dict:
        """发起工具审批：广播请求 + 等待 WS 响应（120s 超时拒绝）。"""
        req_id = str(uuid4())
        future = asyncio.get_event_loop().create_future()
        self.pending_approvals[req_id] = future
        try:
            if dataclasses.is_dataclass(tool_def) and not isinstance(tool_def, type):
                tool_data = dataclasses.asdict(tool_def)
            else:
                tool_data = tool_def
            self.broadcast({
                "type": "tool_approval_request",
                "request_id": req_id,
                "tool": tool_data,
                "args": args,
            })
            return await asyncio.wait_for(future, timeout=120.0)
        except asyncio.TimeoutError:
            self.pending_approvals.pop(req_id, None)
            return {"approved": False, "reason": "审批超时"}
        except Exception:
            self.pending_approvals.pop(req_id, None)
            return {"approved": False, "reason": "审批连接中断"}

    def respond_approval(self, req_id: str, approved: bool, reason: str = "") -> None:
        """WS 收到 tool_approval_response 时回填 future。"""
        fut = self.pending_approvals.pop(req_id, None)
        if fut and not fut.done():
            fut.set_result({"approved": approved, "reason": reason})


@dataclasses.dataclass
class ChatSession:
    """单会话运行时 -- agent 管理 + 本轮任务 + 历史读取。

    Agent 跟随 session 生命周期：session 不删除则 agent 不清理。
    ``current_agent`` 为当前执行体(切换时换人);``chat_task`` 为本轮任务
    (防重入:同时最多一轮)。``db`` 仅两用:user 落库 + 历史兜底读取。
    """
    session_id: int
    db: Any = None
    agents: dict[str, Any] = dataclasses.field(default_factory=dict)
    current_agent: Any = None
    current_key: str = ""
    chat_task: ChatTask | None = None

    def get_agent(self, key: str, build_fn: Callable[[], Any]) -> Any:
        """命中缓存返回，未命中静默构建并缓存。"""
        if key not in self.agents:
            self.agents[key] = build_fn()
        return self.agents[key]

    async def history_messages(self) -> list[dict]:
        """有序对话记录 -- 就近读取:current_agent 优先,None/异常/空兜底 DB。"""
        if self.current_agent is not None:
            try:
                msgs = self.current_agent.get_conversation()
                if msgs:
                    return msgs
            except Exception:
                pass  # agent 读取异常 -> 兜底 DB
        if self.db is None:
            return []
        try:
            rows = await asyncio.to_thread(
                self.db.get_agent_messages, self.session_id, 50)
            return [{"role": r["role"], "content": r["content"]} for r in rows]
        except Exception:
            return []

    def destroy(self) -> None:
        """session 删除时清理：取消进行中任务 + 清空 agent 缓存。"""
        if self.chat_task and not self.chat_task.done and self.chat_task.task:
            self.chat_task.task.cancel()
        self.agents.clear()
        self.current_agent = None
        self.chat_task = None


def get_session(session_id: int, db: Any = None) -> ChatSession:
    """命中返回，未命中新建空壳 ChatSession（agent 惰性构建）。"""
    if session_id not in _sessions:
        _sessions[session_id] = ChatSession(session_id=session_id, db=db)
    return _sessions[session_id]


def destroy_session(session_id: int) -> None:
    """session 删除时调用：cancel 任务 + 清 agents + 移出表。"""
    s = _sessions.pop(session_id, None)
    if s:
        s.destroy()


def _parse_int_sid(raw: str) -> int | None:
    """解析 session_id 字符串为正整数，非法返回 None。"""
    try:
        sid = int(raw)
    except (TypeError, ValueError):
        return None
    return sid if sid >= 1 else None


async def _drive(
    sess: ChatSession,
    ct: ChatTask,
    agent,
    message: str,
    session_id: int,
    model_name: str,
) -> None:
    """泵 agent.chat_stream -- buffer 累积 + 广播给订阅者。

    先累积后广播(同一同步步骤)保证 _forward 快照不重不漏。
    assistant 落库由 agent 内部 _finalize;CancelledError(用户 stop)
    -> stopped=True + broadcast done(stopped) + return 不 re-raise
    (executor 的 finally 已把流出 partial append 进 ctx 并 _finalize)。
    """
    try:
        async for token in agent.chat_stream(
            message, session_id=str(session_id), model_name=model_name
        ):
            ct.buffer += token
            ct.broadcast({"type": "token", "content": token})
    except asyncio.CancelledError:
        ct.stopped = True
        ct.done = True
        ct.broadcast({
            "type": "done", "session_id": session_id,
            "full_reply": ct.buffer, "stopped": True,
        })
        return
    except Exception as e:
        import traceback
        traceback.print_exc()
        ct.error = str(e)[:500]
        ct.done = True
        ct.broadcast({"type": "error", "message": ct.error})
        return
    ct.done = True
    ct.broadcast({
        "type": "done", "session_id": session_id, "full_reply": ct.buffer,
    })


async def _forward(ws: WebSocket, sess: ChatSession, session_id: int,
                   *, snapshot: bool = False) -> None:
    """转发事件到 WebSocket -- snapshot=True 时先发会话快照再续推。

    不变式:running 时先 subscribe 再同步读 buffer(两步间无 await),
    订阅前的 token 全在 buffer 快照,之后全在队列,不重不漏。
    已完成任务不 replay:snapshot(agent/DB)已含最终回复。
    """
    ct = sess.chat_task
    running = bool(ct and not ct.done)
    q = ct.subscribe() if (ct is not None and not ct.done) else None
    partial = ct.buffer if (snapshot and running) else ""  # subscribe 后同步读,原子快照
    try:
        if snapshot:
            messages = await sess.history_messages()
            await ws.send_json({
                "type": "snapshot", "session_id": session_id,
                "messages": messages, "partial": partial,
                "running": running, "agent": sess.current_key,
            })
        if q is not None:
            while True:
                item = await q.get()
                await ws.send_json(item)
                if item.get("type") in ("done", "error"):
                    break
    except Exception:
        pass
    finally:
        if q is not None and ct is not None:
            ct.unsubscribe(q)


async def _build_chat_agent(app_state):
    """聊天室默认 agent -- per-session 独立构建。

    LongTermMemory + 内置工具 + News MCP Server(SSE) + base_prompt。
    与 agent_id 路径(AgentFactory.build)并列,均 per-session 隔离。
    """
    from agent.factory import create_agent
    config = getattr(app_state, "agent_config", None) or {}
    model_cfg = config.get("agent", {}).get("models", {})
    db = getattr(app_state, "db", None)
    base_prompt = getattr(app_state, "base_prompt", "") or ""
    mcp_cfg = config.get("mcp_server", {})
    return await create_agent(
        model_cfg,
        system_prompt=base_prompt,
        register_mcp=True,
        mcp_cfg=mcp_cfg if mcp_cfg.get("enabled") else None,
        db=db,
        memory_type="long",
    )


async def _start_chat(
    sess: ChatSession,
    message: str,
    *,
    model_name: str,
    running_mode: str,
    app_state,
) -> ChatTask:
    """启动一次对话任务 -- 防重入 + 复用/惰性构建 current_agent + user 落库。

    若该 session 已有进行中任务，直接返回它（前端续推）。
    chat 路径零 agent 判断:直接用 current_agent(无则惰性构建默认 agent 并
    activate)。user 消息由接口层接收即落库(不进 agent);assistant 归 agent
    的 _finalize。切换走显式 switch 消息(``_switch_agent``)。
    """
    if sess.chat_task and not sess.chat_task.done:
        return sess.chat_task  # 防重入

    if sess.current_agent is None:
        agent = await _build_chat_agent(app_state)
        await agent.activate(str(sess.session_id))
        sess.agents["chat"] = agent
        sess.current_agent = agent
        sess.current_key = "chat"
    else:
        agent = sess.current_agent

    # 接口层落库 user(接收即存;失败降级不阻断对话)
    if sess.db is not None:
        try:
            await asyncio.to_thread(
                sess.db.save_agent_message, sess.session_id, "user", message)
        except Exception as e:
            print(f"[Agent] user 消息落库失败(降级): {e}")

    agent.running_mode = running_mode
    ct = ChatTask(session_id=sess.session_id)
    agent.executor._approval_callback = ct.request_approval

    ct.task = asyncio.create_task(
        _drive(sess, ct, agent, message, sess.session_id, model_name)
    )
    sess.chat_task = ct
    return ct


async def _switch_agent(sess: ChatSession, agent_id: str, app_state) -> str:
    """切换执行体(点击触发,只在轮间):freeze 旧 -> build/复用 -> activate 新。

    freeze 手动保存旧 agent 状态(幂等,通常 no-op);activate 重载全量
    历史 -> 切回已缓存 agent 不再 ctx 陈旧。生成中拒绝。
    """
    if sess.chat_task and not sess.chat_task.done:
        raise RuntimeError("生成中,无法切换智能体")

    key = f"agent:{agent_id}" if agent_id else "chat"
    if sess.current_agent is not None:
        await sess.current_agent.freeze()

    if key in sess.agents:
        agent = sess.agents[key]
    elif agent_id:
        factory = getattr(app_state, "agent_factory", None)
        db = getattr(app_state, "db", None)
        defn = db.get_agent_definition(agent_id) if db else None
        if not defn or not factory:
            raise ValueError(f"agent {agent_id!r} 不可用")
        agent = factory.build(defn)
        sess.agents[key] = agent
    else:
        agent = await _build_chat_agent(app_state)
        sess.agents[key] = agent

    await agent.activate(str(sess.session_id))
    sess.current_agent = agent
    sess.current_key = key
    return key


# ── REST: 聊天页面 ──

@router.get("/agent", response_class=HTMLResponse)
async def agent_page():
    return HTMLResponse(render_template(
        "pages/agent_chat.html",
        active_page="agent",
    ))


# ── REST: 会话列表 ──

@router.get("/api/agent/sessions")
async def list_sessions(request: Request, page: int = 1, page_size: int = 20):
    db = request.app.state.db
    offset = (page - 1) * page_size
    sessions = db.get_agent_sessions(limit=page_size, offset=offset)
    return {"sessions": sessions}


# ── REST: 新建会话 ──

@router.post("/api/agent/sessions")
async def create_session(request: Request, response: Response):
    db = request.app.state.db
    session_id = db.create_agent_session()
    response.set_cookie(
        key=SESSION_COOKIE,
        value=str(session_id),
        path="/",
        httponly=True,
        samesite="lax",
    )
    return {"id": session_id, "title": "新会话"}


# ── REST: 删除会话 ──

@router.delete("/api/agent/sessions/{session_id}")
async def delete_session(request: Request, session_id: int):
    db = request.app.state.db
    deleted = db.delete_agent_session(session_id)
    if not deleted:
        return JSONResponse({"ok": False, "error": "会话不存在"}, status_code=404)
    destroy_session(session_id)  # 清理内存 ChatSession + 取消任务
    return {"ok": True}


# ── REST: 消息列表 ──

@router.get("/api/agent/sessions/{session_id}/messages")
async def get_messages(request: Request, session_id: int, limit: int = 50):
    db = request.app.state.db
    messages = db.get_agent_messages(session_id, limit=limit)
    return {"messages": messages}


# ── Helper: AgentDefinition → JSON-safe dict ──────────────────────


def _agent_def_to_dict(defn: AgentDefinition) -> dict:
    """将 :class:`AgentDefinition` 转为 JSON-safe dict。"""
    d = dataclasses.asdict(defn)
    return d


def _knowledge_to_dict(kb: AgentKnowledge) -> dict:
    """将 :class:`AgentKnowledge` 转为 JSON-safe dict（含 chunk_count）。"""
    d = dataclasses.asdict(kb)
    d["chunk_count"] = getattr(kb, "_chunk_count", 0)
    return d


# ── REST: Agent CRUD ──────────────────────────────────────────────


@router.post("/api/agents")
async def create_agent(body: dict, request: Request):
    """创建角色定义。"""
    if "name" not in body or "system_prompt" not in body:
        raise HTTPException(status_code=422, detail="name and system_prompt are required")
    db = request.app.state.db
    defn = AgentDefinition(
        id=str(uuid4()),
        name=body["name"],
        description=body.get("description", ""),
        system_prompt=body["system_prompt"],
        tools=body.get("tools", []),
        knowledge_id=body.get("knowledge_id"),
        metadata=body.get("metadata", {}),
    )
    defn.id = db.create_agent_definition(defn)
    return {"id": defn.id}


@router.get("/api/agents")
async def list_agents(request: Request):
    """列出所有角色定义。"""
    db = request.app.state.db
    defns = db.list_agent_definitions()
    return {"agents": [_agent_def_to_dict(d) for d in defns]}


@router.get("/api/agents/{defn_id}")
async def get_agent(defn_id: str, request: Request):
    """按 ID 查询角色定义。"""
    db = request.app.state.db
    defn = db.get_agent_definition(defn_id)
    if not defn:
        raise HTTPException(404, "角色不存在")
    return _agent_def_to_dict(defn)


@router.put("/api/agents/{defn_id}")
async def update_agent(defn_id: str, body: dict, request: Request):
    """更新角色定义。"""
    db = request.app.state.db
    existing = db.get_agent_definition(defn_id)
    if not existing:
        raise HTTPException(404, "角色不存在")
    for key in ("name", "description", "system_prompt", "tools", "knowledge_id", "metadata"):
        if key in body:
            setattr(existing, key, body[key])
    db.update_agent_definition(existing)
    return {"ok": True}


@router.delete("/api/agents/{defn_id}")
async def delete_agent(defn_id: str, request: Request):
    """删除角色定义。"""
    db = request.app.state.db
    deleted = db.delete_agent_definition(defn_id)
    if not deleted:
        raise HTTPException(404, "角色不存在")
    return {"ok": True}


# ── REST: KB 管理 ─────────────────────────────────────────────────


@router.post("/api/agent/knowledge")
async def create_knowledge(body: dict, request: Request):
    """创建知识库定义。"""
    if "name" not in body:
        raise HTTPException(status_code=422, detail="name is required")
    db = request.app.state.db
    kb = AgentKnowledge(
        id=str(uuid4()),
        name=body["name"],
        description=body.get("description", ""),
    )
    kb.id = db.create_agent_knowledge(kb)
    return {"id": kb.id}


@router.get("/api/agent/knowledge")
async def list_knowledge(request: Request):
    """列出所有知识库。"""
    db = request.app.state.db
    kbs = db.list_agent_knowledge()
    return {"knowledge_bases": [_knowledge_to_dict(k) for k in kbs]}


@router.delete("/api/agent/knowledge/{knowledge_id}")
async def delete_knowledge(knowledge_id: str, request: Request):
    """删除知识库（同时清理切片）。"""
    db = request.app.state.db
    deleted = db.delete_agent_knowledge(knowledge_id)
    if not deleted:
        raise HTTPException(404, "知识库不存在")
    return {"ok": True}


@router.post("/api/agent/knowledge/{knowledge_id}/ingest")
async def ingest_knowledge_doc(knowledge_id: str, request: Request):
    """上传文档到知识库（multipart file）。"""
    db = request.app.state.db
    kb = db.get_agent_knowledge(knowledge_id)
    if not kb:
        raise HTTPException(404, "知识库不存在")
    form = await request.form()
    file = form.get("file")
    if not file:
        raise HTTPException(status_code=400, detail="file required")
    MAX_SIZE = 50 * 1024 * 1024  # 50MB
    content = await file.read()
    if len(content) > MAX_SIZE:
        raise HTTPException(status_code=413, detail="file too large")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="file must be UTF-8 text")
    from agent.knowledge import EmbeddingClient, KnowledgeEngine, PgVectorKnowledgeStore

    engine = KnowledgeEngine(
        store=PgVectorKnowledgeStore(db),
        embedding=EmbeddingClient(
            api_key=os.environ.get("KNOWLEDGE_EMBEDDING_API_KEY", ""),
            base_url=os.environ.get("KNOWLEDGE_EMBEDDING_BASE_URL", ""),
            model=os.environ.get("KNOWLEDGE_EMBEDDING_MODEL", "text-embedding-3-small"),
        ),
        top_k=5,
    )
    count = await asyncio.to_thread(
        engine.ingest_documents,
        [{"source": file.filename, "content": text}],
        namespace=kb.namespace,
    )
    return {"chunks": count}


# ── REST: 工具发现 ────────────────────────────────────────────────


@router.get("/api/tools")
async def list_tools(request: Request):
    """列出所有可用工具（名/描述/分类）。"""
    registry = getattr(request.app.state, "tool_registry", None)
    if registry is None:
        return {"tools": []}
    return {"tools": registry.list_tool_defs()}


# ── WebSocket: 统一聊天通道（支持 agent_id 参数） ──


@router.websocket("/api/agent/ws")
async def agent_websocket_endpoint(ws: WebSocket):
    """WebSocket 端点 -- 纯传输层，任务由 ChatSession 托管。

    支持查询参数 ``?session_id=``（重连恢复）与 ``?agent_id=``（角色）。
    """
    await ws.accept()
    client_id = id(ws)
    _ws_clients[client_id] = ws

    forward_task: asyncio.Task | None = None
    sess: ChatSession | None = None

    # 连接即发 snapshot(历史 + 运行中任务则续推;已完成不 replay)
    session_param = ws.query_params.get("session_id", "")
    sid = _parse_int_sid(session_param) if session_param else None
    if sid is not None:
        sess = get_session(sid, db=ws.app.state.db)
        forward_task = asyncio.create_task(_forward(ws, sess, sid, snapshot=True))

    try:
        while True:
            raw = await ws.receive_text()
            try:
                data = _json.loads(raw)
            except _json.JSONDecodeError:
                await ws.send_json({"type": "error", "message": "无效的 JSON"})
                continue

            msg_type = data.get("type", "")

            if msg_type == "chat":
                message = (data.get("message") or "").strip()
                if not message:
                    continue
                session_id = data.get("session_id", 0)
                if not isinstance(session_id, int) or session_id < 1:
                    await ws.send_json({"type": "error", "message": "session_id 必须为正整数"})
                    continue
                model_name = data.get("model") or "quick"
                running_mode = data.get("running_mode") or "normal"

                config = getattr(ws.app.state, "agent_config", None) or {}
                model_cfg = config.get("agent", {}).get("models", {})
                if not model_cfg:
                    await ws.send_json({"type": "error", "message": "模型未配置"})
                    continue
                if model_name not in model_cfg:
                    model_name = "quick" if "quick" in model_cfg else next(iter(model_cfg))

                sess = get_session(session_id, db=ws.app.state.db)
                try:
                    await _start_chat(
                        sess, message, model_name=model_name,
                        running_mode=running_mode, app_state=ws.app.state,
                    )
                except Exception as e:
                    await ws.send_json({"type": "error", "message": f"启动失败: {e!s}"[:500]})
                    continue

                if forward_task and not forward_task.done():
                    forward_task.cancel()
                forward_task = asyncio.create_task(_forward(ws, sess, session_id))

            elif msg_type == "switch":
                # 点击触发的显式切换(轮间):freeze 旧 -> build/复用 -> activate 新
                session_id = data.get("session_id", 0)
                if not isinstance(session_id, int) or session_id < 1:
                    await ws.send_json({"type": "error", "message": "session_id 必须为正整数"})
                    continue
                agent_id = (data.get("agent_id") or "").strip()
                sess = get_session(session_id, db=ws.app.state.db)
                try:
                    key = await _switch_agent(sess, agent_id, ws.app.state)
                except Exception as e:
                    await ws.send_json({"type": "error", "message": f"切换失败: {e!s}"[:500]})
                    continue
                await ws.send_json({
                    "type": "switch_ack", "agent_id": agent_id, "agent_key": key,
                })

            elif msg_type == "stop":
                if sess and sess.chat_task and not sess.chat_task.done and sess.chat_task.task:
                    sess.chat_task.task.cancel()
                if forward_task and not forward_task.done():
                    forward_task.cancel()
                    try:
                        await forward_task
                    except (asyncio.CancelledError, Exception):
                        pass
                    forward_task = None

            elif msg_type == "tool_approval_response":
                if sess and sess.chat_task:
                    sess.chat_task.respond_approval(
                        data.get("request_id", ""),
                        data.get("approved", False),
                        data.get("reason", ""),
                    )

    except WebSocketDisconnect:
        # 仅停转发，绝不取消 agent 任务 / 不动 ChatSession
        if forward_task and not forward_task.done():
            forward_task.cancel()
    finally:
        _ws_clients.pop(client_id, None)
        if forward_task and not forward_task.done():
            forward_task.cancel()


print("[Agent] Routes ready — WebSocket /api/agent/ws")
