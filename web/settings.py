# coding=utf-8
"""Agent 管理面板路由（/settings, /settings/agents, /settings/knowledge, /settings/sources, /settings/models）。

Provides HTML pages for system settings, agent role management, knowledge base management,
news source management, and model management.
Uses ``render_template`` from ``web.config`` (same pattern as ``web/agent.py``).
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from web.config import render_template

router = APIRouter()


# ── 常规设置 ──────────────────────────────────────────────────────────


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    """常规设置页面 —— 只读展示系统配置。"""
    return HTMLResponse(
        render_template("pages/settings.html", **{"request": request, "active_page": "settings"})
    )


# ── 新闻源管理 ────────────────────────────────────────────────────────


@router.get("/settings/sources", response_class=HTMLResponse)
async def settings_sources_page(request: Request):
    """新闻源管理页面 —— 列表 + CRUD + 连通性测试。"""
    return HTMLResponse(
        render_template("pages/settings_sources.html", **{"request": request, "active_page": "settings_sources"})
    )


# ── 模型管理 ──────────────────────────────────────────────────────────


@router.get("/settings/models", response_class=HTMLResponse)
async def settings_models_page(request: Request):
    """模型管理页面 —— 列表 + CRUD。"""
    return HTMLResponse(
        render_template("pages/settings_models.html", **{"request": request, "active_page": "settings_models"})
    )


# ── 智能体设置 ────────────────────────────────────────────────────────


@router.get("/settings/agents", response_class=HTMLResponse)
async def agent_settings_page(request: Request):
    """Agent 角色管理页面 —— 列表视图 + 内联编辑器。"""
    return HTMLResponse(
        render_template("pages/settings_agents.html", **{"request": request, "active_page": "settings_agents"})
    )


@router.get("/settings/agents/{id}", response_class=HTMLResponse)
async def agent_edit_page(request: Request, id: str):
    """Agent 编辑页面（复用同一模板，通过 edit_id 自动打开编辑器）。"""
    return HTMLResponse(
        render_template(
            "pages/settings_agents.html",
            {"request": request, "edit_id": id, "active_page": "settings_agents"},
        )
    )


# ── 知识库管理 ────────────────────────────────────────────────────────


@router.get("/settings/knowledge", response_class=HTMLResponse)
async def knowledge_settings_page(request: Request):
    """知识库管理页面 —— 列表视图 + 详情视图。"""
    return HTMLResponse(
        render_template(
            "pages/settings_knowledge.html", {"request": request, "active_page": "settings_knowledge"}
        )
    )


@router.get("/settings/knowledge/{id}", response_class=HTMLResponse)
async def knowledge_detail_page(request: Request, id: str):
    """知识库详情页面（复用同一模板，通过 kb_id 自动打开详情视图）。"""
    return HTMLResponse(
        render_template(
            "pages/settings_knowledge.html",
            {"request": request, "kb_id": id, "active_page": "settings_knowledge"},
        )
    )