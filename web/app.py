"""NewsRadar Web Frontend — FastAPI + Jinja2 SSR."""

from contextlib import asynccontextmanager
from pathlib import Path

import mistune
from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

from storage.files import S3Storage
from news.constants import TIER_LABELS, TIER_COLORS, TIER_BG

WEB_DIR = Path(__file__).parent
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"

# ── Jinja2 environment ───────────────────────────────────────────

env = Environment(
    loader=FileSystemLoader(TEMPLATES_DIR),
    autoescape=select_autoescape(["html"]),
)

# Lucide icon SVG map (16x16, stroke-width 2)
ICONS = {
    "chart-column": '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 20V8"/><path d="M10 20V4"/><path d="M14 20V12"/><path d="M18 20V16"/></svg>',
    "flame": '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M8.5 14.5A2.5 2.5 0 0 0 11 12c0-1.38-.5-2-1-3-1.072-2.143-.224-4.054 2-6 .5 2.5 2 4.9 4 6.5 2 1.6 3 3.5 3 5.5a7 7 0 1 1-14 0c0-1.153.433-2.294 1-3a3.5 3.5 0 0 0 2.5 2.5z"/></svg>',
    "briefcase-business": '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 12h.01"/><path d="M16 6V4a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v2"/><path d="M22 13a19 19 0 0 0-20 0"/><rect x="2" y="6" width="20" height="14" rx="2"/><rect x="6" y="12" width="12" height="6"/></svg>',
    "trending-up": '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="22 7 13.5 15.5 8.5 10.5 2 17"/><polyline points="16 7 22 7 22 13"/></svg>',
    "file-text": '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v4a2 2 0 0 0 2 2h4"/><path d="M10 9H8"/><path d="M16 13H8"/><path d="M16 17H8"/></svg>',
    "clock": '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>',
    "sliders-horizontal": '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="4" y1="21" x2="4" y2="14"/><line x1="4" y1="10" x2="4" y2="3"/><line x1="12" y1="21" x2="12" y2="12"/><line x1="12" y1="8" x2="12" y2="3"/><line x1="20" y1="21" x2="20" y2="16"/><line x1="20" y1="12" x2="20" y2="3"/><line x1="2" y1="14" x2="6" y2="14"/><line x1="10" y1="8" x2="14" y2="8"/><line x1="18" y1="16" x2="22" y2="16"/></svg>',
    "newspaper": '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2"/><line x1="9" y1="9" x2="15" y2="9"/><line x1="9" y1="13" x2="15" y2="13"/><line x1="9" y1="17" x2="12" y2="17"/></svg>',
    "star": '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/></svg>',
    "trending-up-lg": '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="22 7 13.5 15.5 8.5 10.5 2 17"/><polyline points="16 7 22 7 22 13"/></svg>',
}
# Markdown-to-HTML converter (GitHub Flavoured Markdown)
_md_renderer = mistune.create_markdown(
    escape=False,  # allow raw HTML in source (some articles need it)
    plugins=["strikethrough", "footnotes", "table", "task_lists"],
)


def _markdown_filter(text: str) -> Markup:
    """Jinja2 filter: convert Markdown text to safe HTML."""
    if not text:
        return Markup("")
    return Markup(_md_renderer(text))


env.filters["markdown"] = _markdown_filter
env.globals["icon_svg"] = lambda name: ICONS.get(name, "")
env.globals["len"] = len


def render_template(name: str, **context) -> str:
    """Render a Jinja2 template."""
    template = env.get_template(name)
    return template.render(**context)


# ── App factory ──────────────────────────────────────────────────

def create_app(db, s3_config: dict, signals: dict = None):
    """Create and configure the FastAPI application.

    Args:
        db: A connected :class:`storage.postgres.PostgreSQL` instance.
            It is stored in ``app.state.db`` for route handlers.
        s3_config: S3 config dict for the ``/media/`` image proxy
            (required). Keys: endpoint_url, bucket_name,
            access_key_id, secret_access_key, region.
        signals: Optional dict of ``asyncio.Event`` signals for manual
            task triggering via ``POST /api/trigger/{name}``.
            Keys: ``"crawl"``, ``"sync"``.

    Returns:
        Configured FastAPI application.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """Startup / shutdown lifecycle."""
        if not db.is_connected:
            db.connect()
            db.init_schema()
        print("[Web] Database ready")
        yield
        db.close()
        print("[Web] Database closed")

    app = FastAPI(title="NewsRadar", version="2.0.0", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.state.db = db
    app.state.signals = signals or {}

    # S3 storage — required for /media/ proxy
    app.state.s3_storage = S3Storage(s3_config)

    # ── Routes ───────────────────────────────────────────────────

    @app.get("/", response_class=HTMLResponse)
    async def market_overview(request: Request):
        """Market overview page with real stats."""
        stats = db.get_stats()

        index_cards = [
            {"name": "T1·官媒", "value": str(stats["t1_count"]), "change": None},
            {"name": "T2·主流", "value": str(stats["t2_count"]), "change": None},
            {"name": "T3·垂直", "value": str(stats["t3_count"]), "change": None},
            {"name": "T4·资讯", "value": str(stats["t4_count"]), "change": None},
            {"name": "总计新闻", "value": str(stats["total_count"]), "change": None},
            {"name": "今日新增", "value": str(stats["today_count"]), "change": None},
        ]

        hot_sources = [
            {"name": s["source_name"], "count": s["cnt"]}
            for s in stats["by_source"][:8]
        ]

        html = render_template(
            "pages/market_overview.html",
            active_page="home",
            index_cards=index_cards,
            hot_sources=hot_sources,
        )
        return HTMLResponse(html)

    @app.get("/hot-news", response_class=HTMLResponse)
    async def hot_news(
        request: Request,
        page: int = Query(1, ge=1),
        tier: int = Query(None, ge=0, le=4),
    ):
        """Hot news page with real data from PostgreSQL."""
        per_page = 10
        offset = (page - 1) * per_page

        articles = db.get_recent_news(
            limit=per_page, offset=offset,
            tier=tier if tier and tier > 0 else None,
        )
        total = db.get_news_count(tier=tier if tier and tier > 0 else None)
        total_pages = max(1, (total + per_page - 1) // per_page)
        stats_data = db.get_stats()

        stats = [
            {"label": "今日新增", "value": str(stats_data["today_count"]), "icon": "flame",
             "bg": "hsl(var(--primary) / 0.1)", "color": "hsl(var(--foreground))"},
            {"label": "新闻来源", "value": str(len(stats_data["by_source"])), "icon": "newspaper",
             "bg": "hsl(var(--info) / 0.1)", "color": "hsl(var(--foreground))"},
            {"label": "总计", "value": str(stats_data["total_count"]), "icon": "star",
             "bg": "hsl(var(--warning) / 0.1)", "color": "hsl(var(--foreground))"},
            {"label": "T1·官媒", "value": str(stats_data["t1_count"]), "icon": "trending-up-lg",
             "bg": "hsl(var(--danger) / 0.1)", "color": "hsl(var(--danger))"},
        ]

        tier_labels = [
            {"tier": t, "label": f"T{t}·{TIER_LABELS[t].split('·')[1]}", "color": c, "bg": TIER_BG[t]}
            for t, c in TIER_COLORS.items()
        ]

        def _to_card(article: dict) -> dict:
            score = article.get("sentiment_score")
            if score is not None and score >= 67:
                sentiment, s_bg, s_color = "利好", "hsl(var(--danger) / 0.1)", "hsl(var(--danger))"
            elif score is not None and score <= 33:
                sentiment, s_bg, s_color = "利空", "hsl(var(--success) / 0.1)", "hsl(var(--success))"
            else:
                sentiment, s_bg, s_color = "中性", "hsl(var(--warning) / 0.1)", "hsl(var(--warning))"
            return {
                "id": article.get("id"),
                "sentiment": sentiment,
                "sentiment_bg": s_bg,
                "sentiment_color": s_color,
                "source": article.get("source_name", ""),
                "time": _relative_time(article.get("published_at")),
                "heat": str(article.get("heat_score") or 0),
                "title": article.get("title", ""),
                "summary": article.get("summary", ""),
                "keywords": [{"text": t, "primary": i == 0} for i, t in enumerate(article.get("tags") or [])],
            }

        def _to_list_item(article: dict, seq: int) -> dict:
            return {"id": article.get("id"), "seq": seq, "title": article.get("title", ""), "source": article.get("source_name", "")}

        tier1_cards = [_to_card(a) for a in articles]
        list_items = [_to_list_item(a, offset + i + 1) for i, a in enumerate(articles)]
        page_numbers = _build_page_numbers(page, total_pages)

        html = render_template(
            "pages/hot_news.html",
            active_page="hot-news",
            stats=stats,
            tier_labels=tier_labels,
            keywords=["央行", "AI", "港股", "外资", "芯片", "新能源"],
            tier1_cards=tier1_cards,
            list_items=list_items,
            total_count=total,
            total_pages=total_pages,
            page_start=offset + 1,
            page_end=min(offset + per_page, total),
            current_page=page,
            tier_filter=tier if tier and tier > 0 else None,
            page_numbers=page_numbers,
        )
        return HTMLResponse(html)

    @app.get("/news/{article_id}", response_class=HTMLResponse)
    async def news_detail(request: Request, article_id: int):
        """Single news article detail page."""
        article = db.get_news_by_id(article_id)
        if article is None:
            return HTMLResponse("<h1>404 Not Found</h1>", status_code=404)
        html = render_template("pages/news_detail.html", active_page="hot-news", article=article)
        return HTMLResponse(html)

    # ── Media proxy (S3 presigned redirect) ──────────────────────

    @app.get("/media/{path:path}")
    async def media_proxy(path: str):
        """Proxy S3 object access via presigned redirect.

        Takes an S3 object key as *path*, generates a short-lived
        presigned GET URL (1 hour), and redirects.  This keeps
        stored content URLs stable while bucket access stays private.
        """
        from fastapi.responses import RedirectResponse

        storage = app.state.s3_storage
        url = storage.get(path, expires_in=3600)
        return RedirectResponse(url=url)

    # ── Manual trigger API ───────────────────────────────────────

    @app.post("/api/trigger/crawl")
    async def trigger_crawl():
        """Manually trigger a crawl job."""
        signal = app.state.signals.get("crawl")
        if signal is None:
            return JSONResponse({"ok": False, "error": "not available"}, status_code=404)
        signal.set()
        return {"ok": True, "task": "crawl"}

    @app.post("/api/trigger/sync")
    async def trigger_sync():
        """Manually trigger a cloud sync job."""
        signal = app.state.signals.get("sync")
        if signal is None:
            return JSONResponse({"ok": False, "error": "not available"}, status_code=404)
        signal.set()
        return {"ok": True, "task": "sync"}

    return app


# ── Helpers ──────────────────────────────────────────────────────

def _relative_time(dt) -> str:
    """Convert datetime to relative time string like '2h前'."""
    if dt is None:
        return ""
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    diff = now - dt
    if diff < timedelta(minutes=1):
        return "刚刚"
    elif diff < timedelta(hours=1):
        return f"{int(diff.total_seconds() // 60)}min前"
    elif diff < timedelta(days=1):
        return f"{int(diff.total_seconds() // 3600)}h前"
    else:
        return f"{diff.days}d前"


def _build_page_numbers(current: int, total: int) -> list:
    """Build page number list like [1, 2, 3, '...', 8]."""
    if total <= 7:
        return list(range(1, total + 1))
    pages = [1, 2, 3]
    if current > 4:
        pages.append("...")
    for p in range(max(4, current - 1), min(total - 2, current + 2) + 1):
        if p not in pages:
            pages.append(p)
    if current < total - 3:
        pages.append("...")
    for p in [total - 2, total - 1, total]:
        if p not in pages:
            pages.append(p)
    return pages
