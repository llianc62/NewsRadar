# coding=utf-8
"""Agent 管理面板路由（/settings/agents, /settings/knowledge）。

Provides HTML pages for agent role management and knowledge base management.
Uses ``render_template`` from ``web.config`` (same pattern as ``web/agent.py``).
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from web.config import render_template

router = APIRouter()


@router.get("/settings/agents", response_class=HTMLResponse)
async def agent_settings_page(request: Request):
    """Agent 角色管理页面 —— 列表视图 + 内联编辑器。"""
    return HTMLResponse(
        render_template("pages/agent_settings_agents.html", {"request": request})
    )


@router.get("/settings/agents/{id}", response_class=HTMLResponse)
async def agent_edit_page(request: Request, id: str):
    """Agent 编辑页面（复用同一模板，通过 edit_id 自动打开编辑器）。"""
    return HTMLResponse(
        render_template(
            "pages/agent_settings_agents.html",
            {"request": request, "edit_id": id},
        )
    )


@router.get("/settings/knowledge", response_class=HTMLResponse)
async def knowledge_settings_page(request: Request):
    """知识库管理页面 —— 列表视图 + 详情视图。"""
    return HTMLResponse(
        render_template(
            "pages/agent_settings_knowledge.html", {"request": request}
        )
    )


@router.get("/settings/knowledge/{id}", response_class=HTMLResponse)
async def knowledge_detail_page(request: Request, id: str):
    """知识库详情页面（复用同一模板，通过 kb_id 自动打开详情视图）。"""
    return HTMLResponse(
        render_template(
            "pages/agent_settings_knowledge.html",
            {"request": request, "kb_id": id},
        )
    )