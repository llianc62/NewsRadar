# coding=utf-8
"""设置面板路由 -- HTML 页面 + 系统设置/新闻源/模型的 JSON API。

HTML 页面：/settings, /settings/agents, /settings/knowledge, /settings/sources, /settings/models
JSON API：/api/settings, /api/settings/sources/*, /api/models/*（只读，数据来自 config.yaml）

页面用 ``render_template`` from ``web.config``（同 ``web/agent.py`` 模式）；
JSON API 从 ``app.state.agent_config`` 读取配置。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
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
    """列出所有模型配置（只读，来自 config.yaml agent.models 段）。"""
    cfg = getattr(request.app.state, "agent_config", None) or {}
    models = cfg.get("agent", {}).get("models", {})
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
