# coding=utf-8
"""NewsRadar news routes — APIRouter for news pages, API endpoints, SSE, notifications.

Depends on:
- web.config          → render_template
- web.notification    → NotificationState class
- web.background      → BackgroundTaskRunner class
- news.constants      → TIER_LABELS, TIER_COLORS, TIER_BG, SENTIMENT_* thresholds
- utils               → format_date_today
- storage.files       → FileStorage, LocalStorage, S3Storage

Module-level state (``_refetch_tasks``, ``_refetch_executor``) has been removed.
All state lives on ``request.app.state`` — set up by ``web.app.create_app()``.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import date as date_type
from datetime import datetime
from typing import Any, List, Optional

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from web.config import render_template
from news.constants import (
    SENTIMENT_NEGATIVE_THRESHOLD,
    SENTIMENT_POSITIVE_THRESHOLD,
    TIER_BG,
    TIER_COLORS,
    TIER_LABELS,
)
from storage.files import FileStorage, LocalStorage, S3Storage
from utils import format_date_today

router = APIRouter()


# ═══════════════════════════════════════════════════════════════════
# Helper functions
# ═══════════════════════════════════════════════════════════════════


def _resolve_image_paths(content: str, updated_at) -> str:
    """Replace ``images/`` with ``/media/news/YYYY-MM-DD/images/``.

    The date is extracted from *updated_at*, matching crawler.py's
    image save path (current day); falls back to today when empty.
    """
    if not content or "images/" not in content:
        return content
    if updated_at:
        if hasattr(updated_at, "strftime"):
            date_str = updated_at.strftime("%Y-%m-%d")
        else:
            date_str = str(updated_at)[:10]
    else:
        date_str = date_type.today().isoformat()
    media_prefix = f"/media/news/{date_str}/images/"
    return content.replace("images/", media_prefix)


def _remove_filter(request: Request, key: str, value: Optional[str] = None) -> str:
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
    non_empty = [(k, v) for k, v in params if v]
    qs = urlencode(non_empty) if non_empty else ""
    base = str(request.url).split("?")[0]
    return f"{base}?{qs}" if qs else base


def _relative_time(dt) -> str:
    """Convert datetime to relative time string like '2h前'."""
    if dt is None:
        return ""
    from datetime import timezone, timedelta
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


# ═══════════════════════════════════════════════════════════════════
# Background task functions (run in executor threads)
# ═══════════════════════════════════════════════════════════════════


def _run_fetch_url(url: str, crawler, notif: dict, db, ns) -> None:
    """Execute URL fetch in background — thin wrapper around crawler.fetch()."""
    try:
        notif["status"] = "running"
        ns.push_sse_event({"type": "update", "notification": dict(notif)})
        crawler.fetch(url, 1, True, True)  # OutputStyle.POSTGRESQL = 1
        notif["status"] = "completed"
        article = db.get_article_by_url(url)
        if article:
            notif["article_id"] = article["id"]
    except Exception as e:
        notif["status"] = "failed"
        notif["error_message"] = str(e)[:500]
    finally:
        ns.push_sse_event({"type": "update", "notification": dict(notif)})


def _run_refetch(article_id: int, crawler, notif: dict, db, ns) -> None:
    """Execute refetch in background — re-download content and persist."""
    try:
        notif["status"] = "running"
        ns.push_sse_event({"type": "update", "notification": dict(notif)})
        article = db.get_news_by_id(article_id)
        if article is None:
            raise ValueError("文章不存在")

        article["content"] = ""
        crawler.enrich_content(article, with_image=True)

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
        ns.push_sse_event({"type": "update", "notification": dict(notif)})


# ═══════════════════════════════════════════════════════════════════
# Routes
# ═══════════════════════════════════════════════════════════════════


@router.get("/", response_class=HTMLResponse)
async def market_overview(request: Request):
    """Market overview page with real stats."""
    db = request.app.state.db
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

    today_str = format_date_today()
    keyword_counts = db.get_keyword_counts(date_from=today_str, limit=40)

    html = render_template(
        "pages/market_overview.html",
        active_page="home",
        index_cards=index_cards,
        hot_sources=hot_sources,
        keywords=keyword_counts,
    )
    return HTMLResponse(html)


@router.get("/hot-news", response_class=HTMLResponse)
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
    db = request.app.state.db
    per_page = page_size
    offset = (page - 1) * per_page
    tier_filter = tier if tier and tier > 0 else None
    keyword = keyword or []

    today_str = date_type.today().isoformat()
    show_all = request.query_params.get("all") == "1"
    if show_all:
        date_from = None
        date_to = None
    elif date_from is None and date_to is None:
        date_from = today_str
        date_to = today_str

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
        {"tier": 1, "label": TIER_LABELS[1], "count": stats_data["t1_count"]},
        {"tier": 2, "label": TIER_LABELS[2], "count": stats_data["t2_count"]},
        {"tier": 3, "label": TIER_LABELS[3], "count": stats_data["t3_count"]},
        {"tier": 4, "label": TIER_LABELS[4], "count": stats_data["t4_count"]},
    ]

    sentiment_toggles = [
        {"value": "positive", "label": "利好", "css_class": "positive",
         "count": sentiment_counts["positive"]},
        {"value": "negative", "label": "利空", "css_class": "negative",
         "count": sentiment_counts["negative"]},
        {"value": "neutral", "label": "中性", "css_class": "neutral",
         "count": sentiment_counts["neutral"]},
    ]

    sent_total = sum(sentiment_counts.values()) or 1
    sentiment_pct = {
        "negative_pct": round(sentiment_counts["negative"] / sent_total * 100, 1),
        "neutral_pct": round(sentiment_counts["neutral"] / sent_total * 100, 1),
        "positive_pct": round(sentiment_counts["positive"] / sent_total * 100, 1),
    }

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
        today_hot=stats_data["today_count"],
        positive_signal=sentiment_counts["positive"],
        high_impact=high_impact,
        sentiment_counts=sentiment_counts,
        sentiment_pct=sentiment_pct,
        tier_labels=tier_labels_with_counts,
        sentiment_toggles=sentiment_toggles,
        keyword_list=keyword_list,
        active_filters=active_filters,
        current_tier=tier_filter,
        current_sentiment=sentiment,
        current_keywords=keyword,
        current_search=search,
        masonry_cards=masonry_cards,
        total_count=total,
        total_pages=total_pages,
        current_page=page,
        page_numbers=page_numbers,
        current_date_from=date_from,
        current_date_to=date_to,
        today_date=today_str,
        current_page_size=per_page,
    )
    return HTMLResponse(html)


@router.get("/news/{article_id}", response_class=HTMLResponse)
async def news_detail(request: Request, article_id: int):
    """Single news article detail page."""
    db = request.app.state.db
    article = db.get_news_by_id(article_id)
    if article is None:
        return HTMLResponse("<h1>404 Not Found</h1>", status_code=404)
    if article.get("content"):
        article["content"] = re.sub(r"^# .+?\n\n?", "", article["content"], count=1)
        article["content"] = _resolve_image_paths(
            article["content"], article.get("updated_at"),
        )
    html = render_template("pages/news_detail.html", active_page="hot-news", article=article)
    return HTMLResponse(html)


@router.get("/media/{path:path}")
async def media_proxy(path: str, request: Request):
    """Serve media from S3 (presigned redirect) or local filesystem."""
    from fastapi.responses import FileResponse, RedirectResponse

    storage: FileStorage = request.app.state.media_storage
    if isinstance(storage, S3Storage):
        url = storage.get(path, expires_in=3600)
        return RedirectResponse(url=url)
    else:
        return FileResponse(storage.get(path))


@router.post("/api/trigger/crawl")
async def trigger_crawl(request: Request):
    """Manually trigger a crawl job. Returns 409 if a crawl is already running."""
    queue = request.app.state.queues.get("crawl")
    if queue is None:
        return JSONResponse({"ok": False, "error": "not available"}, status_code=404)

    lock = request.app.state.queues.get("crawl_lock")
    if lock and lock.locked():
        return JSONResponse({"ok": False, "error": "已有抓取任务正在执行"}, status_code=409)

    ns = request.app.state.notification_state
    notif = ns.add_notification(scope="news", article_id=0, title="新闻抓取",
                                status="running", category="crawl")

    def on_complete(success: bool, summary: str):
        notif["status"] = "completed" if success else "failed"
        notif["summary"] = summary
        ns.push_sse_event({"type": "update", "notification": dict(notif)})

    await queue.put(on_complete)
    return {"ok": True, "task": "crawl", "notif_id": notif["id"]}


@router.post("/api/trigger/sync")
async def trigger_sync(request: Request):
    """Manually trigger a cloud sync job. Returns 409 if a sync is already running."""
    queue = request.app.state.queues.get("sync")
    if queue is None:
        return JSONResponse({"ok": False, "error": "not available"}, status_code=404)

    lock = request.app.state.queues.get("sync_lock")
    if lock and lock.locked():
        return JSONResponse({"ok": False, "error": "已有同步任务正在执行"}, status_code=409)

    ns = request.app.state.notification_state
    notif = ns.add_notification(scope="news", article_id=0, title="云端同步",
                                status="running", category="sync")

    def on_complete(success: bool, summary: str):
        notif["status"] = "completed" if success else "failed"
        notif["summary"] = summary
        ns.push_sse_event({"type": "update", "notification": dict(notif)})

    await queue.put(on_complete)
    return {"ok": True, "task": "sync", "notif_id": notif["id"]}


@router.get("/api/notifications/stream")
async def notification_stream(request: Request):
    """SSE endpoint — pushes new/updated notifications to the client."""
    from starlette.responses import StreamingResponse

    ns = request.app.state.notification_state
    ns.set_event_loop(asyncio.get_running_loop())

    queue: asyncio.Queue = asyncio.Queue()
    ns.register_client(queue)

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
            ns.unregister_client(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/api/news/fetch")
async def fetch_news_by_url(request: Request):
    """Submit a URL for background fetch — dedup by URL, refetch if exists."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            {"ok": False, "error": "请求体必须为 JSON"}, status_code=400,
        )
    url = (body.get("url") or "").strip()
    if not url:
        return JSONResponse(
            {"ok": False, "error": "URL 不能为空"}, status_code=400,
        )
    if not (url.startswith("http://") or url.startswith("https://")):
        return JSONResponse(
            {"ok": False, "error": "URL 必须以 http:// 或 https:// 开头"},
            status_code=400,
        )

    db = request.app.state.db
    crawler = request.app.state.crawler
    runner = request.app.state.background_runner
    ns = request.app.state.notification_state

    if crawler is None or runner is None:
        return JSONResponse(
            {"ok": False, "error": "抓取服务未就绪"}, status_code=503,
        )

    existing = db.get_article_by_url(url)
    if existing:
        article_id = existing["id"]
        title = existing.get("title") or url

        task_id = f"refetch-{article_id}"
        status = runner.get_status(task_id)
        if status and status["status"] in ("pending", "running"):
            return {"ok": False, "error": "该文章正在抓取中"}

        notif = ns.add_notification(scope="news", article_id=article_id,
                                    title=title, status="pending")
        runner.submit(task_id, _run_refetch, article_id, crawler, notif, db, ns)
        return {"ok": True, "refetch": True, "article_id": article_id}

    notif = ns.add_notification(scope="news", article_id=0, title=url, status="pending")
    runner.submit(f"fetch-{int(time.time() * 1000)}",
                  _run_fetch_url, url, crawler, notif, db, ns)
    return {"ok": True, "message": "已提交抓取任务"}


@router.post("/api/news/{article_id}/refetch")
async def refetch_article(request: Request, article_id: int):
    """Submit a background refetch job for the given article."""
    db = request.app.state.db
    article = db.get_news_by_id(article_id)
    if article is None:
        return JSONResponse({"ok": False, "error": "文章不存在"}, status_code=404)
    url = (article.get("url") or "").strip()
    title = article.get("title") or ""
    if not url:
        return {"ok": False, "error": "该文章没有原文链接"}

    crawler = request.app.state.crawler
    runner = request.app.state.background_runner
    ns = request.app.state.notification_state

    if crawler is None or runner is None:
        return JSONResponse({"ok": False, "error": "抓取服务未就绪"}, status_code=503)

    task_id = f"refetch-{article_id}"
    status = runner.get_status(task_id)
    if status and status["status"] in ("pending", "running"):
        return {"ok": False, "error": "该文章正在抓取中"}

    notif = ns.add_notification(scope="news", article_id=article_id,
                                title=title, status="pending")
    runner.submit(task_id, _run_refetch, article_id, crawler, notif, db, ns)
    return {"ok": True, "task": notif}


@router.post("/api/news/{article_id}/sentiment-score")
async def set_user_sentiment_score(article_id: int, request: Request):
    """Set user sentiment score for an article."""
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
    db = request.app.state.db
    updated = db.set_sentiment_score(article_id, score)
    if not updated:
        return JSONResponse(
            {"ok": False, "error": "文章不存在"}, status_code=404,
        )
    return {"ok": True, "score": score}


@router.delete("/api/news/{article_id}")
async def delete_article(request: Request, article_id: int):
    """Delete an article and its images (cascade)."""
    db = request.app.state.db
    article = db.get_news_by_id(article_id)
    if article is None:
        return JSONResponse({"ok": False, "error": "文章不存在"}, status_code=404)
    deleted = db.delete_news(article_id)
    if not deleted:
        return JSONResponse({"ok": False, "error": "文章不存在"}, status_code=404)

    runner = request.app.state.background_runner
    if runner:
        runner.remove(f"refetch-{article_id}")
    return {"ok": True}


@router.get("/api/notifications")
async def list_notifications(
    request: Request,
    scope: str | None = Query(None),
    unread_only: bool = Query(False),
):
    """Return notification list, optionally filtered by scope and/or unread."""
    ns = request.app.state.notification_state
    result = ns.get_notifications(scope=scope, unread_only=unread_only)

    db = request.app.state.db
    for n in result:
        if n.get("article_id", 0) == 0 and n.get("title", "").startswith("http"):
            article = db.get_article_by_url(n["title"])
            if article:
                n["article_id"] = article["id"]
    return result


@router.get("/api/notifications/unread-count")
async def unread_notification_count(
    request: Request,
    scope: str | None = Query(None),
):
    """Return the count of unread notifications, optionally filtered by scope."""
    ns = request.app.state.notification_state
    count = ns.get_unread_count(scope=scope)
    return {"count": count}


@router.post("/api/notifications/{notif_id}/read")
async def mark_notification_read(request: Request, notif_id: int):
    """Mark a single notification as read."""
    ns = request.app.state.notification_state
    if ns.mark_read(notif_id):
        return {"ok": True}
    return JSONResponse({"ok": False, "error": "通知不存在"}, status_code=404)


@router.post("/api/notifications/mark-all-read")
async def mark_all_read(request: Request):
    """Mark all notifications as read."""
    ns = request.app.state.notification_state
    ns.mark_all_read()
    return {"ok": True}
