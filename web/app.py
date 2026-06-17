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
from news.constants import (
    TIER_LABELS, TIER_COLORS, TIER_BG,
    SENTIMENT_POSITIVE_THRESHOLD, SENTIMENT_NEGATIVE_THRESHOLD,
)

WEB_DIR = Path(__file__).parent
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"

# ── Jinja2 environment ───────────────────────────────────────────

env = Environment(
    loader=FileSystemLoader(TEMPLATES_DIR),
    autoescape=select_autoescape(["html"]),
)

# SVG icon map (v8 editorial — stroke-width 1.5, 22x22 nav, 24x24 others)
ICONS = {
    # ── Navigation (22x22, stroke-width 1.5) ──
    "home": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M4 10l8-7 8 7"/><path d="M6 9v10h4v-5h4v5h4V9"/></svg>',
    "flame": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M8.5 14.5A2.5 2.5 0 0 0 11 12c0-1.38-.5-2-1-3-1.072-2.143-.224-4.054 2-6 .5 2.5 2 4.9 4 6.5 2 1.6 3 3.5 3 5.5a7 7 0 1 1-14 0c0-1.153.433-2.294 1-3a2.5 2.5 0 0 0 2.5 2.5z"/></svg>',
    "chart-column": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="14" width="3" height="6" rx="0.5"/><rect x="8" y="10" width="3" height="10" rx="0.5"/><rect x="13" y="6" width="3" height="14" rx="0.5"/><rect x="18" y="12" width="3" height="8" rx="0.5"/><path d="M4.5 13l4.5-5 4.5 3 5-8" opacity="0.35"/></svg>',
    "trending-up": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M3 18l5-6 4 4 9-10"/><path d="M16 6h5v5"/></svg>',
    "file-text": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M6 3h10l5 5v13a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1z"/><path d="M16 3v5h5"/><path d="M8 12h5"/><path d="M8 16h8"/></svg>',
    "compass": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="6"/><circle cx="12" cy="12" r="2"/><line x1="12" y1="2" x2="12" y2="8"/><line x1="12" y1="16" x2="12" y2="22"/><line x1="2" y1="12" x2="8" y2="12"/><line x1="16" y1="12" x2="22" y2="12"/></svg>',
    "sliders": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="5" r="1.5"/><circle cx="12" cy="12" r="1.5"/><circle cx="12" cy="19" r="1.5"/><line x1="4" y1="5" x2="10.5" y2="5"/><line x1="13.5" y1="5" x2="20" y2="5"/><line x1="4" y1="12" x2="10.5" y2="12"/><line x1="13.5" y1="12" x2="20" y2="12"/><line x1="4" y1="19" x2="10.5" y2="19"/><line x1="13.5" y1="19" x2="20" y2="19"/></svg>',

    # ── Content / utility ──
    "fire": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M12 21c3.5-3.5 5-7 5-10a5 5 0 1 0-10 0c0 3 1.5 6.5 5 10z"/></svg>',
    "trending-up-lg": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M3 18l5-6 4 4 9-10"/><path d="M16 6h5v5"/></svg>',
    "clock": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/></svg>',
    "list": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 6h16"/><path d="M4 12h16"/><path d="M4 18h10"/></svg>',
    "x": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6L6 18"/><path d="M6 6l12 12"/></svg>',
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
        sentiment: str = Query(None),
        keyword: str = Query(None),
        date_from: str = Query(None),
        date_to: str = Query(None),
        page_size: int = Query(20, ge=10, le=50),
    ):
        """Hot news page — editorial masonry layout with date-first filtering."""
        per_page = page_size  # 使用用户选择的每页条数
        offset = (page - 1) * per_page
        tier_filter = tier if tier and tier > 0 else None

        # ── Date range defaults ──
        from datetime import date as date_type
        today_str = date_type.today().isoformat()
        show_all = request.query_params.get("all") == "1"
        if show_all:
            # "全部" preset — no date filter
            date_from = None
            date_to = None
        elif date_from is None and date_to is None:
            # First visit with no date params — default to today
            date_from = today_str
            date_to = today_str

        # ── Data ──
        articles = db.get_recent_news(
            limit=per_page, offset=offset,
            tier=tier_filter, sentiment=sentiment, keyword=keyword,
            date_from=date_from, date_to=date_to,
        )
        total = db.get_news_count(
            tier=tier_filter, sentiment=sentiment, keyword=keyword,
            date_from=date_from, date_to=date_to,
        )
        total_pages = max(1, (total + per_page - 1) // per_page)
        stats_data = db.get_stats(date_from=date_from, date_to=date_to)
        sentiment_counts = db.get_sentiment_counts(
            tier=tier_filter, keyword=keyword,
            date_from=date_from, date_to=date_to,
        )
        keyword_list = db.get_keyword_counts(
            tier=tier_filter, sentiment=sentiment,
            date_from=date_from, date_to=date_to,
        )
        high_impact = db.get_high_impact_count(
            tier=tier_filter, keyword=keyword,
            date_from=date_from, date_to=date_to,
        )


        tier_labels_with_counts = [
            {"tier": 0, "label": "全部", "count": stats_data["total_count"]},
            {"tier": 1, "label": "T1·核心", "count": stats_data["t1_count"]},
            {"tier": 2, "label": "T2·重要", "count": stats_data["t2_count"]},
            {"tier": 3, "label": "T3·关注", "count": stats_data["t3_count"]},
            {"tier": 4, "label": "T4·参考", "count": stats_data["t4_count"]},
        ]

        # ── Sentiment toggles with counts ──
        sentiment_toggles = [
            {"value": "positive", "label": "利好", "css_class": "positive",
             "count": sentiment_counts["positive"]},
            {"value": "negative", "label": "利空", "css_class": "negative",
             "count": sentiment_counts["negative"]},
            {"value": "neutral", "label": "中性", "css_class": "neutral",
             "count": sentiment_counts["neutral"]},
        ]

        # ── Sentiment bar percentages ──
        sent_total = sum(sentiment_counts.values()) or 1
        sentiment_pct = {
            "negative_pct": round(sentiment_counts["negative"] / sent_total * 100, 1),
            "neutral_pct": round(sentiment_counts["neutral"] / sent_total * 100, 1),
            "positive_pct": round(sentiment_counts["positive"] / sent_total * 100, 1),
        }

        # ── Active filters ──
        active_filters = []
        if tier_filter:
            active_filters.append({
                "label": tier_labels_with_counts[tier_filter]["label"],
                "type": "tier",
                "remove_url": _remove_filter(request, "tier"),
            })
        if sentiment:
            label_map = {"positive": "利好", "negative": "利空", "neutral": "中性"}
            active_filters.append({
                "label": label_map.get(sentiment, sentiment),
                "type": "sentiment",
                "remove_url": _remove_filter(request, "sentiment"),
            })
        if keyword:
            active_filters.append({
                "label": keyword,
                "type": "keyword",
                "remove_url": _remove_filter(request, "keyword"),
            })

        # ── Card transform ──
        def _to_card(article: dict) -> dict:
            score = article.get("sentiment_score")
            if score is not None and score >= SENTIMENT_POSITIVE_THRESHOLD:
                sentiment_label, sentiment_class = "利好", "positive"
            elif score is not None and score <= SENTIMENT_NEGATIVE_THRESHOLD:
                sentiment_label, sentiment_class = "利空", "negative"
            else:
                sentiment_label, sentiment_class = "中性", "neutral"
            article_tier = article.get("tier") or 4
            tier_class = f"t{article_tier}" if article_tier >= 2 else ""
            return {
                "id": article.get("id"),
                "source": article.get("source_name", ""),
                "tier_class": tier_class,
                "heat": article.get("heat_score") or 0,
                "title": article.get("title", ""),
                "summary": article.get("summary", ""),
                "sentiment": sentiment_label,
                "sentiment_class": sentiment_class,
                "keywords": article.get("tags") or [],
                "time": _relative_time(article.get("published_at")),
            }

        masonry_cards = [_to_card(a) for a in articles]
        page_numbers = _build_page_numbers(page, total_pages)

        html = render_template(
            "pages/hot_news.html",
            active_page="hot-news",
            # Stats
            today_hot=stats_data["today_count"],
            positive_signal=sentiment_counts["positive"],
            high_impact=high_impact,
            sentiment_counts=sentiment_counts,
            sentiment_pct=sentiment_pct,
            # Filters
            tier_labels=tier_labels_with_counts,
            sentiment_toggles=sentiment_toggles,
            keyword_list=keyword_list,
            active_filters=active_filters,
            current_tier=tier_filter,
            current_sentiment=sentiment,
            current_keyword=keyword,
            # Content
            masonry_cards=masonry_cards,
            total_count=total,
            total_pages=total_pages,
            current_page=page,
            page_numbers=page_numbers,
            # Date
            current_date_from=date_from,
            current_date_to=date_to,
            today_date=today_str,
            # Pagination
            current_page_size=per_page,
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

def _remove_filter(request, key: str) -> str:
    """Return a URL with the given query param removed, preserving others."""
    from urllib.parse import urlencode
    params = dict(request.query_params)
    params.pop(key, None)
    params["page"] = "1"
    # Clean up empty params
    for k in ("tier", "sentiment", "keyword", "date_from", "date_to"):
        if not params.get(k):
            params.pop(k, None)
    qs = urlencode(params) if params else ""
    base = str(request.url).split("?")[0]
    return f"{base}?{qs}" if qs else base


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
