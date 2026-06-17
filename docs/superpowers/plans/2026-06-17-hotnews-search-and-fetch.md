# 热点新闻 — 搜索 + 手动抓取 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在热点新闻页面增加全文搜索（标题+摘要）和手动提交URL抓取两个功能。

**Architecture:** 利用已有的 `idx_fulltext` GIN 索引（`to_tsvector('simple', title || ' ' || summary || ' ' || content)`）做 PostgreSQL 全文搜索，在 control-deck 和 results-bar 之间插入独立 action-bar 承载搜索框和提交按钮，Modal 弹窗收集 URL 后通过新 API 端点触发后台抓取。

**Tech Stack:** Python/FastAPI + Jinja2 + vanilla JS + PostgreSQL full-text search + existing CSS design tokens

## Global Constraints

- 复用现有设计系统（editorial warm-tone，DM Sans + Newsreader 字体，CSS custom properties）
- 遵循现有代码模式：TDD（先写测试）、不可变数据、类型注解
- 测试覆盖率 ≥ 80%
- 搜索参数命名为 `search`（全文搜索），与现有 `keyword`（标签精确匹配）区分
- 新 API 端点路径：`POST /api/news/fetch`
- action-bar 位于 control-deck 和 results-bar 之间，始终可见
- 通知系统复用现有铃铛/Toast/轮询机制，不新增

---

## File Structure

```
Modify: storage/postgres.py        — 6个查询方法新增 search 参数
Modify: web/app.py                 — 新增 POST /api/news/fetch + hot_news 路由改造
Modify: web/templates/pages/hot_news.html — action-bar + Modal + JS
Modify: web/static/css/app.css     — 新增 ~200 行样式
```

### 接口契约

各层之间通过以下签名通信：

```
PostgreSQL.search: Optional[str]    → WHERE to_tsvector(...) @@ plainto_tsquery('simple', search)
hot_news(search=...)                → 穿透给 get_recent_news / get_news_count / get_stats /
                                      get_sentiment_counts / get_keyword_counts / get_high_impact_count
POST /api/news/fetch {url: str}     → {"ok": True, "message": "..."} | {"ok": False, "error": "..."}
action-bar search input             → window.location.search = "?search=xxx&page=1" (保留其他参数)
```

---

### Task 1: 后端 — storage/postgres.py 6 个方法新增 search 参数

**Files:**
- Modify: `storage/postgres.py:395-455` (get_recent_news)
- Modify: `storage/postgres.py:457-507` (get_news_count)
- Modify: `storage/postgres.py:509-545` (get_sentiment_counts)
- Modify: `storage/postgres.py:547-580` (get_keyword_counts)
- Modify: `storage/postgres.py:587-626` (get_high_impact_count)
- Modify: `storage/postgres.py:664-699` (get_stats)

**Interfaces:**
- Produces: 6 个方法均新增 `search: Optional[str] = None` 参数。当 search 非空时，追加 `to_tsvector('simple', title || ' ' || COALESCE(summary, '')) @@ plainto_tsquery('simple', %s)` 条件
- Consumes: 已有 `idx_fulltext` GIN 索引（postgres.sql:70）

- [ ] **Step 1: 给 `get_recent_news` 新增 search 参数**

在 `storage/postgres.py` 的 `get_recent_news` 方法签名中新增 `search` 参数，并在 where 条件构建块中加入全文搜索条件。

当前签名（第395-406行）：
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
```

在 `keyword` 参数后插入 `search` 参数：
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
    search: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> List[Dict[str, Any]]:
```

在该方法的 conditions 构建区（`if keyword is not None:` 块之后，`if date_from is not None:` 块之前，约第429行后）加入：

```python
        if search is not None:
            conditions.append(
                "to_tsvector('simple', title || ' ' || COALESCE(summary, '')) "
                "@@ plainto_tsquery('simple', %s)"
            )
            params.append(search)
```

- [ ] **Step 2: 给 `get_news_count` 新增 search 参数**

同方法签名（第457-466行），在 `keyword` 后插入 `search: Optional[str] = None`。

在 `if keyword is not None:` 块之后（约第491行后）加入相同的全文搜索条件块。

- [ ] **Step 3: 给 `get_sentiment_counts` 新增 search 参数**

当前签名（第509-515行）：
```python
def get_sentiment_counts(
    self,
    tier: Optional[int] = None,
    keyword: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> Dict[str, int]:
```

在 `keyword` 后插入 `search: Optional[str] = None`。

在 `if keyword is not None:` 块之后（约第525行后）加入相同的全文搜索条件块。

- [ ] **Step 4: 给 `get_keyword_counts` 新增 search 参数**

当前签名（第547-554行），在 `sentiment` 后插入 `search: Optional[str] = None`。

在 `if sentiment == "neutral":` 块之后（约第566行后）加入相同的全文搜索条件块。

注意：`get_keyword_counts` 没有 `keyword` 参数，所以插入位置在 sentiment 条件块之后。

- [ ] **Step 5: 给 `get_high_impact_count` 新增 search 参数**

当前签名（第587-593行），在 `keyword` 后插入 `search: Optional[str] = None`。

在 `if keyword is not None:` 块之后（约第606行后）加入相同的全文搜索条件块。

- [ ] **Step 6: 给 `get_stats` 新增 search 参数**

当前签名（第664行）：
```python
def get_stats(self, date_from: Optional[str] = None, date_to: Optional[str] = None) -> Dict[str, Any]:
```

改为：
```python
def get_stats(self, date_from: Optional[str] = None, date_to: Optional[str] = None,
              search: Optional[str] = None) -> Dict[str, Any]:
```

在该方法的 conditions 构建区（`if date_to:` 块之后，约第672行后）加入相同的全文搜索条件块。

- [ ] **Step 7: 运行现有测试确保不破坏已有功能**

```bash
cd /home/llianc62/ws/NewsRadar && uv run pytest tests/ -v -k "postgres" 2>&1 | tail -30
```

预期：已有测试全部 PASS（新增参数有默认值 None，向后兼容）。

- [ ] **Step 8: 提交**

```bash
git add storage/postgres.py
git commit -m "feat: add full-text search parameter to 6 PostgreSQL query methods"
```

---

### Task 2: 后端 — web/app.py hot_news 路由增加 search 参数穿透

**Files:**
- Modify: `web/app.py:222-382`

**Interfaces:**
- Consumes: `PostgreSQL.get_recent_news(search=...)`, `PostgreSQL.get_news_count(search=...)`, `PostgreSQL.get_stats(search=...)`, `PostgreSQL.get_sentiment_counts(search=...)`, `PostgreSQL.get_keyword_counts(search=...)`, `PostgreSQL.get_high_impact_count(search=...)`
- Produces: `current_search` 模板变量

- [ ] **Step 1: hot_news 函数签名新增 search 查询参数**

在 `web/app.py` 第222行附近，`hot_news` 函数参数中，在 `keyword` 后新增：

```python
search: str = Query(None),
```

- [ ] **Step 2: 所有 DB 调用传入 search**

将 `search=search` 传入以下 6 个调用：

```python
# 第251行附近 — get_recent_news
articles = db.get_recent_news(
    limit=per_page, offset=offset,
    tier=tier_filter, sentiment=sentiment, keyword=keyword,
    search=search,
    date_from=date_from, date_to=date_to,
)

# 第257行附近 — get_news_count
total = db.get_news_count(
    tier=tier_filter, sentiment=sentiment, keyword=keyword,
    search=search,
    date_from=date_from, date_to=date_to,
)

# 第262行附近 — get_stats
stats_data = db.get_stats(date_from=date_from, date_to=date_to, search=search)

# 第263行附近 — get_sentiment_counts
sentiment_counts = db.get_sentiment_counts(
    tier=tier_filter, keyword=keyword,
    search=search,
    date_from=date_from, date_to=date_to,
)

# 第267行附近 — get_keyword_counts
keyword_list = db.get_keyword_counts(
    tier=tier_filter, sentiment=sentiment,
    search=search,
    date_from=date_from, date_to=date_to,
)

# 第271行附近 — get_high_impact_count
high_impact = db.get_high_impact_count(
    tier=tier_filter, keyword=keyword,
    search=search,
    date_from=date_from, date_to=date_to,
)
```

- [ ] **Step 3: active_filters 中增加 search 类型的过滤标签展示**

在 `active_filters` 构建区（约第304-323行），在 `if keyword:` 块之后加入：

```python
        if search:
            active_filters.append({
                "label": f"搜索: {search}",
                "type": "search",
                "remove_url": _remove_filter(request, "search"),
            })
```

- [ ] **Step 4: 模板上下文传入 current_search**

在 `render_template` 调用（约第352行）的参数中新增：

```python
        current_search=search,
```

- [ ] **Step 5: _remove_filter 清理逻辑中增加 search**

在 `_remove_filter` 函数（第497行）的清理循环中，在 `"keyword"` 后加入 `"search"`：

```python
    for k in ("tier", "sentiment", "keyword", "search", "date_from", "date_to"):
```

- [ ] **Step 6: 分页链接保留 search 参数**

在所有分页链接（`pagination-bar` 区域的 `href`）中增加 `search` 参数。这些链接在 `hot_news.html` 模板中，将在 Task 4 中处理。

- [ ] **Step 7: 提交**

```bash
git add web/app.py
git commit -m "feat: plumb search parameter through hot_news route to all DB queries"
```

---

### Task 3: 后端 — storage/postgres.py 新增 get_article_by_url + web/app.py 新增 POST /api/news/fetch

**Files:**
- Modify: `storage/postgres.py`（追加方法）
- Modify: `web/app.py:94` 附近（新增 `_run_fetch_url`），`:430` 附近（新增路由）

**Interfaces:**
- Consumes: `PostgreSQL.get_article_by_url(url)` → `Optional[dict]`, `app.state.crawler`, `_refetch_executor`
- Produces: `POST /api/news/fetch` 接受 `{"url": "https://..."}`
  - URL 已在库中 → `{"ok": True, "refetch": True, "article_id": 123}`
  - URL 不在库中 → `{"ok": True, "message": "已提交抓取任务"}`

**设计思路**：`crawler.fetch()` 已封装 download + parse + persist 全流程，不做拆解。URL 去重逻辑：提交时先查 URL 是否在库中，存在则走 refetch 刷新内容，不存在则走 `crawler.fetch()` 新入库。`_run_fetch_url` 只有一行核心逻辑。

- [ ] **Step 1: 在 storage/postgres.py 新增 `get_article_by_url` 方法**

在 `get_articles_without_content` 方法之前（约第700行），追加：

```python
    def get_article_by_url(self, url: str) -> Optional[Dict[str, Any]]:
        """Return the first article matching *url*, or None."""
        with self.get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT id, title, url FROM news_articles WHERE url = %s ORDER BY id LIMIT 1",
                    (url,),
                )
                return cur.fetchone()
```

- [ ] **Step 2: 在 web/app.py 新增 `_run_fetch_url` 后台函数**

在 `_run_refetch` 函数（第94行）之前插入：

```python
def _run_fetch_url(url: str, crawler, notif: dict) -> None:
    """Execute URL fetch in background thread — thin wrapper around crawler.fetch()."""
    from news.crawler import OutputStyle

    try:
        notif["status"] = "running"
        crawler.fetch(url, OutputStyle.POSTGRESQL)
        notif["status"] = "completed"
    except Exception as e:
        notif["status"] = "failed"
        notif["error_message"] = str(e)[:500]
```

- [ ] **Step 3: 新增 POST /api/news/fetch 路由（含 URL 去重）**

在 `trigger_sync` 端点之后（约第430行），`refetch_article` 端点之前，插入：

```python
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

        c = app.state.crawler
        if c is None:
            return JSONResponse(
                {"ok": False, "error": "抓取服务未就绪"}, status_code=503
            )

        # ── Dedup: if URL already exists, refetch instead ──
        existing = db.get_article_by_url(url)
        if existing:
            article_id = existing["id"]
            title = existing.get("title") or url
            # Dedup check for in-flight refetch tasks
            with _notification_lock:
                dup = _refetch_tasks.get(article_id)
                if dup and dup["status"] in ("pending", "running"):
                    return {"ok": False, "error": "该文章正在抓取中"}
            notif = _add_notification(article_id, title, status="pending")
            task = {"article_id": article_id, "title": title,
                    "status": "pending", "created_at": notif["created_at"]}
            with _notification_lock:
                _refetch_tasks[article_id] = task
            _refetch_executor.submit(_run_refetch, article_id, url, title,
                                     c, db, notif)
            return {"ok": True, "refetch": True, "article_id": article_id}

        # ── New URL: fetch and insert ──
        notif = _add_notification(0, url, status="pending")
        _refetch_executor.submit(_run_fetch_url, url, c, notif)

        return {"ok": True, "message": "已提交抓取任务"}
```

- [ ] **Step 4: 提交**

```bash
git add storage/postgres.py web/app.py
git commit -m "feat: add POST /api/news/fetch with URL dedup, delegate to refetch if exists"
```

---

### Task 4: 前端 — hot_news.html action-bar + Modal + JavaScript

**Files:**
- Modify: `web/templates/pages/hot_news.html`

**Interfaces:**
- Consumes: `current_search` 模板变量（来自 Task 2）
- Produces: action-bar HTML 块、Modal HTML 块、搜索防抖 JS、Modal 交互 JS

- [ ] **Step 1: 在 control_deck include 之后、results-bar 之前插入 action-bar**

在 `<!-- Control Deck -->` 的 `{% include "components/control_deck.html" %}` （第114行）之后、`<!-- Results Bar -->`（第117行）之前，插入：

```html
    <!-- Action Bar — search + submit -->
    <div class="action-bar">
      <div class="search-wrap" id="search-wrap">
        <span class="search-icon">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/>
          </svg>
        </span>
        <input
          type="text"
          class="search-input"
          id="search-input"
          placeholder="搜索标题、摘要…"
          value="{{ current_search or '' }}"
          autocomplete="off"
        >
        {% if current_search %}
        <button class="search-clear" id="search-clear" title="清除搜索" onclick="clearSearch()">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>
        </button>
        {% else %}
        <button class="search-clear" id="search-clear" title="清除搜索" style="display:none" onclick="clearSearch()">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>
        </button>
        {% endif %}
        <div class="search-spinner" id="search-spinner"></div>
      </div>

      <button class="submit-btn" id="submit-btn" onclick="openSubmitModal()">
        <span class="submit-btn-plus">+</span>
        <span class="submit-btn-label">提交新闻</span>
      </button>
    </div>

    <!-- Submit URL Modal -->
    <div class="modal-overlay" id="submit-modal-overlay" onclick="closeSubmitModal()">
      <div class="submit-modal" onclick="event.stopPropagation()">
        <div class="submit-modal-header">
          <button class="submit-modal-back" onclick="closeSubmitModal()" title="返回">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m15 18-6-6 6-6"/></svg>
          </button>
          <div class="submit-modal-title">提交新闻链接</div>
        </div>

        <div class="submit-modal-body">
          <label class="submit-modal-label">新闻链接</label>
          <input
            type="url"
            class="submit-modal-input"
            id="submit-url-input"
            placeholder="https://example.com/news/article"
            autocomplete="off"
          >
          <div class="submit-modal-hint">
            系统将自动抓取标题、正文，分析情感关键词并存入数据库。
            抓取完成后通知栏会收到提醒。
          </div>
          <div class="submit-modal-error" id="submit-modal-error"></div>
        </div>

        <div class="submit-modal-footer">
          <button class="submit-modal-cancel" onclick="closeSubmitModal()">取消</button>
          <button class="submit-modal-confirm" id="submit-modal-confirm" onclick="submitFetchUrl()">开始抓取</button>
        </div>
      </div>
    </div>
```

- [ ] **Step 2: 在分页链接中追加 search 参数**

在 pagination-bar 区域（第151-176行），每一处 URL 参数拼接中加入 search。找到所有类似 `{% if current_keyword %}&amp;keyword={{ current_keyword }}{% endif %}` 的位置（共 4 处：prev 按钮、page 链接、next 按钮），在其后增加：

```jinja2
{% if current_search %}&amp;search={{ current_search }}{% endif %}
```

同时确保 date filter JS 中的 `navigateDate` 函数也保留 search 参数。在 `navigateDate` 函数（第247行）中：

```javascript
function navigateDate(from, to) {
  var params = new URLSearchParams(window.location.search);
  if (from) params.set('date_from', from); else params.delete('date_from');
  if (to) params.set('date_to', to);       else params.delete('date_to');
  params.set('page', '1');
  // 保留 search 参数已由 URLSearchParams 自动处理，无需额外代码
  window.location.search = params.toString();
}
```

（`URLSearchParams` 从 `window.location.search` 初始化已包含 search 参数，无需改动。）

`changePageSize` 函数同理——从 `window.location.search` 构建 `URLSearchParams` 已自动保留所有参数。

- [ ] **Step 3: 在 `{% block scripts %}` 中追加搜索和 Modal 的 JavaScript**

在现有的 `<script>` 标签结束 `</script>` 之前（第352行之前），追加：

```javascript
// ── Search with debounce ──
(function() {
  var input = document.getElementById('search-input');
  var clear = document.getElementById('search-clear');
  var spinner = document.getElementById('search-spinner');
  var timer = null;
  var lastValue = input.value;

  function doSearch(value) {
    var params = new URLSearchParams(window.location.search);
    if (value) {
      params.set('search', value);
    } else {
      params.delete('search');
    }
    params.set('page', '1');
    window.location.search = params.toString();
  }

  input.addEventListener('input', function() {
    var val = this.value;
    // Show/hide clear button
    if (val.length > 0) {
      if (clear) clear.style.display = 'flex';
    } else {
      if (clear) clear.style.display = 'none';
    }

    // Don't search if value unchanged
    if (val === lastValue) return;

    // Show spinner, hide clear temporarily
    if (spinner) spinner.style.display = 'block';
    if (clear) clear.style.display = 'none';

    clearTimeout(timer);
    timer = setTimeout(function() {
      lastValue = val;
      if (spinner) spinner.style.display = 'none';
      if (val.length > 0 && clear) clear.style.display = 'flex';
      doSearch(val);
    }, 300);
  });

  // Clear button
  window.clearSearch = function() {
    input.value = '';
    lastValue = '';
    if (clear) clear.style.display = 'none';
    doSearch('');
  };

  // Keyboard: Escape to clear
  input.addEventListener('keydown', function(e) {
    if (e.key === 'Escape' && this.value) {
      e.preventDefault();
      clearSearch();
    }
  });
})();

// ── Submit URL Modal ──
function openSubmitModal() {
  document.getElementById('submit-modal-overlay').classList.add('is-open');
  document.body.style.overflow = 'hidden';
  setTimeout(function() {
    var urlInput = document.getElementById('submit-url-input');
    if (urlInput) urlInput.focus();
  }, 350);
}

function closeSubmitModal() {
  document.getElementById('submit-modal-overlay').classList.remove('is-open');
  document.body.style.overflow = '';
  // Reset error state
  var errorEl = document.getElementById('submit-modal-error');
  if (errorEl) errorEl.textContent = '';
  var input = document.getElementById('submit-url-input');
  if (input) input.classList.remove('has-error');
}

function submitFetchUrl() {
  var input = document.getElementById('submit-url-input');
  var btn = document.getElementById('submit-modal-confirm');
  var errorEl = document.getElementById('submit-modal-error');
  var url = (input.value || '').trim();

  // Validate
  if (!url) {
    input.classList.add('has-error');
    errorEl.textContent = '请输入新闻链接';
    input.focus();
    return;
  }
  if (!/^https?:\/\//.test(url)) {
    input.classList.add('has-error');
    errorEl.textContent = '链接必须以 http:// 或 https:// 开头';
    input.focus();
    return;
  }

  input.classList.remove('has-error');
  errorEl.textContent = '';

  // Loading state
  btn.disabled = true;
  btn.textContent = '提交中…';

  fetch('/api/news/fetch', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ url: url }),
  })
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data.ok) {
        closeSubmitModal();
        input.value = '';
      } else {
        errorEl.textContent = data.error || '提交失败，请重试';
      }
    })
    .catch(function() {
      errorEl.textContent = '网络错误，请检查连接后重试';
    })
    .finally(function() {
      btn.disabled = false;
      btn.textContent = '开始抓取';
    });
}

// Close modal on Escape
document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') {
    var overlay = document.getElementById('submit-modal-overlay');
    if (overlay && overlay.classList.contains('is-open')) {
      closeSubmitModal();
    }
  }
});

// Clear input error state on input
document.addEventListener('DOMContentLoaded', function() {
  var urlInput = document.getElementById('submit-url-input');
  if (urlInput) {
    urlInput.addEventListener('input', function() {
      this.classList.remove('has-error');
      var errorEl = document.getElementById('submit-modal-error');
      if (errorEl) errorEl.textContent = '';
    });
  }
});
```

- [ ] **Step 4: 提交**

```bash
git add web/templates/pages/hot_news.html
git commit -m "feat: add action-bar with search input and submit URL modal to hot_news page"
```

---

### Task 5: 前端 — app.css 新增样式

**Files:**
- Modify: `web/static/css/app.css`（末尾追加）

**Interfaces:**
- Consumes: 已有 CSS custom properties（`--accent`, `--bg`, `--surface`, `--border`, `--radius-*`, `--shadow-*`, `--ease-*`, `--duration-*` 等）

- [ ] **Step 1: 在 app.css 末尾追加 action-bar 样式**

```css
/* ═══════════════════════════════════════════
   ACTION BAR — search + submit (v9)
   ═══════════════════════════════════════════ */
.action-bar {
  display: flex; align-items: center; gap: 12px;
  background: var(--surface);
  border: 1px solid var(--border-light);
  border-radius: var(--radius-xl);
  padding: 14px 20px;
  margin-bottom: 20px;
  box-shadow: var(--shadow-sm);
}

/* ── Search ── */
.search-wrap {
  flex: 1; position: relative;
  display: flex; align-items: center;
}
.search-icon {
  position: absolute; left: 14px; top: 50%; transform: translateY(-50%);
  display: flex; color: var(--text-muted); z-index: 2;
  pointer-events: none;
  transition: color var(--duration-fast);
}
.search-icon svg { width: 17px; height: 17px; }
.search-input {
  width: 100%; height: 42px;
  padding: 0 38px 0 42px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg);
  font-family: 'DM Sans', -apple-system, sans-serif;
  font-size: 14px; color: var(--text);
  outline: none;
  transition: all var(--duration-normal);
}
.search-input::placeholder { color: var(--text-muted); font-size: 13px; }
.search-input:focus {
  border-color: var(--accent);
  background: var(--surface);
  box-shadow: 0 0 0 3px var(--accent-glow);
}
.search-wrap:focus-within .search-icon { color: var(--accent); }

.search-clear {
  position: absolute; right: 10px; top: 50%; transform: translateY(-50%);
  width: 22px; height: 22px; border-radius: 50%;
  border: none; background: transparent;
  color: var(--text-muted); cursor: pointer;
  display: flex; align-items: center; justify-content: center;
  padding: 0; z-index: 2;
  transition: all var(--duration-fast);
}
.search-clear:hover { background: var(--border-light); color: var(--text); }
.search-clear svg { width: 14px; height: 14px; }

.search-spinner {
  position: absolute; right: 12px; top: 50%; transform: translateY(-50%);
  width: 18px; height: 18px; border-radius: 50%;
  border: 2px solid var(--border);
  border-top-color: var(--accent);
  animation: searchSpin 0.6s linear infinite;
  display: none; z-index: 2;
}
@keyframes searchSpin { to { transform: translateY(-50%) rotate(360deg); } }

/* ── Submit button ── */
.submit-btn {
  display: inline-flex; align-items: center; gap: 7px;
  height: 42px; padding: 0 18px;
  border: 1px solid var(--border); border-radius: var(--radius);
  background: var(--surface); cursor: pointer;
  font-family: 'DM Sans', -apple-system, sans-serif;
  font-size: 13px; font-weight: 600; color: var(--text-secondary);
  white-space: nowrap; flex-shrink: 0;
  transition: all var(--duration-fast);
  box-shadow: var(--shadow-xs);
}
.submit-btn:hover {
  border-color: var(--accent); color: var(--accent);
  background: var(--accent-light);
  box-shadow: var(--shadow-sm);
}
.submit-btn:active { transform: scale(0.97); }
.submit-btn-plus {
  display: inline-flex; align-items: center; justify-content: center;
  width: 20px; height: 20px; border-radius: 50%;
  background: var(--accent); color: #fff;
  font-size: 14px; font-weight: 700; line-height: 1;
  transition: transform var(--duration-fast);
}
.submit-btn:hover .submit-btn-plus { transform: rotate(90deg); }
.submit-btn-label { font-size: 13px; }
```

- [ ] **Step 2: 在 app.css 末尾追加 Modal 样式**

```css
/* ═══════════════════════════════════════════
   SUBMIT MODAL — URL fetch dialog (v9)
   ═══════════════════════════════════════════ */
.modal-overlay {
  position: fixed; inset: 0;
  background: rgba(26, 24, 22, 0.35);
  backdrop-filter: blur(4px);
  -webkit-backdrop-filter: blur(4px);
  z-index: 1000;
  display: flex; align-items: center; justify-content: center;
  opacity: 0; pointer-events: none;
  transition: opacity var(--duration-normal);
}
.modal-overlay.is-open { opacity: 1; pointer-events: auto; }

.submit-modal {
  background: var(--surface);
  border-radius: var(--radius-xl);
  box-shadow: var(--shadow-xl);
  width: 500px; max-width: calc(100vw - 48px);
  padding: 0;
  overflow: hidden;
  transform: translateY(12px) scale(0.97);
  transition: transform var(--duration-normal) var(--ease-out-expo);
}
.modal-overlay.is-open .submit-modal { transform: translateY(0) scale(1); }

/* ── Modal header ── */
.submit-modal-header {
  display: flex; align-items: center; gap: 12px;
  padding: 20px 24px 0;
}
.submit-modal-back {
  display: flex; align-items: center; justify-content: center;
  width: 32px; height: 32px; border-radius: var(--radius-sm);
  border: 1px solid var(--border); background: var(--surface);
  cursor: pointer; color: var(--text-secondary);
  transition: all var(--duration-fast);
  flex-shrink: 0;
}
.submit-modal-back:hover {
  border-color: var(--accent); color: var(--accent);
  background: var(--accent-light);
}
.submit-modal-back svg { width: 16px; height: 16px; }
.submit-modal-title {
  font-family: 'Newsreader', serif; font-size: 20px; font-weight: 600;
  color: var(--text); letter-spacing: -0.2px;
}

/* ── Modal body ── */
.submit-modal-body { padding: 20px 24px; }
.submit-modal-label {
  display: block; font-size: 12px; font-weight: 600;
  color: var(--text-secondary); margin-bottom: 8px;
  letter-spacing: 0.2px;
}
.submit-modal-input {
  width: 100%; height: 46px;
  padding: 0 16px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg);
  font-family: 'JetBrains Mono', 'DM Sans', monospace;
  font-size: 13px; color: var(--text);
  outline: none;
  transition: all var(--duration-normal);
}
.submit-modal-input::placeholder {
  color: var(--text-muted); font-size: 12px;
  font-family: 'DM Sans', -apple-system, sans-serif;
}
.submit-modal-input:focus {
  border-color: var(--accent);
  background: var(--surface);
  box-shadow: 0 0 0 3px var(--accent-glow);
}
.submit-modal-input.has-error {
  border-color: var(--red);
  box-shadow: 0 0 0 3px var(--red-light);
}
.submit-modal-hint {
  margin-top: 10px; font-size: 12px; color: var(--text-muted);
  line-height: 1.6;
}
.submit-modal-error {
  margin-top: 8px; font-size: 12px; color: var(--red);
  min-height: 18px;
}

/* ── Modal footer ── */
.submit-modal-footer {
  display: flex; justify-content: flex-end; gap: 10px;
  padding: 0 24px 20px;
}
.submit-modal-cancel {
  height: 38px; padding: 0 18px;
  border: 1px solid var(--border); border-radius: var(--radius);
  background: var(--surface); cursor: pointer;
  font-family: 'DM Sans', -apple-system, sans-serif;
  font-size: 13px; font-weight: 500; color: var(--text-secondary);
  transition: all var(--duration-fast);
}
.submit-modal-cancel:hover { border-color: var(--text-muted); color: var(--text); }
.submit-modal-confirm {
  height: 38px; padding: 0 22px;
  border: none; border-radius: var(--radius);
  background: var(--accent); cursor: pointer;
  font-family: 'DM Sans', -apple-system, sans-serif;
  font-size: 13px; font-weight: 600; color: #fff;
  transition: all var(--duration-fast);
  box-shadow: 0 2px 8px rgba(242, 95, 15, 0.25);
}
.submit-modal-confirm:hover {
  background: #e0550a;
  box-shadow: 0 4px 14px rgba(242, 95, 15, 0.35);
}
.submit-modal-confirm:active { transform: scale(0.97); }
.submit-modal-confirm:disabled {
  opacity: 0.5; cursor: not-allowed;
}
.submit-modal-confirm:disabled:hover {
  background: var(--accent);
  box-shadow: 0 2px 8px rgba(242, 95, 15, 0.25);
}

/* ── Responsive: action-bar stacks on narrow screens ── */
@media (max-width: 640px) {
  .action-bar {
    flex-direction: column; gap: 10px;
    padding: 12px 16px;
  }
  .submit-btn { width: 100%; justify-content: center; }
  .submit-btn-label { display: inline; }
}
```

- [ ] **Step 3: 提交**

```bash
git add web/static/css/app.css
git commit -m "feat: add action-bar, search input, and submit modal styles"
```

---

### Task 6: 验证 — 启动应用进行端到端验证

**Files:**
- 无新建/修改

- [ ] **Step 1: 启动应用**

```bash
cd /home/llianc62/ws/NewsRadar && python main.py &
sleep 3
```

- [ ] **Step 2: 验证搜索功能**

打开浏览器访问 `http://localhost:8000/hot-news`，在搜索框中输入关键词（如"中美"），等待 300ms 后确认：
- 页面自动刷新
- URL 包含 `?search=中美&page=1`
- 搜索结果仅显示标题或摘要包含"中美"的新闻
- control-deck 中的统计数字对应该搜索范围
- 清除按钮（×）可点击，点击后清空搜索词，页面刷新

- [ ] **Step 3: 验证提交新闻功能**

点击"提交新闻"按钮，确认：
- Modal 弹出，带模糊背景
- 输入 URL 后点击"开始抓取"触发 API 调用
- 空输入点击提交 → 显示红色错误提示
- 非 HTTP URL → 显示错误提示
- 有效 URL 提交后 → Modal 关闭，通知系统收到抓取任务
- Esc 键关闭 Modal

- [ ] **Step 4: 验证 action-bar 响应式**

缩小浏览器宽度到 640px 以下，确认 action-bar 从水平布局变为垂直堆叠。

- [ ] **Step 5: 运行完整测试套件**

```bash
cd /home/llianc62/ws/NewsRadar && uv run pytest tests/ -v 2>&1 | tail -40
```

预期：所有已有测试 PASS。

- [ ] **Step 6: 删除设计预览文件（非生产代码）**

```bash
rm /home/llianc62/ws/NewsRadar/web/static/design-preview.html
```

- [ ] **Step 7: 最终提交**

```bash
git add -A
git commit -m "chore: remove design preview file, final verification passed"
```

---

## Self-Review 检查清单

- [x] Spec coverage: 搜索 → Task 1+2+4+5, 手动抓取 → Task 3+4+5, 前端UI → Task 4+5
- [x] 无占位符：所有步骤包含确切代码
- [x] 类型一致性：`search: Optional[str] = None` 在所有 6 个 DB 方法中一致；`current_search` 模板变量名前后一致；`POST /api/news/fetch` 请求/响应格式一致
- [x] 向后兼容：所有新增参数有默认值 `None`，已有调用不受影响
