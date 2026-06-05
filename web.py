"""NewsRadar Web Frontend — FastAPI + Jinja2 SSR."""

from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape

from config import load_config
from database import init_db, close_db, get_recent_news, get_news_count, get_stats, get_news_by_id

BASE_DIR = Path(__file__).parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

# Load config for DB connection
_config = load_config(str(BASE_DIR / "config.yaml"))

# Jinja2 environment
env = Environment(
    loader=FileSystemLoader(TEMPLATES_DIR),
    autoescape=select_autoescape(["html"]),
)

# Lucide icon SVG map (16x16, stroke-width 2, stroke-linecap round, stroke-linejoin round)
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
env.globals["icon_svg"] = lambda name: ICONS.get(name, "")
env.globals["len"] = len


def render_template(name: str, **context) -> str:
    """Render a Jinja2 template."""
    template = env.get_template(name)
    return template.render(**context)


app = FastAPI(title="NewsRadar", version="1.0.0")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.on_event("startup")
async def startup():
    """Initialize database on app start."""
    init_db(_config["postgresql"])
    print("[Web] Database initialized")


@app.on_event("shutdown")
async def shutdown():
    """Close database on app shutdown."""
    close_db()


# ===== Routes =====

@app.get("/", response_class=HTMLResponse)
async def market_overview(request: Request):
    """Market overview page with real stats."""
    stats = get_stats()

    index_cards = [
        {"name": "T1·官媒", "value": str(stats["t1_count"]), "change": None},
        {"name": "T2·主流", "value": str(stats["t2_count"]), "change": None},
        {"name": "T3·垂直", "value": str(stats["t3_count"]), "change": None},
        {"name": "T4·资讯", "value": str(stats["t4_count"]), "change": None},
        {"name": "总计新闻", "value": str(stats["total_count"]), "change": None},
        {"name": "今日新增", "value": str(stats["today_count"]), "change": None},
    ]

    hot_stocks = [
        {"name": s["source_name"], "change": s["cnt"]}
        for s in stats["by_source"][:8]
    ]

    html = render_template(
        "pages/market_overview.html",
        active_page="home",
        index_cards=index_cards,
        hot_stocks=hot_stocks,
    )
    return HTMLResponse(html)


@app.get("/hot-news", response_class=HTMLResponse)
async def hot_news(
    request: Request,
    page: int = Query(1, ge=1),
    tier: int = Query(None, ge=0, le=4),
):
    """Hot news page with real data from PostgreSQL."""
    from notifier import TIER_LABELS, TIER_COLORS, TIER_BG

    per_page = 10
    offset = (page - 1) * per_page

    # Fetch from DB
    articles = get_recent_news(
        limit=per_page,
        offset=offset,
        tier=tier if tier and tier > 0 else None,
    )
    total = get_news_count(tier=tier if tier and tier > 0 else None)
    total_pages = max(1, (total + per_page - 1) // per_page)

    # Stats
    stats_data = get_stats()

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
        {"label": f"T{t}·{TIER_LABELS[t].split('·')[1]}", "color": c, "bg": TIER_BG[t]}
        for t, c in TIER_COLORS.items()
    ]

    keywords = ["央行", "AI", "港股", "外资", "芯片", "新能源"]

    # Convert articles to template format
    def _to_card(article: dict) -> dict:
        score = article.get("sentiment_score")
        if score is not None and score >= 67:
            sentiment, s_bg, s_color = "利好", "hsl(var(--danger) / 0.1)", "hsl(var(--danger))"
        elif score is not None and score <= 33:
            sentiment, s_bg, s_color = "利空", "hsl(var(--success) / 0.1)", "hsl(var(--success))"
        else:
            sentiment, s_bg, s_color = "中性", "hsl(var(--warning) / 0.1)", "hsl(var(--warning))"

        return {
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
        return {
            "seq": seq,
            "title": article.get("title", ""),
            "source": article.get("source_name", ""),
        }

    tier1_cards = [_to_card(a) for a in articles]
    list_items = [_to_list_item(a, offset + i + 1) for i, a in enumerate(articles)]
    page_numbers = _build_page_numbers(page, total_pages)

    html = render_template(
        "pages/hot_news.html",
        active_page="hot-news",
        stats=stats,
        tier_labels=tier_labels,
        keywords=keywords,
        tier1_cards=tier1_cards,
        list_items=list_items,
        total_count=total,
        page_start=offset + 1,
        page_end=min(offset + per_page, total),
        current_page=page,
        page_numbers=page_numbers,
    )
    return HTMLResponse(html)


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


@app.get("/news/{article_id}", response_class=HTMLResponse)
async def news_detail(request: Request, article_id: int):
    """Single news article detail page."""
    article = get_news_by_id(article_id)
    if article is None:
        return HTMLResponse("<h1>404 Not Found</h1>", status_code=404)

    html = render_template(
        "pages/news_detail.html",
        active_page="hot-news",
        article=article,
    )
    return HTMLResponse(html)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("web:app", host="0.0.0.0", port=8000, reload=True)
