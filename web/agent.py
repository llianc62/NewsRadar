# coding=utf-8
"""NewsRadar agent routes — APIRouter for Agent chat page, session REST, WebSocket.

All routes resolve dependencies from ``request.app.state`` (or ``ws.app.state``
for the WebSocket endpoint), matching the pattern used by ``web/news.py``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json as _json
import uuid

from fastapi import APIRouter, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse

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

                # Build agent (with running mode and approval callback)
                if agent_instance:
                    agent_instance.running_mode = current_running_mode
                    agent_instance.executor._approval_callback = approval_handler
                    chat_agent = agent_instance
                else:
                    from agent.executor import DirectExecutor
                    executor = DirectExecutor(approval_callback=approval_handler)
                    from agent.agent import DefaultAgent
                    chat_agent = DefaultAgent(
                        model_cfg,
                        executor=executor,
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


print("[Agent] Routes ready — WebSocket at /api/ws")
