"""NewsRadar Web Frontend — FastAPI + Jinja2 SSR."""

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape

BASE_DIR = Path(__file__).parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

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


def render_template(name: str, **context) -> str:
    """Render a Jinja2 template."""
    template = env.get_template(name)
    return template.render(**context)


app = FastAPI(title="NewsRadar", version="1.0.0")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ===== Routes =====

@app.get("/", response_class=HTMLResponse)
async def market_overview(request: Request):
    """Market overview page."""
    index_cards = [
        {"name": "上证指数", "value": "3,258.16", "change": 1.23},
        {"name": "深证成指", "value": "11,432.07", "change": 0.87},
        {"name": "创业板指", "value": "2,358.42", "change": -0.45},
        {"name": "恒生科技", "value": "4,521.33", "change": 2.15},
        {"name": "纳斯达克", "value": "19,856.27", "change": 0.62},
        {"name": "标普500", "value": "5,934.11", "change": 0.38},
    ]
    hot_stocks = [
        {"name": "贵州茅台", "change": 2.34},
        {"name": "宁德时代", "change": 4.12},
        {"name": "腾讯控股", "change": 3.15},
        {"name": "比亚迪", "change": -1.87},
        {"name": "中芯国际", "change": 5.63},
        {"name": "招商银行", "change": 1.05},
        {"name": "阿里巴巴", "change": 2.78},
        {"name": "药明康德", "change": -0.92},
    ]
    html = render_template(
        "pages/market_overview.html",
        active_page="home",
        index_cards=index_cards,
        hot_stocks=hot_stocks,
    )
    return HTMLResponse(html)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("web:app", host="0.0.0.0", port=8000, reload=True)
