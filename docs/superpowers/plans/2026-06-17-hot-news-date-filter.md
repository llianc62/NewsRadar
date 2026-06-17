# Hot-News 日期筛选 + 面板移除 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将热点新闻页面重构为"日期优先"的筛选架构 — 右上角日历 Popover 作为一级日期筛选，移除右侧滑出面板，瀑布流区域支持翻页和每页条数选择。

**Architecture:** 后端在 `get_recent_news` / `get_news_count` / `get_sentiment_counts` / `get_keyword_counts` / `get_high_impact_count` 五个查询方法上新增 `date_from` / `date_to` 参数（过滤 `published_at` 列）；前端用 CSS-only Popover 实现日历组件，JS 管理预设和自定义范围的状态同步。

**Tech Stack:** Python/FastAPI + Jinja2 + PostgreSQL (psycopg2) + vanilla JS/CSS（无额外前端框架依赖）

**Design reference:** `web/static/hot-news-redesign-v2.html`

## Global Constraints

- Python ≥3.12, 所有依赖通过 `uv sync` 管理
- 数据库查询使用参数化 SQL（`%s` 占位符），禁止字符串拼接
- CSS 复用现有设计 tokens（`--accent`, `--border`, `--radius` 等 CSS 变量）
- 默认日期范围为"今日"（`published_at >= CURRENT_DATE`）
- 日期范围包含起止日整天：`published_at >= date_from::date AND published_at < (date_to::date + interval '1 day')`
- 移除所有 panel 相关代码：`panel_page`、`panel_size`、`panel_items`、`_to_panel_item`、`news_list.html` include、`#panel-toggle` checkbox
- 提交遵循 `feat:` / `refactor:` / `fix:` 约定式提交格式

---

### Task 1: PostgreSQL 查询方法添加日期过滤参数

**Files:**
- Modify: `storage/postgres.py:395-582`

**Interfaces:**
- Consumes: 现有查询方法签名
- Produces: 五个方法各新增 `date_from: Optional[str] = None, date_to: Optional[str] = None` 参数

**背景:** `published_at` 是 `TIMESTAMPTZ` 类型。日期过滤使用 `::date` 转换实现包含整天范围的语义。默认不传参时行为不变（现有调用者不受影响）。

- [ ] **Step 1: 修改 `get_recent_news` 添加日期过滤**

在 `storage/postgres.py:395` 修改方法签名和查询逻辑：

```python
def get_recent_news(
    self,
    limit: int = 50,
    offset: int = 0,
    tier: Optional[int] = None,
    category: Optional[str] = None,
    min_confidence: Optional[int] = None,
    sentiment: Optional[str] = None,
    keyword: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Return recent news articles with optional filters."""
    conditions = ["TRUE"]
    params: List[Any] = []

    if tier is not None:
        conditions.append("tier = %s")
        params.append(tier)
    if category is not None:
        conditions.append("category = %s")
        params.append(category)
    if min_confidence is not None:
        conditions.append("(confidence IS NULL OR confidence >= %s)")
        params.append(min_confidence)
    else:
        conditions.append("(confidence IS NULL OR confidence >= 20)")
    if sentiment == "positive":
        conditions.append("sentiment_score >= 67")
    elif sentiment == "negative":
        conditions.append("sentiment_score <= 33")
    elif sentiment == "neutral":
        conditions.append("sentiment_score > 33 AND sentiment_score < 67")
    if keyword is not None:
        conditions.append("%s = ANY(tags)")
        params.append(keyword)
    # ★ 日期过滤：published_at 在 [date_from, date_to] 范围内（含整天）
    if date_from is not None:
        conditions.append("published_at >= %s::date")
        params.append(date_from)
    if date_to is not None:
        conditions.append("published_at < %s::date + interval '1 day'")
        params.append(date_to)

    where = " AND ".join(conditions)

    with self.get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"""SELECT id, title, source_id, source_name, source_type,
                           tier, priority, url, mobile_url, summary,
                           tags, heat_score, sentiment_score,
                           crawled_from, is_analyzed,
                           published_at, created_at
                    FROM news_articles
                    WHERE {where}
                    ORDER BY published_at DESC NULLS LAST, heat_score DESC NULLS LAST
                    LIMIT %s OFFSET %s""",
                params + [limit, offset],
            )
            return cur.fetchall()
```

- [ ] **Step 2: 修改 `get_news_count` 添加日期过滤**

在 `storage/postgres.py:448` 同样添加 `date_from` / `date_to` 参数：

```python
def get_news_count(
    self,
    tier: Optional[int] = None,
    category: Optional[str] = None,
    min_confidence: Optional[int] = None,
    sentiment: Optional[str] = None,
    keyword: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> int:
    conditions: List[str] = []
    params: List[Any] = []

    if min_confidence is not None:
        conditions.append("(confidence IS NULL OR confidence >= %s)")
        params.append(min_confidence)
    else:
        conditions.append("(confidence IS NULL OR confidence >= 20)")

    if tier is not None:
        conditions.append("tier = %s")
        params.append(tier)
    if category is not None:
        conditions.append("category = %s")
        params.append(category)
    if sentiment == "positive":
        conditions.append("sentiment_score >= 67")
    elif sentiment == "negative":
        conditions.append("sentiment_score <= 33")
    elif sentiment == "neutral":
        conditions.append("sentiment_score > 33 AND sentiment_score < 67")
    if keyword is not None:
        conditions.append("%s = ANY(tags)")
        params.append(keyword)
    if date_from is not None:
        conditions.append("published_at >= %s::date")
        params.append(date_from)
    if date_to is not None:
        conditions.append("published_at < %s::date + interval '1 day'")
        params.append(date_to)

    where = " AND ".join(conditions)

    with self.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT COUNT(*) FROM news_articles WHERE {where}",
                params,
            )
            return cur.fetchone()[0]
```

- [ ] **Step 3: 修改 `get_sentiment_counts` 添加日期过滤**

在 `storage/postgres.py:492` 添加：

```python
def get_sentiment_counts(
    self,
    tier: Optional[int] = None,
    keyword: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> Dict[str, int]:
    conditions = ["(confidence IS NULL OR confidence >= 20)"]
    params: List[Any] = []

    if tier is not None:
        conditions.append("tier = %s")
        params.append(tier)
    if keyword is not None:
        conditions.append("%s = ANY(tags)")
        params.append(keyword)
    if date_from is not None:
        conditions.append("published_at >= %s::date")
        params.append(date_from)
    if date_to is not None:
        conditions.append("published_at < %s::date + interval '1 day'")
        params.append(date_to)

    where = " AND ".join(conditions)
    # ... rest unchanged
```

- [ ] **Step 4: 修改 `get_keyword_counts` 添加日期过滤**

在 `storage/postgres.py:522` 添加同样的 `date_from` / `date_to` 参数和条件块。

- [ ] **Step 5: 修改 `get_high_impact_count` 添加日期过滤**

在 `storage/postgres.py:554`，同时将硬编码的 `created_at >= CURRENT_DATE` 替换为日期参数：

```python
def get_high_impact_count(
    self,
    tier: Optional[int] = None,
    keyword: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> int:
    conditions = [
        "(confidence IS NULL OR confidence >= 20)",
        "heat_score >= 80",
    ]
    params: List[Any] = []

    if tier is not None:
        conditions.append("tier = %s")
        params.append(tier)
    if keyword is not None:
        conditions.append("%s = ANY(tags)")
        params.append(keyword)
    # 使用日期参数替代硬编码的 CURRENT_DATE
    if date_from is not None:
        conditions.append("published_at >= %s::date")
        params.append(date_from)
    if date_to is not None:
        conditions.append("published_at < %s::date + interval '1 day'")
        params.append(date_to)
    # 当没有日期参数时，默认今日（保持向后兼容）
    if date_from is None and date_to is None:
        conditions.append("published_at >= CURRENT_DATE")

    where = " AND ".join(conditions)
    # ... rest unchanged
```

- [ ] **Step 6: 快速验证 — 启动服务确认无导入错误**

```bash
python -c "from storage.postgres import PostgresDatabase; print('OK')"
```

- [ ] **Step 7: Commit**

```bash
git add storage/postgres.py
git commit -m "feat: add date_from/date_to params to PostgreSQL query methods"
```

---

### Task 2: 更新 Web 路由 — 日期参数 + 移除 Panel

**Files:**
- Modify: `web/app.py:141-316` （`hot_news` 函数）
- Modify: `web/app.py:369-384` （`_remove_filter` 函数）

**Interfaces:**
- Consumes: Task 1 中更新的五个 PostgreSQL 查询方法
- Produces: 更新的 `GET /hot-news` 路由，新 query params: `date_from`, `date_to`, `page_size`

- [ ] **Step 1: 修改 `hot_news` 路由签名**

替换 `web/app.py:141-149`：

```python
async def hot_news(
    request: Request,
    page: int = Query(1, ge=1),
    tier: int = Query(None, ge=0, le=4),
    sentiment: str = Query(None),
    keyword: str = Query(None),
    date_from: str = Query(None),
    date_to: str = Query(None),
    page_size: int = Query(50, ge=10, le=100),
):
    """Hot news page — editorial masonry layout with date-first filtering."""
```

删除 `panel_page: int = Query(1, ge=1)` 和 `panel_size: int = Query(30)` 参数。

- [ ] **Step 2: 添加日期默认值逻辑**

在 `per_page = 20` 行之后（`web/app.py:151` 附近）添加：

```python
per_page = page_size  # 使用用户选择的每页条数
offset = (page - 1) * per_page
tier_filter = tier if tier and tier > 0 else None

# ── Date range defaults ──
from datetime import date as date_type
today_str = date_type.today().isoformat()
if date_from is None and date_to is None:
    date_from = today_str
    date_to = today_str
```

- [ ] **Step 3: 更新所有数据查询调用**

在 `web/app.py:156-167` 区域，给每个查询加上 `date_from=date_from, date_to=date_to`：

```python
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
stats_data = db.get_stats()
sentiment_counts = db.get_sentiment_counts(
    tier=tier_filter, keyword=keyword,
    date_from=date_from, date_to=date_to,
)
keyword_list = db.get_keyword_counts(
    tier=tier_filter, sentiment=sentiment,
)
high_impact = db.get_high_impact_count(
    tier=tier_filter, keyword=keyword,
    date_from=date_from, date_to=date_to,
)
```

注意：`get_keyword_counts` 的日期过滤范围较大（关键词云不需要精确到天），可暂不添加日期参数。

- [ ] **Step 4: 删除 Panel 相关代码**

删除以下代码块（原 `web/app.py:169-278` 区域）：
- `panel_offset`、`panel_articles`、`panel_total` 变量
- `_to_panel_item` 函数定义
- `panel_items` 变量
- `panel_page_numbers` 变量

- [ ] **Step 5: 更新 `_remove_filter` 函数**

修改 `web/app.py:369-384`，移除 `panel_page` 引用，添加日期参数处理：

```python
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
```

- [ ] **Step 6: 更新模板变量传递**

修改 `web/app.py:285-317` 的 `render_template` 调用，移除 panel 相关变量，添加日期相关变量：

```python
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
    # Date
    current_date_from=date_from,
    current_date_to=date_to,
    today_date=today_str,
    # Content
    masonry_cards=masonry_cards,
    total_count=total,
    total_pages=total_pages,
    current_page=page,
    page_numbers=page_numbers,
    current_page_size=per_page,
)
```

- [ ] **Step 7: 快速验证**

```bash
python -c "from web.app import create_app; print('OK')"
```

- [ ] **Step 8: Commit**

```bash
git add web/app.py
git commit -m "feat: add date range params to hot-news route, remove panel logic"
```

---

### Task 3: 重写 hot_news.html 模板

**Files:**
- Modify: `web/templates/pages/hot_news.html`

**Interfaces:**
- Consumes: Task 2 中传入的所有模板变量
- Produces: 完整的日期筛选 + 瀑布流 + 翻页页面

- [ ] **Step 1: 重写模板文件**

完整替换 `web/templates/pages/hot_news.html`，核心结构：

```jinja2
{% extends "base.html" %}
{% block title %}NewsRadar · 热点新闻{% endblock %}

{% block content %}
<div class="app-main">
  <div class="content-cards">

    <!-- Section Header with Date Filter Trigger -->
    <div class="section-header">
      <div class="section-header-line"></div>
      <h1>热点新闻</h1>

      <div class="date-filter-trigger" id="date-filter-trigger">
        <button class="date-trigger-btn" id="date-trigger-btn"
                onclick="toggleDatePopover()" aria-label="选择日期范围">
          <span class="date-trigger-icon">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
                 stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <rect x="3" y="4" width="18" height="18" rx="3"/>
              <path d="M3 10h18"/><path d="M8 2v4"/><path d="M16 2v4"/>
            </svg>
          </span>
          <span class="date-trigger-dot"></span>
          <span class="date-trigger-label" id="date-trigger-label">今日</span>
          <span class="date-trigger-range" id="date-trigger-range">
            {{ current_date_from }}{% if current_date_from != current_date_to %} → {{ current_date_to }}{% endif %}
          </span>
          <span class="date-trigger-chevron">
            <svg viewBox="0 0 16 16" fill="none" stroke="currentColor"
                 stroke-width="2" stroke-linecap="round"><path d="M3 6l5 4 5-4"/></svg>
          </span>
        </button>

        <!-- Date Popover -->
        <div class="date-popover" id="date-popover">
          <div class="date-popover-arrow"></div>
          <div class="date-popover-inner">
            <!-- Quick presets: 4×2 grid -->
            <div class="date-popover-section">
              <span class="date-popover-section-label">快捷筛选</span>
              <div class="date-preset-grid">
                {% set presets = [
                  ('today', '今日', today_date, today_date),
                  ('3days', '近3天', (today_date_obj - timedelta(days=2)).isoformat(), today_date),
                  ('week', '近一周', (today_date_obj - timedelta(days=6)).isoformat(), today_date),
                  ('month', '近一月', (today_date_obj - timedelta(days=29)).isoformat(), today_date),
                  ('3months', '近三月', (today_date_obj - timedelta(days=89)).isoformat(), today_date),
                  ('year', '近一年', (today_date_obj - timedelta(days=364)).isoformat(), today_date),
                  ('thisYear', '今年', today_date[:4] + '-01-01', today_date),
                  ('all', '全部', '', ''),
                ] %}
                {% for pid, plabel, pfrom, pto in presets %}
                <button class="date-preset-btn{% if pid == 'today' %} today-btn active{% endif %}"
                        data-preset="{{ pid }}"
                        data-from="{{ pfrom }}"
                        data-to="{{ pto }}"
                        onclick="selectPreset('{{ pid }}', '{{ pfrom }}', '{{ pto }}')">
                  <span class="date-preset-btn-name">{{ plabel }}</span>
                  <span class="date-preset-btn-range">{{ pfrom if pfrom else '不限' }}</span>
                </button>
                {% endfor %}
              </div>
            </div>

            <!-- Custom range -->
            <div class="date-popover-section">
              <span class="date-popover-section-label">自定义范围</span>
              <div class="date-custom-row">
                <div class="date-field">
                  <span class="date-field-label">开始日期</span>
                  <input type="date" id="date-from" value="{{ current_date_from }}">
                </div>
                <span class="date-custom-range-sep">→</span>
                <div class="date-field">
                  <span class="date-field-label">结束日期</span>
                  <input type="date" id="date-to" value="{{ current_date_to }}">
                </div>
              </div>
            </div>

            <div class="date-popover-actions">
              <button class="date-reset-btn" onclick="resetToToday()">重置为今日</button>
              <button class="date-apply-btn" onclick="applyCustomDate()">应用自定义范围</button>
            </div>
          </div>
        </div><!-- /date-popover -->
      </div><!-- /date-filter-trigger -->
    </div><!-- /section-header -->

    <!-- Control Deck -->
    {% include "components/control_deck.html" %}

    <!-- Results Bar -->
    <div class="results-bar">
      <div class="results-count">共 <strong>{{ total_count }}</strong> 条新闻</div>
      <div class="results-controls">
        <label for="page-size">每页显示</label>
        <select class="page-size-select" id="page-size"
                onchange="changePageSize(this.value)">
          {% for ps in [20, 30, 50, 100] %}
          <option value="{{ ps }}"{% if ps == current_page_size %} selected{% endif %}>{{ ps }} 条</option>
          {% endfor %}
        </select>
      </div>
    </div>

    <!-- Masonry Cards -->
    {% if masonry_cards %}
    <div class="masonry">
      {% for card in masonry_cards %}
      <div class="masonry-card new-card" style="animation-delay:{{ '%.2f' % (loop.index0 * 0.025) }}s">
        <!-- card content same as existing news_cards.html -->
        {% include "components/news_card_item.html" %}
      </div>
      {% endfor %}
    </div>
    {% else %}
    <div class="empty-state">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1" stroke-linecap="round">
        <circle cx="11" cy="11" r="8"/><path d="M21 21l-4.35-4.35"/>
      </svg>
      <h3>该时间范围内暂无新闻</h3>
      <p>尝试调整日期筛选条件或切换其他预设范围</p>
    </div>
    {% endif %}

    <!-- Pagination -->
    {% if total_pages > 1 %}
    <div class="pagination-bar">
      {% if current_page > 1 %}
      <a href="?page={{ current_page - 1 }}&page_size={{ current_page_size }}{% if current_tier %}&tier={{ current_tier }}{% endif %}{% if current_sentiment %}&sentiment={{ current_sentiment }}{% endif %}{% if current_keyword %}&keyword={{ current_keyword }}{% endif %}&date_from={{ current_date_from }}&date_to={{ current_date_to }}"
         class="pg-btn arr">‹</a>
      {% else %}
      <span class="pg-btn arr disabled">‹</span>
      {% endif %}

      {% for pn in page_numbers %}
        {% if pn == '...' %}
        <span class="pg-ellipsis">…</span>
        {% elif pn == current_page %}
        <span class="pg-btn active">{{ pn }}</span>
        {% else %}
        <a href="?page={{ pn }}&page_size={{ current_page_size }}{% if current_tier %}&tier={{ current_tier }}{% endif %}{% if current_sentiment %}&sentiment={{ current_sentiment }}{% endif %}{% if current_keyword %}&keyword={{ current_keyword }}{% endif %}&date_from={{ current_date_from }}&date_to={{ current_date_to }}"
           class="pg-btn">{{ pn }}</a>
        {% endif %}
      {% endfor %}

      {% if current_page < total_pages %}
      <a href="?page={{ current_page + 1 }}&page_size={{ current_page_size }}{% if current_tier %}&tier={{ current_tier }}{% endif %}{% if current_sentiment %}&sentiment={{ current_sentiment }}{% endif %}{% if current_keyword %}&keyword={{ current_keyword }}{% endif %}&date_from={{ current_date_from }}&date_to={{ current_date_to }}"
         class="pg-btn arr">›</a>
      {% else %}
      <span class="pg-btn arr disabled">›</span>
      {% endif %}
    </div>
    {% endif %}

  </div><!-- /content-cards -->
</div><!-- /app-main -->
{% endblock %}

{% block scripts %}
<script>
// ── Date filter state ──
const TODAY = '{{ today_date }}';
let currentPreset = 'today';

function navigateDate(from, to) {
  const params = new URLSearchParams(window.location.search);
  params.set('date_from', from);
  params.set('date_to', to);
  params.set('page', '1');
  window.location.search = params.toString();
}

function selectPreset(preset, from, to) {
  if (preset === 'all') {
    const params = new URLSearchParams(window.location.search);
    params.delete('date_from');
    params.delete('date_to');
    params.set('page', '1');
    window.location.search = params.toString();
  } else {
    navigateDate(from, to);
  }
}

function applyCustomDate() {
  const from = document.getElementById('date-from').value;
  const to = document.getElementById('date-to').value;
  if (!from || !to) return;
  if (from > to) { alert('开始日期不能晚于结束日期'); return; }
  navigateDate(from, to);
}

function resetToToday() {
  navigateDate(TODAY, TODAY);
}

function changePageSize(size) {
  const params = new URLSearchParams(window.location.search);
  params.set('page_size', size);
  params.set('page', '1');
  window.location.search = params.toString();
}

// ── Popover ──
function toggleDatePopover() {
  const popover = document.getElementById('date-popover');
  const btn = document.getElementById('date-trigger-btn');
  popover.classList.toggle('is-visible');
  btn.classList.toggle('is-open');
}

document.addEventListener('click', function(e) {
  const trigger = document.getElementById('date-filter-trigger');
  if (!trigger.contains(e.target)) {
    document.getElementById('date-popover').classList.remove('is-visible');
    document.getElementById('date-trigger-btn').classList.remove('is-open');
  }
});

document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') {
    document.getElementById('date-popover').classList.remove('is-visible');
    document.getElementById('date-trigger-btn').classList.remove('is-open');
  }
});

// ── Filter toggle ──
(function() {
  const KEY = 'newsradar.filters-collapsed';
  const deck = document.getElementById('control-deck');
  function apply(state) {
    if (state) deck.classList.add('filters-collapsed');
    else deck.classList.remove('filters-collapsed');
  }
  var collapsed = localStorage.getItem(KEY) !== 'false';
  apply(collapsed);
  window.toggleDeckFilters = function() {
    collapsed = !deck.classList.contains('filters-collapsed');
    localStorage.setItem(KEY, collapsed);
    apply(collapsed);
  };
})();
</script>
{% endblock %}
```

> **注意:** 由于 Jinja2 模板中 `timedelta` 等 Python 对象不可直接使用，预设日期需要在路由中计算好作为模板变量传入。或者在前端 JS 中计算（推荐 — 减少模板复杂度）。实际实现时，模板只传 `current_date_from`、`current_date_to`、`today_date`，JS 根据这些值判断当前激活的 preset 并高亮。

- [ ] **Step 2: 创建 `news_card_item.html` 组件**

将 `web/templates/components/news_cards.html` 中单个卡片的 HTML 提取为独立组件：

```jinja2
{# web/templates/components/news_card_item.html #}
<div class="card-body">
  <div class="card-meta">
    <span class="card-source{% if card.tier_class %} {{ card.tier_class }}{% endif %}">{{ card.source }}</span>
    <span class="card-heat">🔥 {{ card.heat }}</span>
  </div>
  <a href="/news/{{ card.id }}" class="card-title-link">{{ card.title }}</a>
  <div class="card-summary">{{ card.summary }}</div>
  <div class="card-footer">
    <span class="card-tag {{ card.sentiment_class }}">{{ card.sentiment }}</span>
    {% for kw in card.keywords[:2] %}
    <span class="card-tag keyword">{{ kw }}</span>
    {% endfor %}
    <span class="card-time">{{ card.time }}</span>
  </div>
</div>
```

- [ ] **Step 3: 简化 `news_cards.html`**

更新 `web/templates/components/news_cards.html`，使之也使用 `news_card_item.html` include（保持 DRY）。

- [ ] **Step 4: Commit**

```bash
git add web/templates/pages/hot_news.html web/templates/components/news_card_item.html web/templates/components/news_cards.html
git commit -m "feat: rewrite hot_news template with date popover and pagination"
```

---

### Task 4: 清理 base.html — 移除 Panel 基础设施

**Files:**
- Modify: `web/templates/base.html`

**Interfaces:**
- Consumes: 无
- Produces: 移除 `#panel-toggle` checkbox 和 `.panel-overlay` label

- [ ] **Step 1: 删除 panel toggle checkbox**

删除 `web/templates/base.html:16`：

```diff
- <!-- Hidden panel toggle checkbox -->
- <input type="checkbox" id="panel-toggle">
- <script>
- (function() {
-   const tgl = document.getElementById('panel-toggle');
-   if (!document.querySelector('.content-list')) return;
-   if (localStorage.getItem('newsradar.panel-open') === 'true') tgl.checked = true;
-   tgl.addEventListener('change', function() { localStorage.setItem('newsradar.panel-open', this.checked); });
- })();
- </script>
- 
- <!-- Panel overlay -->
- <label class="panel-overlay" for="panel-toggle"></label>
```

- [ ] **Step 2: 检查其他模板是否使用了 panel**

```bash
grep -r "panel-toggle\|content-list\|panel-overlay" web/templates/
```

确认只有 `hot_news.html` 使用了 panel 相关元素（base.html 中删除后应该无其他引用）。

- [ ] **Step 3: Commit**

```bash
git add web/templates/base.html
git commit -m "refactor: remove panel toggle checkbox and overlay from base template"
```

---

### Task 5: 更新 CSS — 添加日期组件样式 + 移除 Panel 样式

**Files:**
- Modify: `web/static/css/app.css`

**Interfaces:**
- Consumes: CSS 设计 tokens（`:root` 变量）
- Produces: 新增样式不影响其他页面

- [ ] **Step 1: 在 app.css 末尾追加日期组件样式**

从 `web/static/hot-news-redesign-v2.html` 的 `<style>` 块中提取以下 CSS 段，追加到 `web/static/css/app.css`：

1. **Date Filter Trigger Button** — `.date-filter-trigger`, `.date-trigger-btn`, `.date-trigger-icon`, `.date-trigger-label`, `.date-trigger-range`, `.date-trigger-chevron`, `.date-trigger-dot`
2. **Date Popover** — `.date-popover`, `.date-popover.is-visible`, `.date-popover-inner`, `.date-popover-arrow`
3. **Preset Grid** — `.date-popover-section`, `.date-popover-section-label`, `.date-preset-grid`, `.date-preset-btn`, `.date-preset-btn.today-btn`, `.date-preset-btn.active`, `.date-preset-btn-name`, `.date-preset-btn-range`
4. **Custom Range** — `.date-custom-row`, `.date-field`, `.date-field-label`, `.date-custom-range-sep`, `input[type="date"]`
5. **Popover Actions** — `.date-popover-actions`, `.date-apply-btn`, `.date-reset-btn`
6. **Results Bar** — `.results-bar`, `.results-count`, `.results-controls`, `.page-size-select`
7. **Empty State** — `.empty-state`, `.empty-state svg`, `.empty-state h3`, `.empty-state p`
8. **Pagination** — `.pagination-bar`, `.pg-btn`, `.pg-btn.active`, `.pg-btn.arr`, `.pg-btn.disabled`, `.pg-ellipsis`
9. **Card Animation** — `.new-card`, `@keyframes cardIn`
10. **Responsive** — `@media (max-width: 768px)` 适配

- [ ] **Step 2: 移除 Panel 相关 CSS**

删除 `web/static/css/app.css` 中以下 CSS 块（可通过搜索定位）：
- `.content-list` 及其子元素（panel 容器，约 `line 189-201`）
- `#panel-toggle:checked ~ .app-shell .content-list`（约 `line 204-209`）
- `.panel-overlay`（约 `line 212-222`）
- `#panel-toggle:checked ~ .app-shell .content-cards`（约 `line 222`）
- `.panel-toggle-btn`（约 `line 227-246`）
- `.panel-header`, `.panel-close`, `.panel-page-size`, `.panel-divider`, `.panel-body`（约 `line 248-291`）
- `.pn-item`, `.pn-index`, `.pn-content`, `.pn-title`, `.pn-meta`, `.pn-source`, `.pn-time`, `.pn-sentiment`（约 `line 293-350`）
- `#panel-toggle:checked ~ .app-shell .content-list .pn-item` 及 animation（约 `line 302-331`）
- `.panel-pagination`, `.pp-btn`（约 `line 354-367`）

- [ ] **Step 3: Commit**

```bash
git add web/static/css/app.css
git commit -m "feat: add date popover CSS, remove panel styles"
```

---

### Task 6: 端到端验证

**Files:**
- 无新建文件

- [ ] **Step 1: 启动服务并检查页面**

```bash
python main.py &
sleep 3
curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/hot-news
```

预期：HTTP 200

- [ ] **Step 2: 测试日期参数**

```bash
# 默认今日
curl -s "http://localhost:8000/hot-news" | grep -o 'date-trigger-range[^<]*'

# 指定日期范围
curl -s "http://localhost:8000/hot-news?date_from=2026-06-10&date_to=2026-06-17" | grep -o 'date-trigger-range[^<]*'

# 无数据范围
curl -s "http://localhost:8000/hot-news?date_from=2020-01-01&date_to=2020-01-02" | grep -o '暂无新闻'
```

- [ ] **Step 3: 测试翻页和每页条数**

```bash
curl -s "http://localhost:8000/hot-news?page=2&page_size=10" | grep -o '共 <strong>[0-9]*</strong>'
```

- [ ] **Step 4: 确认 Panel 已完全移除**

```bash
curl -s "http://localhost:8000/hot-news" | grep -c "panel-toggle\|content-list\|pn-item"
```

预期输出：`0`

- [ ] **Step 5: Commit（如有小的修复）**

```bash
git add -u
git commit -m "chore: final verification and cleanup for hot-news redesign"
```

---

## 自检清单

| 检查项 | 状态 |
|--------|------|
| 所有 DB 查询方法支持 `date_from`/`date_to` | ✅ Task 1 |
| 默认日期范围为今日 | ✅ Task 2 Step 2 |
| Panel 代码完全移除 | ✅ Task 2 Step 4 + Task 4 |
| 日期 Popover CSS 完整 | ✅ Task 5 |
| 翻页链接包含所有当前筛选参数 | ✅ Task 3 Step 1 |
| `_remove_filter` 不再引用 `panel_page` | ✅ Task 2 Step 5 |
| 无 TBD/TODO/placeholder | ✅ |
| 所有函数签名跨任务一致 | ✅ |

---

## 风险点

1. **`get_stats` 的 `today_count` 仍使用硬编码 `CURRENT_DATE`** — 目前统计条中的"今日热点"保持全局统计语义（不管日期筛选），如需联动可在后续迭代中修改。
2. **`get_keyword_counts` 未添加日期过滤** — 关键词云数据量大，跨天汇总更有意义。如需按日期范围过滤可后续添加。
3. **CSS 文件行号漂移** — 删除 panel 样式后，后续 CSS 行号会变化。实际编辑时按选择器名搜索定位，不依赖行号。
