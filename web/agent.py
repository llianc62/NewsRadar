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

SESSION_COOKIE = "newsradar_session"


@dataclasses.dataclass
class ChatTask:
    """进行中的对话任务 -- 与 WebSocket 解耦。

    token/done/error 通过 broadcast 推给当前在线的 WS 订阅者；
    WS 断开只退订，不取消任务。审批也走订阅通道，不绑 WS。
    """
    session_id: int
    task: "asyncio.Task | None" = None
    full_reply: str = ""
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
    """单会话运行时 -- 托管该会话用过的 agent 与进行中任务。

    Agent 跟随 session 生命周期：session 不删除则 agent 不清理。
    一个 session 可持有多个 agent（切换角色时各自构建并缓存）。
    """
    session_id: int
    agents: dict[str, Any] = dataclasses.field(default_factory=dict)
    chat_task: ChatTask | None = None

    def get_agent(self, key: str, build_fn: Callable[[], Any]) -> Any:
        """命中缓存返回，未命中静默构建并缓存。"""
        if key not in self.agents:
            self.agents[key] = build_fn()
        return self.agents[key]

    def destroy(self) -> None:
        """session 删除时清理：取消进行中任务 + 清空 agent 缓存。"""
        if self.chat_task and not self.chat_task.done and self.chat_task.task:
            self.chat_task.task.cancel()
        self.agents.clear()
        self.chat_task = None


_sessions: dict[int, ChatSession] = {}


def get_session(session_id: int) -> ChatSession:
    """命中返回，未命中新建空壳 ChatSession（agent 惰性构建）。"""
    if session_id not in _sessions:
        _sessions[session_id] = ChatSession(session_id=session_id)
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


async def _run_chat(
    sess: ChatSession,
    ct: ChatTask,
    agent,
    message: str,
    session_id: int,
    model_name: str,
) -> None:
    """后台运行 agent.chat_stream，token 累积到 ct.full_reply 并广播给订阅者。

    CancelledError（用户 stop）-> stopped=True + broadcast done(stopped) + return 不 re-raise
    （executor 的 finally _finalize 已执行 memory.save）。
    """
    try:
        async for token in agent.chat_stream(
            message, session_id=str(session_id), model_name=model_name
        ):
            ct.full_reply += token
            ct.broadcast({"type": "token", "content": token})
    except asyncio.CancelledError:
        ct.stopped = True
        ct.done = True
        ct.broadcast({
            "type": "done", "session_id": session_id,
            "full_reply": ct.full_reply, "stopped": True,
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
        "type": "done", "session_id": session_id, "full_reply": ct.full_reply,
    })


async def _forward(ws: WebSocket, ct: ChatTask, session_id: int) -> None:
    """订阅 ChatTask 并转发到 WebSocket -- 补发 + 实时续推。

    subscribe 后立即读 full_reply（无 await 间隔）保证原子快照：
    subscribe 之前的 token 在 full_reply，之后的在 queue，不重不漏。
    """
    q = ct.subscribe()
    replay = ct.full_reply  # 原子快照
    try:
        if ct.done:
            if replay:
                await ws.send_json({"type": "resume", "full_reply": replay})
            await ws.send_json({
                "type": "done", "session_id": session_id,
                "full_reply": replay, "stopped": ct.stopped,
            })
        else:
            await ws.send_json({"type": "resume", "full_reply": replay})
            while True:
                item = await q.get()
                await ws.send_json(item)
                if item.get("type") in ("done", "error"):
                    break
    except Exception:
        pass
    finally:
        ct.unsubscribe(q)


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


# ── REST: 常规设置 ──────────────────────────────────────────────────


@router.get("/api/settings")
async def get_settings(request: Request):
    """返回常规设置（只读，来自 config.yaml）。"""
    cfg = getattr(request.app.state, "agent_config", None) or {}
    app_cfg = cfg.get("app", {})
    crawler_cfg = cfg.get("crawler", {})
    notif_cfg = cfg.get("notification", {})

    return {
        "timezone": app_cfg.get("timezone", "Asia/Shanghai"),
        "crawl_circle": crawler_cfg.get("crawl_circle", 60),
        "sync_circle": crawler_cfg.get("sync_circle", 60),
        "email": {
            "from_addr": notif_cfg.get("email", {}).get("from_addr", ""),
            "to_addr": notif_cfg.get("email", {}).get("to_addr", ""),
            "frequency_words": notif_cfg.get("frequency_words", ""),
            "keyword_limit_news": notif_cfg.get("keyword_limit_news", 0),
        },
        "blacklist": notif_cfg.get("black_list", []),
    }


# ── REST: 新闻源管理（只读，数据来自 config.yaml） ────────────────────


@router.get("/api/settings/sources")
async def list_sources(request: Request, source_type: str | None = None):
    """列出新闻源（只读，来自 config.yaml crawler 段）。"""
    cfg = getattr(request.app.state, "agent_config", None) or {}
    crawler_cfg = cfg.get("crawler", {})

    sources = []
    for s in crawler_cfg.get("newsnow", {}).get("sources", []):
        sources.append({
            "id": s.get("id", ""),
            "source_type": "newsnow",
            "name": s.get("name", ""),
            "source_id": s.get("id", ""),
            "url": "",
            "tier": s.get("tier", 4),
            "priority": s.get("priority", 0),
            "enabled": True,
            "config": {},
        })
    for s in crawler_cfg.get("rss", {}).get("sources", []):
        sources.append({
            "id": s.get("id", ""),
            "source_type": "rss",
            "name": s.get("name", ""),
            "source_id": s.get("id", ""),
            "url": s.get("url", ""),
            "tier": s.get("tier", 4),
            "priority": s.get("priority", 0),
            "enabled": s.get("enabled", True),
            "config": {"max_age_days": s.get("max_age_days", 0)},
        })

    if source_type:
        sources = [s for s in sources if s["source_type"] == source_type]
    return {"sources": sources}


@router.post("/api/settings/sources")
async def create_source(_body: dict, _request: Request):
    """新建新闻源（只读模式，已禁用）。"""
    return {"id": None, "source": None}


@router.put("/api/settings/sources/{source_id}")
async def update_source(_source_id: str, _body: dict, _request: Request):
    """更新新闻源（只读模式，已禁用）。"""
    return {"ok": True, "source": None}


@router.delete("/api/settings/sources/{source_id}")
async def delete_source(_source_id: str, _request: Request):
    """删除新闻源（只读模式，已禁用）。"""
    return {"ok": True}


@router.post("/api/settings/sources/{source_id}/test")
async def test_source_connectivity(source_id: str, request: Request):
    """测试 RSS 连通性（HTTP GET 到 URL，检查响应）。

    从 config.yaml 查找来源 URL，不依赖数据库。
    """
    cfg = getattr(request.app.state, "agent_config", None) or {}
    crawler_cfg = cfg.get("crawler", {})
    rss_sources = crawler_cfg.get("rss", {}).get("sources", [])
    source = next((s for s in rss_sources if s.get("id") == source_id), None)
    if not source:
        raise HTTPException(404, "新闻源不存在")
    url = source.get("url", "")
    if not url:
        return {"ok": False, "error": "该新闻源没有配置 URL"}
    import httpx
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            resp = await client.get(url)
            content_type = resp.headers.get("content-type", "")
            is_xml = "xml" in content_type or resp.text.strip().startswith("<?xml")
            return {
                "ok": True,
                "status_code": resp.status_code,
                "content_type": content_type,
                "is_xml": is_xml,
                "body_preview": resp.text[:500] if resp.status_code < 400 else "",
            }
    except Exception as e:
        return {"ok": False, "error": str(e)[:500]}


@router.post("/api/settings/sources/seed")
async def seed_sources(_request: Request):
    """从 config.yaml 种子（只读模式，已禁用）。"""
    return {"ok": True, "inserted": 0}


# ── REST: 模型管理（只读，数据来自 config.yaml） ─────────────────────────


@router.get("/api/models")
async def list_models(request: Request):
    """列出所有模型配置（只读，来自 config.yaml models 段）。"""
    cfg = getattr(request.app.state, "agent_config", None) or {}
    models = cfg.get("models", {})
    items = []
    for name, m in models.items():
        item = dict(m)
        item["name"] = name
        # 隐藏 api_key，前端不需要看到
        item.pop("api_key", None)
        items.append(item)
    return {"models": items}


@router.post("/api/models")
async def create_model(_body: dict, _request: Request):
    """添加模型（只读模式，已禁用）。"""
    return {"ok": True, "model": None}


@router.put("/api/models/{model_name}")
async def update_model(_model_name: str, _body: dict, _request: Request):
    """更新模型配置（只读模式，已禁用）。"""
    return {"ok": True, "model": None}


@router.delete("/api/models/{model_name}")
async def delete_model(_model_name: str, _request: Request):
    """删除模型（只读模式，已禁用）。"""
    return {"ok": True}


# ── WebSocket: 统一聊天通道（支持 agent_id 参数） ──


@router.websocket("/api/agent/ws")
async def agent_websocket_endpoint(ws: WebSocket):
    """WebSocket 端点，支持 ``agent_id`` 查询参数。

    当 ``agent_id`` 存在时，从 DB 加载 ``AgentDefinition`` 并通过
    ``AgentFactory`` 构建角色化 Agent；否则回退到 ``app.state.default_agent``。
    """
    agent_id = ws.query_params.get("agent_id", "")
    await ws.accept()
    client_id = id(ws)
    _ws_clients[client_id] = ws

    config = getattr(ws.app.state, "agent_config", None) or {}
    db = ws.app.state.db

    agent_cfg = config.get("agent", {})
    current_model = agent_cfg.get("default_model", "quick")
    current_running_mode = "normal"
    current_task: asyncio.Task | None = None
    pending_approvals: dict[str, asyncio.Future] = {}

    # Resolve agent: agent_id → AgentDefinition → AgentFactory.build()
    if agent_id:
        factory = getattr(ws.app.state, "agent_factory", None)
        defn = db.get_agent_definition(agent_id)
        if defn and factory:
            agent_instance = factory.build(defn)
        elif not defn:
            await ws.close(code=4004, reason="agent not found")
            return
        else:
            await ws.close(code=4004, reason="agent_factory not configured")
            return
    else:
        agent_instance = getattr(ws.app.state, "agent_instance", None)

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
                if "model" in data:
                    current_model = data["model"]
                if "running_mode" in data:
                    current_running_mode = data["running_mode"]

                model_cfg = config.get("models", {})
                if not model_cfg:
                    await ws.send_json({"type": "error", "message": "模型未配置"})
                    continue
                if current_model not in model_cfg:
                    current_model = "quick" if "quick" in model_cfg else next(iter(model_cfg))

                async def approval_handler(tool_def, args: dict) -> dict:
                    req_id = str(uuid.uuid4())
                    future = asyncio.get_event_loop().create_future()
                    pending_approvals[req_id] = future
                    try:
                        if dataclasses.is_dataclass(tool_def) and not isinstance(tool_def, type):
                            tool_data = dataclasses.asdict(tool_def)
                        else:
                            tool_data = tool_def
                        await ws.send_json({
                            "type": "tool_approval_request",
                            "request_id": req_id,
                            "tool": tool_data,
                            "args": args,
                        })
                        result = await asyncio.wait_for(future, timeout=120.0)
                        return result
                    except asyncio.TimeoutError:
                        pending_approvals.pop(req_id, None)
                        return {"approved": False, "reason": "审批超时"}
                    except Exception:
                        pending_approvals.pop(req_id, None)
                        return {"approved": False, "reason": "审批连接中断"}

                chat_agent = agent_instance
                if chat_agent is None:
                    print("[Agent WS] agent_instance is None, creating fallback agent")
                if chat_agent is not None:
                    chat_agent.running_mode = current_running_mode
                    chat_agent.executor._approval_callback = approval_handler
                else:
                    from agent.agent import DefaultAgent
                    from agent.memory import ShortTermMemory
                    from agent.tools.tools import setup_builtin_tools
                    print("[Agent WS] Creating fallback ReActExecutor agent")
                    fallback_registry = setup_builtin_tools()
                    try:
                        from agent.mcp import MCPClient
                        mcp_session = await MCPClient.connect_stdio(
                            "python", "-m", "agent.mcp.news_server",
                        )
                        fallback_registry.add_mcp(mcp_session, level_map={
                            "search_news": 2, "get_hot_topics": 1,
                            "get_news_detail": 2, "analyze_sentiment": 1, "get_source_stats": 1,
                        })
                    except Exception as mcp_err:
                        print(f"[Agent WS] MCP fallback connect failed: {mcp_err}")
                    chat_agent = DefaultAgent(
                        model_cfg,
                        tools=fallback_registry,
                        memory=ShortTermMemory(db, window_size=20),
                        running_mode=current_running_mode,
                        approval_callback=approval_handler,
                    )

                full_reply = ""

                async def generate():
                    nonlocal full_reply
                    try:
                        async for token in chat_agent.chat_stream(message, session_id=str(session_id), model_name=current_model):
                            full_reply += token
                            await ws.send_json({"type": "token", "content": token})
                    except asyncio.CancelledError:
                        await ws.send_json({"type": "done", "session_id": session_id, "full_reply": full_reply, "stopped": True})
                        return
                    except Exception as e:
                        import traceback
                        traceback.print_exc()
                        await ws.send_json({"type": "error", "message": str(e)[:500]})
                        return
                    await ws.send_json({"type": "done", "session_id": session_id, "full_reply": full_reply})

                current_task = asyncio.create_task(generate())

            elif msg_type == "stop":
                if current_task and not current_task.done():
                    current_task.cancel()
                    try:
                        await current_task
                    except asyncio.CancelledError:
                        pass
                current_task = None

            elif msg_type == "tool_approval_response":
                req_id = data.get("request_id", "")
                if req_id in pending_approvals:
                    future = pending_approvals.pop(req_id)
                    future.set_result({
                        "approved": data.get("approved", False),
                        "reason": data.get("reason", ""),
                    })

    except WebSocketDisconnect:
        if current_task and not current_task.done():
            current_task.cancel()
        for future in pending_approvals.values():
            if not future.done():
                future.set_result({"approved": False, "reason": "连接断开"})
        pending_approvals.clear()
    finally:
        _ws_clients.pop(client_id, None)


print("[Agent] Routes ready — WebSocket /api/agent/ws")
