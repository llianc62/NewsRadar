# NewsRadar 前端实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 NewsRadar 添加 FastAPI + Jinja2 SSR Web 前端，实现市场概览和热点新闻两个页面。

**Architecture:** FastAPI 应用通过 Jinja2 模板渲染 SSR 页面。现有 CLI（crawl/notify）保持不变，新增 `web.py` 作为 Web 入口。模板使用组件化拆分（sidebar、stats_bar、news_card 等），CSS 使用原生自定义属性。首页使用 mock 数据，后续对接 SQLite。

**Tech Stack:** FastAPI, Jinja2, uvicorn, Lucide Icons (inline SVG), Inter + JetBrains Mono fonts (CDN), vanilla CSS with custom properties

---

## Task 0: 环境准备

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: 添加 Web 依赖**

```toml
# pyproject.toml, 在 dependencies 列表末尾添加:
    "fastapi>=0.115",
    "uvicorn[standard]>=0.34",
    "jinja2>=3.1",
```

- [ ] **Step 2: 安装依赖**

```bash
cd /home/llianc62/ws/NewsRadar && uv sync
```

Expected: 无报错，fastapi/uvicorn/jinja2 安装成功。

- [ ] **Step 3: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "chore: add FastAPI + Jinja2 + uvicorn dependencies

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 1: 目录结构与全局 CSS

**Files:**
- Create: `static/css/app.css`
- Create: `templates/base.html`

- [ ] **Step 1: 创建目录**

```bash
mkdir -p /home/llianc62/ws/NewsRadar/templates/pages
mkdir -p /home/llianc62/ws/NewsRadar/templates/components
mkdir -p /home/llianc62/ws/NewsRadar/static/css
```

- [ ] **Step 2: 编写全局 CSS 设计系统**

`static/css/app.css`:

```css
/* ===== Design Tokens ===== */
:root {
  --background: 220 20% 98%;
  --foreground: 220 20% 10%;
  --card: 0 0% 100%;
  --primary: 27 90% 52%;
  --muted: 220 14% 92%;
  --muted-foreground: 220 14% 46%;
  --border: 220 14% 87%;
  --radius-sm: 6px;
  --radius: 12px;
  --radius-lg: 14px;

  --danger: 0 84% 60%;
  --success: 142 71% 45%;
  --warning: 38 92% 50%;
  --info: 217 91% 60%;

  --sidebar-width: 220px;
  --font-sans: 'Inter', system-ui, -apple-system, sans-serif;
  --font-mono: 'JetBrains Mono', monospace;
}

/* ===== Reset ===== */
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

html, body {
  height: 100%;
  font-family: var(--font-sans);
  background: hsl(var(--background));
  color: hsl(var(--foreground));
  font-size: 14px;
  line-height: 1.5;
  -webkit-font-smoothing: antialiased;
}

a { color: inherit; text-decoration: none; }

/* ===== Layout ===== */
.app-layout {
  display: flex;
  min-height: 100vh;
}

.app-sidebar {
  width: var(--sidebar-width);
  min-width: var(--sidebar-width);
  background: hsl(var(--card));
  border-right: 1px solid hsl(var(--border));
  display: flex;
  flex-direction: column;
  position: fixed;
  top: 0;
  left: 0;
  bottom: 0;
  z-index: 10;
}

.app-content {
  margin-left: var(--sidebar-width);
  flex: 1;
  padding: 20px 28px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

/* ===== Utility ===== */
.font-mono { font-family: var(--font-mono); }
.text-muted { color: hsl(var(--muted-foreground)); }
```

- [ ] **Step 3: 编写 base.html 布局模板**

`templates/base.html`:

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{% block title %}NewsRadar{% end %}</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
  <link rel="stylesheet" href="/static/css/app.css">
  {% block head %}{% end %}
</head>
<body>
  <div class="app-layout">
    {% include "components/sidebar.html" %}
    <main class="app-content">
      {% block content %}{% end %}
    </main>
  </div>
</body>
</html>
```

- [ ] **Step 4: Commit**

```bash
git add static/ templates/
git commit -m "feat: add base layout template and CSS design system

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: 侧边导航栏组件

**Files:**
- Create: `templates/components/sidebar.html`

- [ ] **Step 1: 编写 sidebar.html**

`templates/components/sidebar.html`:

```html
<aside class="app-sidebar">
  <!-- Logo -->
  <div style="padding: 18px; border-bottom: 1px solid hsl(var(--border)); display: flex; align-items: center; gap: 10px;">
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="hsl(var(--primary))" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
      <path d="M6 20V8"/><path d="M10 20V4"/><path d="M14 20V12"/><path d="M18 20V16"/>
    </svg>
    <span style="font-weight: 700; font-size: 14px; color: hsl(var(--foreground));">NewsRadar</span>
  </div>

  <!-- Navigation -->
  <nav style="flex: 1; padding: 12px 10px; display: flex; flex-direction: column; gap: 1px;">
    {% set nav_items = [
      ('home', '/', 'chart-column', 'Home'),
      ('hot-news', '/hot-news', 'flame', '热点新闻'),
      ('positions', '/positions', 'briefcase-business', '持仓分析'),
      ('stocks', '/stocks', 'trending-up', '个股分析'),
      ('reports', '/reports', 'file-text', '行业报告'),
      ('trading', '/trading', 'clock', '交易决策'),
      ('settings', '/settings', 'sliders-horizontal', '系统配置'),
    ] %}
    {% for id, url, icon, label in nav_items %}
    <a href="{{ url }}"
       class="nav-item{% if active_page == id %} active{% end %}"
       style="display: flex; align-items: center; gap: 10px; padding: 8px 12px; border-radius: var(--radius-sm); font-size: 13px; font-weight: 500;">
      {% raw icon_svg(icon) %}
      {{ label }}
    </a>
    {% end %}
  </nav>
</aside>

<style>
.nav-item {
  color: hsl(var(--muted-foreground));
  transition: color 0.15s;
}
.nav-item:hover { color: hsl(var(--foreground)); }
.nav-item.active {
  color: hsl(var(--primary));
  font-weight: 600;
}
</style>
```

- [ ] **Step 2: Commit**

```bash
git add templates/components/sidebar.html
git commit -m "feat: add sidebar navigation component with Lucide icons

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: Lucide 图标辅助函数 + Web 入口

**Files:**
- Create: `web.py`
- Create: `templates/helpers.py` (Jinja2 自定义过滤器/全局函数)

**Note:** 采用简单方案——图标 SVG 直接内联在模板 helper 中，避免额外依赖。

- [ ] **Step 1: 编写 web.py Web 入口**

`web.py`:

```python
"""NewsRadar Web 前端 — FastAPI + Jinja2 SSR."""

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
env.globals["icon_svg"] = lambda name: ICONS.get(name, "")


def render_template(name: str, **context) -> str:
    """Render a Jinja2 template."""
    template = env.get_template(name)
    return template.render(**context)


app = FastAPI(title="NewsRadar", version="1.0.0")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
```

- [ ] **Step 2: Commit**

```bash
git add web.py
git commit -m "feat: add FastAPI web entry with Jinja2 and Lucide icons

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: 市场概览页（Mock 数据）

**Files:**
- Create: `templates/pages/market_overview.html`
- Modify: `web.py` (添加路由)

- [ ] **Step 1: 编写市场概览页模板**

`templates/pages/market_overview.html`:

```html
{% extends "base.html" %}
{% block title %}NewsRadar · 市场概览{% end %}

{% block content %}

<!-- Index Cards -->
<div style="display: grid; grid-template-columns: repeat(6, 1fr); gap: 12px;">
  {% for card in index_cards %}
  <div style="background: hsl(var(--card)); border-radius: var(--radius); border: 1px solid hsl(var(--border)); padding: 16px 20px;">
    <div style="font-size: 12px; color: hsl(var(--muted-foreground)); margin-bottom: 8px;">{{ card['name'] }}</div>
    <div class="font-mono" style="font-size: 19px; font-weight: 700; color: hsl(var(--foreground));">{{ card['value'] }}</div>
    <div class="font-mono" style="font-size: 12px; margin-top: 4px; color: hsl({{ 'var(--danger)' if card['change'] > 0 else 'var(--success)' }});">
      {{ '+' if card['change'] > 0 else '' }}{{ card['change'] }}%
    </div>
  </div>
  {% end %}
</div>

<!-- Charts + Hot Stocks -->
<div style="display: flex; gap: 16px; flex: 1; min-height: 0;">
  <!-- Charts Area -->
  <div style="flex: 2; background: hsl(var(--card)); border-radius: var(--radius); border: 1px solid hsl(var(--border)); padding: 24px; display: flex; align-items: center; justify-content: center;">
    <div style="text-align: center; color: hsl(var(--muted-foreground));">
      <div style="font-size: 48px; margin-bottom: 8px;">📈</div>
      <div style="font-size: 14px;">图表区 — 后续集成 ECharts</div>
    </div>
  </div>

  <!-- Hot Stocks Sidebar -->
  <div style="flex: 1; background: hsl(var(--card)); border-radius: var(--radius); border: 1px solid hsl(var(--border)); padding: 16px;">
    <div style="font-size: 13px; font-weight: 600; margin-bottom: 12px;">热门股票</div>
    {% for stock in hot_stocks %}
    <div style="display: flex; justify-content: space-between; align-items: center; padding: 8px 0; border-bottom: 1px solid hsl(var(--muted)); font-size: 13px;">
      <span>{{ stock['name'] }}</span>
      <span class="font-mono" style="color: hsl({{ 'var(--danger)' if stock['change'] > 0 else 'var(--success)' }});">
        {{ '+' if stock['change'] > 0 else '' }}{{ stock['change'] }}%
      </span>
    </div>
    {% end %}
  </div>
</div>

{% end %}
```

- [ ] **Step 2: 添加路由到 web.py**

在 `web.py` 末尾追加:

```python
# ===== Routes =====

@app.get("/", response_class=HTMLResponse)
async def market_overview(request: Request):
    """市场概览页."""
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
```

- [ ] **Step 3: Commit**

```bash
git add templates/pages/market_overview.html web.py
git commit -m "feat: add market overview page with mock data

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: 热点新闻页 — Mock 数据 + 统计栏

**Files:**
- Create: `templates/components/stats_bar.html`
- Create: `templates/pages/hot_news.html`
- Modify: `web.py` (添加路由)

- [ ] **Step 1: 编写统计栏组件**

`templates/components/stats_bar.html`:

```html
<div style="display: flex; gap: 12px; flex-shrink: 0;">
  {% for stat in stats %}
  <div style="flex:1; display: flex; align-items: center; gap: 14px; padding: 14px 20px; background: hsl(var(--card)); border-radius: var(--radius); border: 1px solid hsl(var(--border));">
    <div style="width: 42px; height: 42px; border-radius: 10px; background: {{ stat['bg'] }}; display: flex; align-items: center; justify-content: center;">
      {% raw icon_svg(stat['icon']) %}
    </div>
    <div>
      <div style="font-size: 11px; color: hsl(var(--muted-foreground));">{{ stat['label'] }}</div>
      <div class="font-mono" style="font-size: 24px; font-weight: 700; color: {{ stat['color'] }};">{{ stat['value'] }}</div>
    </div>
  </div>
  {% end %}
</div>
```

- [ ] **Step 2: 编写热点新闻页模板**

`templates/pages/hot_news.html`:

```html
{% extends "base.html" %}
{% block title %}NewsRadar · 热点新闻{% end %}

{% block content %}

<!-- Global Filter Bar -->
<div style="display: flex; flex-direction: column; gap: 8px; padding: 12px 16px; background: hsl(var(--card)); border-radius: var(--radius); border: 1px solid hsl(var(--border)); flex-shrink: 0;">
  <div style="font-size: 12px; font-weight: 700; color: hsl(var(--foreground));">筛选：</div>

  <!-- Level 1: Tier -->
  <div style="display: flex; align-items: center; gap: 10px;">
    <span style="font-size: 11px; font-weight: 600; color: hsl(var(--foreground)); min-width: 56px; white-space: nowrap;">级别</span>
    <span class="filter-chip active" style="font-size: 11px; padding: 5px 14px; border-radius: 14px; background: hsl(var(--primary)); color: white; font-weight: 600;">全部</span>
    {% for t in tier_labels %}
    <span class="filter-chip" style="font-size: 11px; padding: 5px 14px; border-radius: 14px; background: {{ t['bg'] }}; color: {{ t['color'] }}; font-weight: 500;">{{ t['label'] }}</span>
    {% end %}
  </div>

  <!-- Level 2: Tags -->
  <div style="display: flex; align-items: center; gap: 10px;">
    <span style="font-size: 11px; font-weight: 600; color: hsl(var(--foreground)); min-width: 56px; white-space: nowrap;">标签</span>
    <span class="filter-chip" style="font-size: 11px; padding: 5px 14px; border-radius: 14px; background: hsl(var(--muted)); color: hsl(var(--muted-foreground)); font-weight: 500;">全部</span>
    <span class="filter-chip" style="font-size: 11px; padding: 5px 14px; border-radius: 14px; background: hsl(var(--danger) / 0.08); color: hsl(var(--danger)); font-weight: 500;">利好</span>
    <span class="filter-chip" style="font-size: 11px; padding: 5px 14px; border-radius: 14px; background: hsl(var(--success) / 0.08); color: hsl(var(--success)); font-weight: 500;">利空</span>
    {% for kw in keywords %}
    <span class="filter-chip" style="font-size: 11px; padding: 5px 14px; border-radius: 14px; background: hsl(var(--muted)); color: hsl(var(--muted-foreground)); font-weight: 500;">{{ kw }}</span>
    {% end %}
  </div>
</div>

<style>
.filter-chip { cursor: default; white-space: nowrap; transition: opacity 0.15s; }
.filter-chip:hover { opacity: 0.8; }
</style>

<!-- Stats Bar -->
{% include "components/stats_bar.html" %}

<!-- Content: Waterfall + List -->
<div style="display: flex; gap: 16px; align-items: flex-start; flex: 1; min-height: 0;">
  {% include "components/news_cards.html" %}
  {% include "components/news_list.html" %}
</div>

{% end %}
```

- [ ] **Step 3: 添加路由到 web.py**

在 `web.py` 的路由区域追加:

```python
@app.get("/hot-news", response_class=HTMLResponse)
async def hot_news(request: Request):
    """热点新闻页."""
    from notifier import TIER_LABELS, TIER_COLORS, TIER_BG

    stats = [
        {"label": "今日热点", "value": "86", "icon": "flame",
         "bg": "hsl(var(--primary) / 0.1)", "color": "hsl(var(--foreground))"},
        {"label": "新闻来源", "value": "7", "icon": "newspaper",
         "bg": "hsl(var(--info) / 0.1)", "color": "hsl(var(--foreground))"},
        {"label": "热点新闻", "value": "8", "icon": "star",
         "bg": "hsl(var(--warning) / 0.1)", "color": "hsl(var(--foreground))"},
        {"label": "利好指数", "value": "62%", "icon": "trending-up-lg",
         "bg": "hsl(var(--danger) / 0.1)", "color": "hsl(var(--danger))"},
    ]
    tier_labels = [
        {"label": f"T{t}·{TIER_LABELS[t].split('·')[1]}", "color": c, "bg": TIER_BG[t]}
        for t, c in TIER_COLORS.items()
    ]
    keywords = ["央行", "AI", "港股", "外资", "芯片", "新能源"]
    html = render_template(
        "pages/hot_news.html",
        active_page="hot-news",
        stats=stats,
        tier_labels=tier_labels,
        keywords=keywords,
    )
    return HTMLResponse(html)
```

- [ ] **Step 4: Commit**

```bash
git add templates/components/stats_bar.html templates/pages/hot_news.html web.py
git commit -m "feat: add hot news page with filter bar and stats bar

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 6: 瀑布卡片组件

**Files:**
- Create: `templates/components/news_cards.html`

- [ ] **Step 1: 编写瀑布卡片组件**

`templates/components/news_cards.html`:

```html
<!-- Left: Tier 1 Waterfall Cards -->
<div style="flex: 2; display: flex; flex-direction: column; gap: 10px;">
  <div style="font-size: 12px; font-weight: 600; color: hsl(var(--muted-foreground)); margin-bottom: 2px; display: flex; align-items: center; gap: 6px;">
    {% raw icon_svg('star') %}
    热点新闻
    <span style="font-weight: 400; font-family: var(--font-mono); font-size: 11px; padding: 2px 8px; border-radius: 10px; background: hsl(var(--primary) / 0.1); color: hsl(var(--primary));">仅 T1 · {{ len(tier1_cards) }} 条</span>
  </div>

  <!-- CSS Columns Waterfall -->
  <div style="column-count: 2; column-gap: 14px;">
    {% for card in tier1_cards %}
    <div style="break-inside: avoid; margin-bottom: 14px; background: hsl(var(--card)); border-radius: var(--radius-lg); border: 1px solid hsl(var(--border)); padding: 24px; display: flex; flex-direction: column; gap: 16px;">
      <!-- Top meta -->
      <div style="display: flex; justify-content: space-between; align-items: center;">
        <div style="display: flex; align-items: center; gap: 8px;">
          <span style="font-size: 10px; padding: 3px 10px; border-radius: 10px; background: {{ card['sentiment_bg'] }}; color: {{ card['sentiment_color'] }}; font-weight: 600;">{{ card['sentiment'] }}</span>
          <span style="font-size: 11px; color: hsl(var(--muted-foreground));">{{ card['source'] }} · {{ card['time'] }}</span>
        </div>
        <span class="font-mono" style="font-size: 12px; color: hsl(var(--primary)); font-weight: 700;">🔥 {{ card['heat'] }}</span>
      </div>

      <!-- Title -->
      <div style="font-weight: 700; font-size: 16px; color: hsl(var(--foreground)); line-height: 1.55;">{{ card['title'] }}</div>

      <!-- Divider -->
      <div style="height: 1px; background: hsl(var(--muted));"></div>

      <!-- Summary -->
      <div style="font-size: 13px; color: hsl(var(--muted-foreground)); line-height: 1.8;">{{ card['summary'] }}</div>

      <!-- Analysis points (optional) -->
      {% if card.get('points') %}
      <div style="font-size: 13px; color: hsl(var(--muted-foreground)); line-height: 1.8;">
        {% for p in card['points'] %}
        · {{ p }}<br>
        {% end %}
      </div>
      {% end %}

      <!-- Keywords -->
      {% if card.get('keywords') %}
      <div style="display: flex; flex-wrap: wrap; gap: 6px;">
        {% for kw in card['keywords'] %}
        <span style="font-size: 11px; padding: 5px 12px; border-radius: 14px; background: {{ 'hsl(var(--primary) / 0.08)' if kw['primary'] else 'hsl(var(--muted))' }}; color: {{ 'hsl(var(--primary))' if kw['primary'] else 'hsl(var(--muted-foreground))' }}; font-weight: {{ 600 if kw['primary'] else 400 }};">{{ kw['text'] }}</span>
        {% end %}
      </div>
      {% end %}
    </div>
    {% end %}
  </div>

  <div style="text-align: center; padding: 6px; font-size: 11px; color: hsl(var(--muted-foreground));">热点新闻：{{ len(tier1_cards) }}/{{ len(tier1_cards) }} 已全部展示</div>
</div>
```

- [ ] **Step 2: Commit**

```bash
git add templates/components/news_cards.html
git commit -m "feat: add waterfall news cards component with CSS columns

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 7: 分页新闻列表组件

**Files:**
- Create: `templates/components/news_list.html`

- [ ] **Step 1: 编写分页列表组件**

`templates/components/news_list.html`:

```html
<!-- Right: Paginated News List -->
<div style="flex: 1; background: hsl(var(--card)); border-radius: var(--radius); border: 1px solid hsl(var(--border)); display: flex; flex-direction: column;">

  <!-- Header -->
  <div style="padding: 12px 16px; border-bottom: 1px solid hsl(var(--muted)); display: flex; align-items: center; justify-content: space-between; flex-shrink: 0;">
    <span style="font-size: 13px; font-weight: 600; color: hsl(var(--foreground)); display: flex; align-items: center; gap: 6px;">
      {% raw icon_svg('newspaper') %}
      其他新闻
    </span>
    <span class="font-mono" style="font-size: 11px; color: hsl(var(--muted-foreground));">共 {{ total_count }} 条</span>
  </div>

  <!-- List Items -->
  <div style="">
    {% for item in list_items %}
    <div style="padding: 9px 16px; display: flex; gap: 8px; align-items: flex-start; border-bottom: 1px solid hsl(var(--muted)); font-size: 12px;">
      <span class="font-mono" style="min-width: 28px; color: hsl(var(--muted-foreground)); font-size: 11px; padding-top: 1px;">{{ item['seq'] }}</span>
      <span style="flex: 1; font-weight: 500; color: hsl(var(--foreground)); line-height: 1.4;">{{ item['title'] }}</span>
      <span style="min-width: 52px; font-size: 10px; color: hsl(var(--muted-foreground)); text-align: right; white-space: nowrap;">{{ item['source'] }}</span>
    </div>
    {% end %}
  </div>

  <!-- Pagination -->
  <div style="border-top: 1px solid hsl(var(--muted)); padding: 10px 16px; display: flex; align-items: center; justify-content: space-between; flex-shrink: 0; background: hsl(var(--background));">
    <span class="font-mono" style="font-size: 11px; color: hsl(var(--muted-foreground));">
      第 {{ page_start }}-{{ page_end }} 条
    </span>
    <div style="display: flex; align-items: center; gap: 4px;">
      <span style="width: 28px; height: 28px; border-radius: var(--radius-sm); border: 1px solid hsl(var(--border)); background: hsl(var(--card)); display: flex; align-items: center; justify-content: center; opacity: 0.4; font-size: 12px; color: hsl(var(--muted-foreground));">‹</span>

      {% for p in page_numbers %}
        {% if p == '...' %}
        <span style="font-size: 11px; color: hsl(var(--muted-foreground)); padding: 0 2px;">…</span>
        {% elif p == current_page %}
        <span style="width: 28px; height: 28px; border-radius: var(--radius-sm); border: none; background: hsl(var(--primary)); color: white; font-weight: 600; font-size: 11px; font-family: var(--font-mono); display: flex; align-items: center; justify-content: center;">{{ p }}</span>
        {% else %}
        <span style="width: 28px; height: 28px; border-radius: var(--radius-sm); border: 1px solid hsl(var(--border)); background: hsl(var(--card)); font-size: 11px; font-family: var(--font-mono); color: hsl(var(--muted-foreground)); display: flex; align-items: center; justify-content: center;">{{ p }}</span>
        {% end %}
      {% end %}

      <span style="width: 28px; height: 28px; border-radius: var(--radius-sm); border: 1px solid hsl(var(--border)); background: hsl(var(--card)); display: flex; align-items: center; justify-content: center; font-size: 12px; color: hsl(var(--muted-foreground));">›</span>
    </div>
  </div>
</div>
```

- [ ] **Step 2: Commit**

```bash
git add templates/components/news_list.html
git commit -m "feat: add paginated news list component

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 8: 热点新闻 Mock 数据 + 完整集成

**Files:**
- Modify: `web.py` (补充 mock 数据到 hot_news 路由)

- [ ] **Step 1: 补充完整 mock 数据到 hot_news 路由**

替换 `web.py` 中 `hot_news` 路由函数为:

```python
@app.get("/hot-news", response_class=HTMLResponse)
async def hot_news(request: Request):
    """热点新闻页."""
    from notifier import TIER_LABELS, TIER_COLORS, TIER_BG

    stats = [
        {"label": "今日热点", "value": "86", "icon": "flame",
         "bg": "hsl(var(--primary) / 0.1)", "color": "hsl(var(--foreground))"},
        {"label": "新闻来源", "value": "7", "icon": "newspaper",
         "bg": "hsl(var(--info) / 0.1)", "color": "hsl(var(--foreground))"},
        {"label": "热点新闻", "value": "8", "icon": "star",
         "bg": "hsl(var(--warning) / 0.1)", "color": "hsl(var(--foreground))"},
        {"label": "利好指数", "value": "62%", "icon": "trending-up-lg",
         "bg": "hsl(var(--danger) / 0.1)", "color": "hsl(var(--danger))"},
    ]
    tier_labels = [
        {"label": f"T{t}·{TIER_LABELS[t].split('·')[1]}", "color": c, "bg": TIER_BG[t]}
        for t, c in TIER_COLORS.items()
    ]
    keywords = ["央行", "AI", "港股", "外资", "芯片", "新能源"]

    tier1_cards = [
        {
            "sentiment": "利好", "sentiment_bg": "hsl(var(--danger) / 0.1)",
            "sentiment_color": "hsl(var(--danger))",
            "source": "华尔街见闻", "time": "2h前", "heat": "98",
            "title": "央行降准释放万亿流动性，A股三大指数集体高开",
            "summary": "中国人民银行宣布下调金融机构存款准备金率0.5个百分点，预计释放长期资金约1.2万亿元。分析人士认为此举将有效降低企业融资成本，支持实体经济持续恢复。",
            "points": ["银行板块全线飘红，招商银行涨超3%", "券商板块联动上涨，中信证券涨逾2%"],
            "keywords": [
                {"text": "央行", "primary": True}, {"text": "降准", "primary": False},
                {"text": "流动性", "primary": False}, {"text": "A股", "primary": False},
            ],
        },
        {
            "sentiment": "利空", "sentiment_bg": "hsl(var(--success) / 0.1)",
            "sentiment_color": "hsl(var(--success))",
            "source": "证券时报", "time": "1h前", "heat": "95",
            "title": "美联储暗示6月暂停加息，全球风险资产应声大涨",
            "summary": "美联储主席鲍威尔在国会听证中释放明确鸽派信号，市场对6月加息概率预期从70%骤降至15%。纳指、标普500盘中双双创下历史新高。",
            "keywords": [
                {"text": "美联储", "primary": True}, {"text": "利率", "primary": False},
                {"text": "美股", "primary": False}, {"text": "鲍威尔", "primary": False},
            ],
        },
        {
            "sentiment": "利好", "sentiment_bg": "hsl(var(--danger) / 0.1)",
            "sentiment_color": "hsl(var(--danger))",
            "source": "36氪", "time": "3h前", "heat": "91",
            "title": "OpenAI 发布新一代推理模型，AI 芯片需求预期上调",
            "summary": "新模型在多项基准测试中大幅领先，推理效率提升3倍。多家中外券商同步上调英伟达、台积电目标价，AI算力产业链全面走强。",
            "keywords": [
                {"text": "AI", "primary": True}, {"text": "芯片", "primary": False},
                {"text": "英伟达", "primary": False},
            ],
        },
        {
            "sentiment": "利好", "sentiment_bg": "hsl(var(--danger) / 0.1)",
            "sentiment_color": "hsl(var(--danger))",
            "source": "财联社", "time": "30min前", "heat": "93",
            "title": "北向资金今日净买入超120亿，连续5日净流入创年内纪录",
            "summary": "沪股通净买入58亿，深股通净买入62亿，外资加速回流中国资产。大金融、白酒板块获集中加仓，贵州茅台、招商银行位列净买入榜首。",
            "points": ["连续5日净流入累计超400亿元", "单日净买入额创年内新高"],
            "keywords": [
                {"text": "北向资金", "primary": True}, {"text": "外资", "primary": False},
                {"text": "茅台", "primary": False},
            ],
        },
        {
            "sentiment": "利好", "sentiment_bg": "hsl(var(--danger) / 0.1)",
            "sentiment_color": "hsl(var(--danger))",
            "source": "澎湃新闻", "time": "4h前", "heat": "88",
            "title": "新能源汽车渗透率突破45%，行业拐点已至",
            "summary": "工信部最新数据显示，5月新能源乘用车零售渗透率达45.2%，充电桩、动力电池板块集体爆发，宁德时代盘中涨逾4%。",
            "keywords": [
                {"text": "新能源汽车", "primary": True}, {"text": "宁德时代", "primary": False},
                {"text": "充电桩", "primary": False},
            ],
        },
        {
            "sentiment": "中性", "sentiment_bg": "hsl(var(--warning) / 0.1)",
            "sentiment_color": "hsl(var(--warning))",
            "source": "财联社", "time": "5h前", "heat": "76",
            "title": "比亚迪新车发布，新能源SUV市场格局生变",
            "summary": "比亚迪全新SUV起售价14.98万元，远低于市场预期的16-18万元区间。竞品股价普遍承压，理想、蔚来港股跌幅超过4%。",
            "keywords": [
                {"text": "比亚迪", "primary": True}, {"text": "SUV", "primary": False},
            ],
        },
        {
            "sentiment": "利好", "sentiment_bg": "hsl(var(--danger) / 0.1)",
            "sentiment_color": "hsl(var(--danger))",
            "source": "今日头条", "time": "2h前", "heat": "85",
            "title": "恒生科技指数涨超3%，腾讯阿里双双走强",
            "summary": "港股科技股集体爆发，恒生科技指数创近三个月新高。腾讯控股涨超4%，阿里巴巴涨逾3%，南向资金连续第8个交易日净流入。",
            "keywords": [
                {"text": "港股", "primary": True}, {"text": "腾讯", "primary": False},
                {"text": "阿里", "primary": False},
            ],
        },
        {
            "sentiment": "利好", "sentiment_bg": "hsl(var(--danger) / 0.1)",
            "sentiment_color": "hsl(var(--danger))",
            "source": "澎湃新闻", "time": "6h前", "heat": "72",
            "title": "国务院印发数字经济发展规划，数据要素市场迎重磅利好",
            "summary": "规划明确提出到2027年数字经济核心产业增加值占GDP比重达到12%，数据要素、信创、国产软件等板块迎来政策催化。",
            "keywords": [
                {"text": "数字经济", "primary": True}, {"text": "数据要素", "primary": False},
                {"text": "信创", "primary": False},
            ],
        },
    ]

    list_items = [
        {"seq": 1, "title": "北向资金今日净买入超120亿，连续5日净流入", "source": "华尔街见闻"},
        {"seq": 2, "title": "恒生科技指数涨超3%，腾讯阿里双双走强", "source": "财联社"},
        {"seq": 3, "title": "比特币重返6.5万美元，加密概念股集体上涨", "source": "今日头条"},
        {"seq": 4, "title": "国务院印发数字经济发展规划，数据要素市场迎利好", "source": "澎湃新闻"},
        {"seq": 5, "title": "贵州茅台宣布特别分红，股息率创历史新高", "source": "证券时报"},
        {"seq": 6, "title": "多地放松楼市限购，房地产板块触底反弹", "source": "36氪"},
        {"seq": 7, "title": "中芯国际14nm良率突破，国产替代加速推进", "source": "财联社"},
        {"seq": 8, "title": "光伏组件价格触底，龙头厂商宣布联合限产", "source": "澎湃新闻"},
        {"seq": 9, "title": "瑞银上调中国GDP增速预期至5.3%", "source": "证券时报"},
        {"seq": 10, "title": "科创50ETF份额突破千亿，资金持续涌入", "source": "今日头条"},
    ]

    html = render_template(
        "pages/hot_news.html",
        active_page="hot-news",
        stats=stats,
        tier_labels=tier_labels,
        keywords=keywords,
        tier1_cards=tier1_cards,
        list_items=list_items,
        total_count=78,
        page_start=1,
        page_end=10,
        current_page=1,
        page_numbers=[1, 2, 3, "...", 8],
    )
    return HTMLResponse(html)
```

- [ ] **Step 2: 添加启动入口**

在 `web.py` 末尾追加:

```python
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("web:app", host="0.0.0.0", port=8000, reload=True)
```

- [ ] **Step 3: 启动并验证**

```bash
cd /home/llianc62/ws/NewsRadar && python web.py
```

打开 `http://localhost:8000` 确认市场概览页正常。
打开 `http://localhost:8000/hot-news` 确认热点新闻页正常。

验证：
- 侧边导航栏显示完整，Home 和 热点新闻 高亮正确
- 过滤栏两级显示，T1-T4 颜色正确
- 统计栏 4 张卡片数据正确
- 瀑布卡片 CSS columns 两列，利好/利空徽标正确
- 右侧列表 10 条，来源列显示正确，分页器渲染正常

- [ ] **Step 4: Commit**

```bash
git add web.py
git commit -m "feat: add complete mock data and startup for hot news page

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 9: 最终验证与清理

- [ ] **Step 1: 运行完整验证**

```bash
cd /home/llianc62/ws/NewsRadar && python web.py &
sleep 2
# 验证首页
curl -s http://localhost:8000/ | grep -q "市场概览" && echo "PASS: /" || echo "FAIL: /"
# 验证热点新闻页
curl -s http://localhost:8000/hot-news | grep -q "热点新闻" && echo "PASS: /hot-news" || echo "FAIL: /hot-news"
# 验证静态资源
curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/static/css/app.css | grep -q "200" && echo "PASS: CSS" || echo "FAIL: CSS"
```

- [ ] **Step 2: 停止测试服务器**

```bash
kill %1 2>/dev/null
```

- [ ] **Step 3: 最终 Commit**

```bash
git add -A
git diff --cached --stat
git commit -m "feat: complete NewsRadar frontend MVP — market overview + hot news

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## 验证清单

完成后应确认：

- [ ] `python web.py` 启动无报错
- [ ] `http://localhost:8000` 显示市场概览页（6张指数卡 + 图表区 + 热门股票）
- [ ] `http://localhost:8000/hot-news` 显示热点新闻页（过滤栏 + 统计栏 + 瀑布卡片 + 分页列表）
- [ ] 侧边导航栏点击切换页面，active 状态正确
- [ ] 红色=利好，绿色=利空（中国股市惯例）
- [ ] Tier 标签使用 notifier.py 中的 TIER_LABELS
- [ ] Lucide 图标正常渲染
- [ ] CSS 瀑布流两列正常
- [ ] 分页器渲染正确
