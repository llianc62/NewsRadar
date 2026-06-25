"""NewsRadar Web Frontend — FastAPI + Jinja2 SSR."""

import json
import re
import time
import asyncio
import threading
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import mistune
from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

from storage.files import S3Storage
from news.crawler import Crawler, OutputStyle
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

    # ── Notification ──
    "bell": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/></svg>',
    "refresh": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/><path d="M8 16H3v5"/></svg>',
    "check-circle": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M20 6L9 17l-5-5"/></svg>',
    "x-circle": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M18 6L6 18M6 6l12 12"/></svg>',
    "arrow-up": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 16a1 1 0 0 0 1-1v-2a1 1 0 0 1 1-1h3.293a.707.707 0 0 0 .5-1.207l-6.939-6.939a1.207 1.207 0 0 0-1.708 0l-6.94 6.94a.707.707 0 0 0 .5 1.206H8a1 1 0 0 1 1 1v2a1 1 0 0 0 1 1z"/><path d="M9 20h6"/></svg>',
    "trash": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10 11v6"/><path d="M14 11v6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M3 6h18"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>',
    "alert-triangle": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>',
    "gauge": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m12 14 4-4"/><path d="M3.34 19a10 10 0 1 1 17.32 0"/></svg>',
}
# ── Refetch state (in-memory) ─────────────────────────────────────

_refetch_tasks: dict[int, dict] = {}       # key=article_id, 去重+状态跟踪
_notifications: list[dict] = []            # 通知列表，最多 50 条
_notification_counter: int = 0             # 自增 ID
_notification_lock = threading.Lock()      # 线程安全
_refetch_executor: ThreadPoolExecutor | None = None

# ── SSE state ──
_sse_clients: set["asyncio.Queue"] = set()
_sse_clients_lock = threading.Lock()
_sse_event_loop: "asyncio.AbstractEventLoop | None" = None


def _now() -> float:
    return time.time()


def _add_notification(
    article_id: int,
    title: str,
    status: str = "pending",
    error_message: str = "",
    category: str = "fetch",
    summary: str = "",
) -> dict:
    """Create a notification, append to list, return the dict."""
    global _notification_counter
    with _notification_lock:
        _notification_counter += 1
        notif = {
            "id": _notification_counter,
            "category": category,
            "article_id": article_id,
            "title": title,
            "summary": summary,
            "status": status,
            "error_message": error_message,
            "is_read": False,
            "created_at": _now(),
        }
        _notifications.insert(0, notif)
        # Cap at 50
        if len(_notifications) > 50:
            _notifications.pop()
    # Push SSE event outside the lock to keep lock scope tight
    _push_sse_event({"type": "new", "notification": dict(notif)})
    return notif


def _push_sse_event(data: dict) -> None:
    """Push an SSE event to all connected clients. Thread-safe."""
    loop = _sse_event_loop
    if loop is None or not _sse_clients:
        return

    def _put():
        with _sse_clients_lock:
            clients = list(_sse_clients)
        for q in clients:
            try:
                q.put_nowait(data)
            except asyncio.QueueFull:
                pass

    try:
        running = asyncio.get_running_loop()
        if running is loop:
            _put()
        else:
            loop.call_soon_threadsafe(_put)
    except RuntimeError:
        # No running loop — called from thread pool thread
        loop.call_soon_threadsafe(_put)


def _run_fetch_url(url: str, crawler, notif: dict, db) -> None:
    """Execute URL fetch in background — thin wrapper around crawler.fetch()."""
    try:
        notif["status"] = "running"
        _push_sse_event({"type": "update", "notification": dict(notif)})
        crawler.fetch(url, OutputStyle.POSTGRESQL, True, True)
        notif["status"] = "completed"
        # 回填 article_id
        article = db.get_article_by_url(url)
        if article:
            notif["article_id"] = article["id"]
    except Exception as e:
        notif["status"] = "failed"
        notif["error_message"] = str(e)[:500]
    finally:
        _push_sse_event({"type": "update", "notification": dict(notif)})


def _run_refetch(article_id: int, crawler, notif: dict, db) -> None:
    """Execute refetch in background — re-download content and persist."""
    try:
        notif["status"] = "running"
        _push_sse_event({"type": "update", "notification": dict(notif)})
        article = db.get_news_by_id(article_id)
        if article is None:
            raise ValueError("文章不存在")

        # 清空 content 以触发 _run_batch_parse 重新下载解析
        # （enrich_content 跳过已有 content 的条目）
        article["content"] = ""

        crawler.enrich_content(article, with_image=True)

        # update_article_full 内部会规范 published_at 为 datetime/None，
        # 无需在此做类型转换
        db.update_article_full(
            article_id,
            tags=article.get("tags", []),
            author=article.get("author", ""),
            summary=article.get("summary", ""),
            category=article.get("category", ""),
            content=article.get("content", ""),
            published_at=article.get("published_at", ""),
        )
        notif["status"] = "completed"
    except Exception as e:
        notif["status"] = "failed"
        notif["error_message"] = str(e)[:500]
    finally:
        _push_sse_event({"type": "update", "notification": dict(notif)})
        _refetch_tasks.pop(article_id, None)


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

def create_app(db, s3_config: dict, queues: dict = None, crawler=None):
    """Create and configure the FastAPI application.

    Args:
        db: A connected :class:`storage.postgres.PostgreSQL` instance.
            It is stored in ``app.state.db`` for route handlers.
        s3_config: S3 config dict for the ``/media/`` image proxy
            (required). Keys: endpoint_url, bucket_name,
            access_key_id, secret_access_key, region.
        queues: Optional dict of ``asyncio.Queue`` for manual trigger +
                notification callback. Keys: ``"crawl"``, ``"sync"``.
        crawler: Optional :class:`news.crawler.Crawler` instance for
            refetch API. If ``None``, the refetch endpoint will reject
            requests with "抓取服务未就绪".

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

    # ── Cache-Control middleware for static assets ─────────────────
    # Without explicit Cache-Control, browsers may re-download static
    # files on every navigation.  Fonts (immutable by filename) get
    # 1-year cache; CSS/JS get 24-hour cache.
    @app.middleware("http")
    async def cache_static(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/static/"):
            if request.url.path.endswith(".woff2"):
                response.headers["Cache-Control"] = (
                    "public, max-age=31536000, immutable"
                )
            else:
                response.headers["Cache-Control"] = "public, max-age=86400"
        return response
    app.state.queues = queues or {}

    # S3 storage — required for /media/ proxy
    app.state.s3_storage = S3Storage(s3_config)

    # Refetch state
    global _refetch_executor
    if _refetch_executor is None or crawler is not None:
        if _refetch_executor is not None:
            _refetch_executor.shutdown(wait=False)
        _refetch_executor = ThreadPoolExecutor(max_workers=10)
    app.state.crawler = crawler

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
        keyword: List[str] = Query(None),
        search: str = Query(None),
        date_from: str = Query(None),
        date_to: str = Query(None),
        page_size: int = Query(20, ge=10, le=50),
    ):
        """Hot news page — editorial masonry layout with date-first filtering."""
        per_page = page_size  # 使用用户选择的每页条数
        offset = (page - 1) * per_page
        tier_filter = tier if tier and tier > 0 else None
        keyword = keyword or []  # normalize None → [] for template iteration

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
            tier=tier_filter, sentiment=sentiment, keywords=keyword,
            search=search,
            date_from=date_from, date_to=date_to,
        )
        total = db.get_news_count(
            tier=tier_filter, sentiment=sentiment, keywords=keyword,
            search=search,
            date_from=date_from, date_to=date_to,
        )
        total_pages = max(1, (total + per_page - 1) // per_page)
        # 统计类查询不传 search 参数，保持筛选组件数值稳定
        stats_data = db.get_stats(date_from=date_from, date_to=date_to)
        sentiment_counts = db.get_sentiment_counts(
            tier=tier_filter, keywords=keyword,
            date_from=date_from, date_to=date_to,
        )
        keyword_list = db.get_keyword_counts(
            tier=tier_filter, sentiment=sentiment,
            date_from=date_from, date_to=date_to,
        )
        high_impact = db.get_high_impact_count(
            tier=tier_filter, keywords=keyword,
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
            for kw in keyword:
                active_filters.append({
                    "label": kw,
                    "type": "keyword",
                    "remove_url": _remove_filter(request, "keyword", kw),
                })
        if search:
            active_filters.append({
                "label": f"搜索: {search}",
                "type": "search",
                "remove_url": _remove_filter(request, "search"),
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
                "time": _relative_time(article.get("created_at")),
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
            current_keywords=keyword,
            current_search=search,
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

    def _resolve_image_paths(content: str, updated_at) -> str:
        """将 content 中的 ``images/xxx`` 替换为 ``/media/news/YYYY-MM-DD/images/xxx``。

        日期从 *updated_at* 提取，与 crawler.py 图片保存路径（当天日期）保持一致；
        若为空则回退到当天日期。
        """
        if not content or "images/" not in content:
            return content
        if updated_at:
            if hasattr(updated_at, "strftime"):
                date_str = updated_at.strftime("%Y-%m-%d")
            else:
                date_str = str(updated_at)[:10]
        else:
            from datetime import date as date_type
            date_str = date_type.today().isoformat()
        media_prefix = f"/media/news/{date_str}/images/"
        return content.replace("images/", media_prefix)

    @app.get("/news/{article_id}", response_class=HTMLResponse)
    async def news_detail(request: Request, article_id: int):
        """Single news article detail page."""
        article = db.get_news_by_id(article_id)
        if article is None:
            return HTMLResponse("<h1>404 Not Found</h1>", status_code=404)
        # 正文中的 H1 标题与页面 title 重复，在渲染前移除
        if article.get("content"):
            article["content"] = re.sub(r"^# .+?\n\n?", "", article["content"], count=1)
            article["content"] = _resolve_image_paths(
                article["content"], article.get("updated_at"),
            )
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
        """Manually trigger a crawl job.  Returns 409 if a crawl is already running."""
        queue = app.state.queues.get("crawl")
        if queue is None:
            return JSONResponse({"ok": False, "error": "not available"}, status_code=404)

        lock = app.state.queues.get("crawl_lock")
        if lock and lock.locked():
            return JSONResponse({"ok": False, "error": "已有抓取任务正在执行"}, status_code=409)

        notif = _add_notification(0, "新闻抓取", "running", category="crawl")

        def on_complete(success: bool, summary: str):
            with _notification_lock:
                notif["status"] = "completed" if success else "failed"
                notif["summary"] = summary
            _push_sse_event({"type": "update", "notification": dict(notif)})

        await queue.put(on_complete)
        return {"ok": True, "task": "crawl", "notif_id": notif["id"]}

    @app.post("/api/trigger/sync")
    async def trigger_sync():
        """Manually trigger a cloud sync job.  Returns 409 if a sync is already running."""
        queue = app.state.queues.get("sync")
        if queue is None:
            return JSONResponse({"ok": False, "error": "not available"}, status_code=404)

        lock = app.state.queues.get("sync_lock")
        if lock and lock.locked():
            return JSONResponse({"ok": False, "error": "已有同步任务正在执行"}, status_code=409)

        notif = _add_notification(0, "云端同步", "running", category="sync")

        def on_complete(success: bool, summary: str):
            with _notification_lock:
                notif["status"] = "completed" if success else "failed"
                notif["summary"] = summary
            _push_sse_event({"type": "update", "notification": dict(notif)})

        await queue.put(on_complete)
        return {"ok": True, "task": "sync", "notif_id": notif["id"]}

    # ── SSE stream ──────────────────────────────────────────────

    @app.get("/api/notifications/stream")
    async def notification_stream(request: Request):
        """SSE endpoint — pushes new/updated notifications to the client."""
        from starlette.responses import StreamingResponse

        global _sse_event_loop
        if _sse_event_loop is None:
            _sse_event_loop = asyncio.get_running_loop()

        queue: asyncio.Queue = asyncio.Queue()
        with _sse_clients_lock:
            _sse_clients.add(queue)

        async def event_generator():
            try:
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        data = await asyncio.wait_for(queue.get(), timeout=30.0)
                        event_type = data.get("type", "message")
                        yield f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                with _sse_clients_lock:
                    _sse_clients.discard(queue)

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # ── Manual Fetch API ───────────────────────────────────────

    @app.post("/api/news/fetch")
    async def fetch_news_by_url(request: Request):
        """Submit a URL for background fetch — dedup by URL, refetch if exists."""
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(
                {"ok": False, "error": "请求体必须为 JSON"}, status_code=400
            )
        url = (body.get("url") or "").strip()
        if not url:
            return JSONResponse(
                {"ok": False, "error": "URL 不能为空"}, status_code=400
            )
        if not (url.startswith("http://") or url.startswith("https://")):
            return JSONResponse(
                {"ok": False, "error": "URL 必须以 http:// 或 https:// 开头"},
                status_code=400,
            )

        crawler = app.state.crawler
        if crawler is None:
            return JSONResponse(
                {"ok": False, "error": "抓取服务未就绪"}, status_code=503
            )

        # ── Dedup: if URL already exists, refetch instead ──
        existing = db.get_article_by_url(url)
        if existing:
            article_id = existing["id"]
            title = existing.get("title") or url
            with _notification_lock:
                dup = _refetch_tasks.get(article_id)
                if dup and dup["status"] in ("pending", "running"):
                    return {"ok": False, "error": "该文章正在抓取中"}
            notif = _add_notification(article_id, title, status="pending")
            task = {"article_id": article_id, "title": title,
                    "status": "pending", "created_at": notif["created_at"]}
            with _notification_lock:
                _refetch_tasks[article_id] = task
            _refetch_executor.submit(_run_refetch, article_id, crawler, notif, db)
            return {"ok": True, "refetch": True, "article_id": article_id}

        # ── New URL: fetch and insert ──
        notif = _add_notification(0, url, status="pending")
        _refetch_executor.submit(_run_fetch_url, url, crawler, notif, db)

        return {"ok": True, "message": "已提交抓取任务"}

    # ── Refetch API ────────────────────────────────────────────

    @app.post("/api/news/{article_id}/refetch")
    async def refetch_article(article_id: int):
        """Submit a background refetch job for the given article."""
        article = db.get_news_by_id(article_id)
        if article is None:
            return JSONResponse({"ok": False, "error": "文章不存在"}, status_code=404)
        url = (article.get("url") or "").strip()
        title = article.get("title") or ""
        if not url:
            return {"ok": False, "error": "该文章没有原文链接"}

        # Dedup (under lock)
        with _notification_lock:
            existing = _refetch_tasks.get(article_id)
            if existing and existing["status"] in ("pending", "running"):
                return {"ok": False, "error": "该文章正在抓取中"}

        crawler = app.state.crawler
        if crawler is None:
            return {"ok": False, "error": "抓取服务未就绪"}

        # All validations passed — create notification + task
        notif = _add_notification(article_id, title, status="pending")
        task = {"article_id": article_id, "title": title,
                "status": "pending", "created_at": notif["created_at"]}
        with _notification_lock:
            _refetch_tasks[article_id] = task

        _refetch_executor.submit(_run_refetch, article_id, crawler, notif, db)
        return {"ok": True, "task": task}

    @app.post("/api/news/{article_id}/sentiment-score")
    async def set_user_sentiment_score(article_id: int, request: Request):
        """Set user sentiment score for an article.

        Accepts JSON ``{"score": <int>}`` where score is one of 0, 30, 60, 80, 100.
        """
        ALLOWED_SCORES = {0, 30, 60, 80, 100}
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(
                {"ok": False, "error": "无效的请求体"}, status_code=400,
            )
        score = body.get("score")
        if score not in ALLOWED_SCORES:
            return JSONResponse(
                {"ok": False, "error": f"分数必须是 {sorted(ALLOWED_SCORES)} 之一"},
                status_code=400,
            )
        updated = db.set_sentiment_score(article_id, score)
        if not updated:
            return JSONResponse(
                {"ok": False, "error": "文章不存在"}, status_code=404,
            )
        return {"ok": True, "score": score}

    @app.delete("/api/news/{article_id}")
    async def delete_article(article_id: int):
        """Delete an article and its images (cascade).

        Returns 404 if no article has that ID; 200 with ``{ok: True}`` on
        success. In-memory refetch state for the article is also cleared.
        """
        article = db.get_news_by_id(article_id)
        if article is None:
            return JSONResponse({"ok": False, "error": "文章不存在"}, status_code=404)
        deleted = db.delete_news(article_id)
        if not deleted:
            return JSONResponse({"ok": False, "error": "文章不存在"}, status_code=404)
        # Drop any lingering refetch task so it cannot fire against a gone row
        with _notification_lock:
            _refetch_tasks.pop(article_id, None)
        return {"ok": True}

    @app.get("/api/notifications")
    async def list_notifications(unread_only: bool = Query(False)):
        """Return notification list, optionally filtered to unread only."""
        with _notification_lock:
            result = [dict(n) for n in _notifications]
        # 回填 article_id：新 URL 抓取的通知 title 就是 URL
        for n in result:
            if n.get("article_id", 0) == 0 and n.get("title", "").startswith("http"):
                article = db.get_article_by_url(n["title"])
                if article:
                    n["article_id"] = article["id"]
        if unread_only:
            result = [n for n in result if not n.get("is_read")]
        return result

    @app.get("/api/notifications/unread-count")
    async def unread_notification_count():
        """Return the count of unread notifications."""
        with _notification_lock:
            count = sum(1 for n in _notifications if not n.get("is_read"))
        return {"count": count}

    @app.post("/api/notifications/{notif_id}/read")
    async def mark_notification_read(notif_id: int):
        """Mark a single notification as read."""
        with _notification_lock:
            for n in _notifications:
                if n["id"] == notif_id:
                    n["is_read"] = True
                    return {"ok": True}
        return JSONResponse({"ok": False, "error": "通知不存在"}, status_code=404)

    @app.post("/api/notifications/mark-all-read")
    async def mark_all_read():
        """Mark all notifications as read."""
        with _notification_lock:
            for n in _notifications:
                n["is_read"] = True
        return {"ok": True}

    return app


# ── Helpers ──────────────────────────────────────────────────────

def _remove_filter(request, key: str, value: Optional[str] = None) -> str:
    """Return a URL with the given query param removed, preserving others.

    When *value* is given (multi-value params like ``keyword``), only that
    specific value is removed; other values for the same key are kept.
    """
    from urllib.parse import urlencode

    params: list[tuple[str, str]] = []
    for k, v in request.query_params.multi_items():
        if k == key and value is not None:
            if v == value:
                continue  # skip this specific value
        elif k == key:
            continue  # skip all values for this key
        params.append((k, v))
    params.append(("page", "1"))
    # Clean up empty params
    non_empty = [(k, v) for k, v in params if v]
    qs = urlencode(non_empty) if non_empty else ""
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
