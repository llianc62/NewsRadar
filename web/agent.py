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

from fastapi import APIRouter, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse

from agent.models import AgentDefinition, AgentKnowledge
from web.config import render_template

router = APIRouter()

# ── WebSocket connection pool ──
_ws_clients: dict[int, WebSocket] = {}

SESSION_COOKIE = "newsradar_session"


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


# ── REST: 角色列表（右侧团队面板） ──

@router.get("/api/agent/personas")
async def list_personas_api(request: Request):
    """列出可用角色（供前端右侧团队面板渲染）。

    未配置 ``persona_manager`` 时返回空列表 + ``enabled: false``，前端据此
    隐藏团队面板。
    """
    manager = getattr(request.app.state, "persona_manager", None)
    if manager is None:
        return {"personas": [], "enabled": False, "default_team": []}
    personas_cfg = (config_get(request, "personas") or {})
    disabled = set(personas_cfg.get("disabled") or [])
    # editor 是聚合者，不出现在可选团队面板
    personas = [
        {
            "name": s.name,
            "display_name": s.display_name,
            "description": s.description,
            "category": s.category,
            "order": s.order,
        }
        for s in manager.available()
        if s.category != "editor" and s.name not in disabled
    ]
    return {
        "enabled": True,
        "personas": personas,
        "default_team": personas_cfg.get("default_team") or [],
    }


def config_get(request: Request, key: str):
    """从 app.state.agent_config 取顶层段（容错）。"""
    cfg = getattr(request.app.state, "agent_config", None) or {}
    return cfg.get(key)


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
        raise HTTPException(400, "file required")
    content = (await file.read()).decode("utf-8")
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
        [{"source": file.filename, "content": content}],
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


# ── WebSocket: 统一实时通道 ──

@router.websocket("/api/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    client_id = id(ws)
    _ws_clients[client_id] = ws

    # Resolve dependencies from app state (matches news.py pattern)
    config = getattr(ws.app.state, "agent_config", None) or {}
    db = ws.app.state.db
    agent_instance = getattr(ws.app.state, "agent_instance", None)
    persona_manager = getattr(ws.app.state, "persona_manager", None)
    persona_orchestrator = getattr(ws.app.state, "persona_orchestrator", None)

    agent_cfg = config.get("agent", {})
    current_model = agent_cfg.get("default_model", "quick")
    current_running_mode = "strict"
    current_task: asyncio.Task | None = None
    pending_approvals: dict[str, asyncio.Future] = {}

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

                # ── 角色解析：支持单选（字符串）或多选（列表）──
                raw_persona = data.get("persona")
                if isinstance(raw_persona, str):
                    persona_names = [raw_persona.strip()] if raw_persona.strip() else []
                elif isinstance(raw_persona, list):
                    persona_names = [p.strip() for p in raw_persona
                                     if isinstance(p, str) and p.strip()]
                else:
                    persona_names = []
                if persona_manager:
                    persona_names = [n for n in persona_names if persona_manager.has(n)]

                is_team = len(persona_names) >= 2 and persona_orchestrator is not None

                # 把运行模式 + 审批回调下发给 manager（单角色 get 与编排器内部 get 共用）
                if persona_manager:
                    persona_manager.set_running_config(current_running_mode, approval_handler)

                # Resolve chat agent（单角色 / 默认 / 现建降级）；团队会诊另走编排器
                chat_agent = None
                if not is_team:
                    if persona_manager and len(persona_names) == 1:
                        try:
                            chat_agent = await persona_manager.get(persona_names[0])
                        except Exception as e:
                            await ws.send_json({"type": "error", "message": f"角色构建失败: {e!s}"[:500]})
                            continue
                    if chat_agent is None:
                        chat_agent = agent_instance
                    if chat_agent is not None:
                        chat_agent.running_mode = current_running_mode
                        chat_agent.executor._approval_callback = approval_handler
                    else:
                        from agent.executor import DirectExecutor
                        from agent.agent import DefaultAgent
                        chat_agent = DefaultAgent(
                            model_cfg,
                            executor=DirectExecutor(approval_callback=approval_handler),
                            running_mode=current_running_mode,
                        )

                # Save user message
                try:
                    db.save_agent_message(session_id, "user", message)
                except Exception:
                    pass

                full_reply = ""

                async def generate():
                    nonlocal full_reply
                    try:
                        if is_team:
                            # 团队会诊：Phase 1 各角色并行（静默）-> signals -> Phase 2 主编流式
                            await ws.send_json({
                                "type": "team_thinking",
                                "personas": persona_names,
                            })
                            async for event in persona_orchestrator.chat_stream(
                                message, persona_names, model_name=current_model
                            ):
                                if event["type"] == "signals":
                                    await ws.send_json({
                                        "type": "signals",
                                        "signals": event["signals"],
                                    })
                                elif event["type"] == "token":
                                    full_reply += event["content"]
                                    await ws.send_json({"type": "token", "content": event["content"]})
                        else:
                            async for token in chat_agent.chat_stream(message, model_name=current_model):
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
                    try:
                        db.save_agent_message(session_id, "assistant", full_reply)
                    except Exception:
                        pass
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


# ── WebSocket: 带 agent_id 的角色化聊天通道 ────────────────────────


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
    persona_manager = getattr(ws.app.state, "persona_manager", None)
    persona_orchestrator = getattr(ws.app.state, "persona_orchestrator", None)

    agent_cfg = config.get("agent", {})
    current_model = agent_cfg.get("default_model", "quick")
    current_running_mode = "strict"
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

                # ── 角色解析：支持单选（字符串）或多选（列表）──
                raw_persona = data.get("persona")
                if isinstance(raw_persona, str):
                    persona_names = [raw_persona.strip()] if raw_persona.strip() else []
                elif isinstance(raw_persona, list):
                    persona_names = [p.strip() for p in raw_persona
                                     if isinstance(p, str) and p.strip()]
                else:
                    persona_names = []
                if persona_manager:
                    persona_names = [n for n in persona_names if persona_manager.has(n)]

                is_team = len(persona_names) >= 2 and persona_orchestrator is not None

                if persona_manager:
                    persona_manager.set_running_config(current_running_mode, approval_handler)

                chat_agent = None
                if not is_team:
                    if persona_manager and len(persona_names) == 1:
                        try:
                            chat_agent = await persona_manager.get(persona_names[0])
                        except Exception as e:
                            await ws.send_json({"type": "error", "message": f"角色构建失败: {e!s}"[:500]})
                            continue
                    if chat_agent is None:
                        chat_agent = agent_instance
                    if chat_agent is not None:
                        chat_agent.running_mode = current_running_mode
                        chat_agent.executor._approval_callback = approval_handler
                    else:
                        from agent.executor import DirectExecutor
                        from agent.agent import DefaultAgent
                        chat_agent = DefaultAgent(
                            model_cfg,
                            executor=DirectExecutor(approval_callback=approval_handler),
                            running_mode=current_running_mode,
                        )

                try:
                    db.save_agent_message(session_id, "user", message)
                except Exception:
                    pass

                full_reply = ""

                async def generate():
                    nonlocal full_reply
                    try:
                        if is_team:
                            await ws.send_json({
                                "type": "team_thinking",
                                "personas": persona_names,
                            })
                            async for event in persona_orchestrator.chat_stream(
                                message, persona_names, model_name=current_model
                            ):
                                if event["type"] == "signals":
                                    await ws.send_json({
                                        "type": "signals",
                                        "signals": event["signals"],
                                    })
                                elif event["type"] == "token":
                                    full_reply += event["content"]
                                    await ws.send_json({"type": "token", "content": event["content"]})
                        else:
                            async for token in chat_agent.chat_stream(message, model_name=current_model):
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
                    try:
                        db.save_agent_message(session_id, "assistant", full_reply)
                    except Exception:
                        pass
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


print("[Agent] Routes ready — WebSocket at /api/ws + /api/agent/ws")
