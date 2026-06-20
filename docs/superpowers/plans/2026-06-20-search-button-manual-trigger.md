# 搜索交互改造：移除 5s 自动搜索，添加手动搜索按钮

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 移除搜索框 5 秒 setTimeout 自动搜索，在搜索框旁新增 accent-color 搜索按钮，点击/Enter 手动触发搜索。

**Architecture:** 纯前端改动 — 修改 Jinja2 模板中的 HTML + 内联 JS，新增 CSS 样式。后端（`web/app.py` `/hot-news` 路由）无需变更，搜索仍通过 `?search=` URL 参数驱动。

**Tech Stack:** Jinja2 模板, vanilla JS, CSS custom properties

**Design Preview:** `web/static/preview/search-button-preview.html`（已确认通过）

## Global Constraints

- 保持现有 URL 驱动搜索模式（`?search=关键词` query param）
- 保留 Enter / Escape 键盘快捷键
- 保留 clearSearch() 清空行为
- 搜索按钮样式使用 `--accent` (#f25f0f) 作为底色，匹配现有设计系统
- CSS 变量沿用 `web/static/css/app.css` 顶层 `:root` tokens

---

### Task 1: 新增搜索按钮 CSS 样式

**Files:**
- Modify: `web/static/css/app.css` — 在 `.search-spinner` 块之后插入新样式

**Interfaces:**
- Produces: `.search-btn-wrap`, `.search-btn` 及其 `:hover`/`:active`/`:focus-visible` 状态样式

- [ ] **Step 1: 在 `.search-spinner` / `@keyframes searchSpin` 之后插入搜索按钮样式**

打开 `web/static/css/app.css`，找到第 1153 行 `@keyframes searchSpin { to { transform: translateY(-50%) rotate(360deg); } }`，在其后插入：

```css
/* ── Search button ── */
.search-btn-wrap {
  flex-shrink: 0;
}
.search-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 7px;
  height: 42px;
  padding: 0 18px;
  border: none;
  border-radius: var(--radius);
  background: var(--accent);
  color: #fff;
  font-family: 'DM Sans', -apple-system, sans-serif;
  font-size: 14px;
  font-weight: 600;
  cursor: pointer;
  white-space: nowrap;
  transition: all var(--duration-normal) var(--ease-out-expo);
  box-shadow: 0 1px 3px rgba(242, 95, 15, 0.25);
  letter-spacing: 0.02em;
}
.search-btn:hover {
  background: #e55a0e;
  box-shadow: 0 4px 14px rgba(242, 95, 15, 0.32);
  transform: translateY(-1px);
}
.search-btn:active {
  transform: scale(0.96);
  box-shadow: 0 1px 2px rgba(242, 95, 15, 0.2);
  transition: all 80ms var(--ease-out-expo);
}
.search-btn:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 3px;
}
.search-btn svg {
  width: 16px;
  height: 16px;
  flex-shrink: 0;
}
/* Loading state */
.search-btn.is-loading {
  pointer-events: none;
  opacity: 0.85;
}
.search-btn.is-loading .btn-text {
  opacity: 0;
}
.search-btn.is-loading .btn-spinner {
  display: block;
}
.btn-spinner {
  display: none;
  position: absolute;
  width: 18px;
  height: 18px;
  border-radius: 50%;
  border: 2px solid rgba(255, 255, 255, 0.4);
  border-top-color: #fff;
  animation: btnSpin 0.6s linear infinite;
}
@keyframes btnSpin {
  to { transform: rotate(360deg); }
}
```

> **注意：** `.search-spinner` 和 `@keyframes searchSpin` 保留不动（其他地方可能引用），仅新增搜索按钮样式。

- [ ] **Step 2: 验证 CSS 无语法错误**

```bash
python -c "import cssutils; cssutils.parseFile('web/static/css/app.css'); print('CSS OK')" 2>/dev/null || echo "cssutils not available — visually verify CSS block is well-formed"
```

- [ ] **Step 3: 提交**

```bash
git add web/static/css/app.css
git commit -m "feat: add search button CSS styles for manual search trigger"
```

---

### Task 2: 修改 hot_news.html — HTML 结构

**Files:**
- Modify: `web/templates/pages/hot_news.html:116-137` — action-bar 中的 search-wrap 区域

**Interfaces:**
- Produces: 搜索按钮 HTML 元素 `<button class="search-btn" id="search-btn">`

- [ ] **Step 1: 定位现有 HTML 结构**

打开 `web/templates/pages/hot_news.html`，找到第 116-137 行 action-bar 内的 search-wrap：

当前代码：
```html
    <!-- Action Bar — search + action buttons -->
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
        <button class="search-clear" id="search-clear" title="清除搜索"
                {% if not current_search %}style="display:none"{% endif %}
                onclick="clearSearch()">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>
        </button>
        <div class="search-spinner" id="search-spinner"></div>
      </div>

      <div class="action-divider"></div>
      ...
```

- [ ] **Step 2: 删除 search-spinner，新增搜索按钮**

将上述 HTML 块替换为：

```html
    <!-- Action Bar — search + action buttons -->
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
        <button class="search-clear" id="search-clear" title="清除搜索"
                {% if not current_search %}style="display:none"{% endif %}
                onclick="clearSearch()">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>
        </button>
      </div>

      <!-- NEW: 手动搜索按钮 -->
      <div class="search-btn-wrap">
        <button class="search-btn" id="search-btn" onclick="triggerSearch()" title="搜索" aria-label="执行搜索">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
            <circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/>
          </svg>
          <span class="btn-text">搜索</span>
        </button>
      </div>

      <div class="action-divider"></div>
      ...
```

变更要点：
- 删除 `<div class="search-spinner" id="search-spinner"></div>`
- 在 `</div><!-- /search-wrap -->` 和 `<div class="action-divider">` 之间插入搜索按钮 HTML

- [ ] **Step 3: 提交**

```bash
git add web/templates/pages/hot_news.html
git commit -m "feat: add search button HTML, remove spinner element"
```

---

### Task 3: 修改 hot_news.html — JavaScript 逻辑

**Files:**
- Modify: `web/templates/pages/hot_news.html:438-515` — `{% block scripts %}` 中的搜索 IIFE

**Interfaces:**
- Consumes: `#search-input`, `#search-clear`, `#search-btn` DOM 元素
- Produces: `triggerSearch()` 全局函数, 重写输入事件处理器

- [ ] **Step 1: 替换整个搜索 IIFE**

将第 438-515 行的搜索 IIFE（`(function() { ... })();`）替换为：

```javascript
// ── Search with manual button trigger ──
(function() {
  var input = document.getElementById('search-input');
  var clear = document.getElementById('search-clear');
  var searchBtn = document.getElementById('search-btn');

  function doSearch(value) {
    var params = new URLSearchParams(window.location.search);
    var current = params.get('search') || '';
    if (value === current) return;

    if (value) {
      params.set('search', value);
    } else {
      params.delete('search');
    }
    params.set('page', '1');
    window.location.search = params.toString();
  }

  // 手动触发搜索
  window.triggerSearch = function() {
    var val = input.value;
    if (val) {
      doSearch(val);
    }
  };

  // 输入时仅控制清除按钮显隐，不自动搜索
  input.addEventListener('input', function() {
    var val = this.value;
    if (clear) clear.style.display = val.length > 0 ? 'flex' : 'none';
  });

  // 清空搜索
  window.clearSearch = function() {
    input.value = '';
    if (clear) clear.style.display = 'none';
    doSearch('');
  };

  // 键盘快捷键
  input.addEventListener('keydown', function(e) {
    if (e.key === 'Enter') {
      e.preventDefault();
      var val = this.value;
      doSearch(val);
    }
    if (e.key === 'Escape' && this.value) {
      e.preventDefault();
      clearSearch();
    }
  });
})();
```

变更要点：
- 删除 `timer`、`lastValue`、`spinner` 变量
- 删除 `scheduleSearch()` 函数（含 `setTimeout(doSearch, 5000)`）
- 删除输入事件中的 `scheduleSearch()` 调用和 spinner 显隐逻辑
- 新增 `window.triggerSearch()` 全局函数，供搜索按钮 onclick 调用
- `doSearch()` 保持不变
- `clearSearch()` 移除 spinner 引用
- Enter/Escape 处理简化（移除 timer、lastValue、spinner 操作）

- [ ] **Step 2: 验证 HTML 模板可正常渲染**

```bash
python -c "
from web.app import create_app
from unittest.mock import MagicMock

# 验证模板编译不报错
from jinja2 import Environment, FileSystemLoader
import os

tpl_dir = os.path.join(os.path.dirname(__file__), 'web', 'templates')
env = Environment(loader=FileSystemLoader(tpl_dir))
tmpl = env.get_template('pages/hot_news.html')
print('Template compiles OK')
" 2>&1 | tail -1
```

- [ ] **Step 3: 提交**

```bash
git add web/templates/pages/hot_news.html
git commit -m "feat: replace 5s auto-search with manual search button trigger"
```

---

### Task 4: 端到端验证

**Files:**
- 无新建文件 — 功能验证

**Interfaces:**
- 验证搜索按钮点击触发页面导航，Enter 键搜索、Escape 清空、清空按钮均正常

- [ ] **Step 1: 启动开发服务器**

```bash
python main.py &
sleep 3
echo "Server should be running"
```

- [ ] **Step 2: 使用浏览器验证搜索交互**

用 Playwright 或浏览器手动验证以下场景：

| 场景 | 操作 | 预期行为 |
|------|------|----------|
| 输入关键词 + 点击搜索按钮 | 输入 "AI"，点击搜索按钮 | 页面导航至 `?search=AI&page=1` |
| 输入关键词 + 按 Enter | 输入 "AI"，按 Enter | 同上 |
| 清空输入 + 按 Enter | 清空后按 Enter | 导航至无 search 参数页面 |
| 点击清除按钮 | 点击 × | 立即清空并导航 |
| Escape 清空 | 输入文本，按 Esc | 输入框清空，导航至无 search 参数 |
| 空输入点击搜索 | 不输入直接点搜索 | 无导航发生（`if (val)` guard） |
| 搜索按钮 hover | 鼠标悬停 | 背景变深、浮起 1px |
| 搜索按钮 active | 鼠标按下 | scale(0.96) |
| 搜索按钮 focus-visible | Tab 聚焦 | 2px outline ring |

- [ ] **Step 3: 确认无回归**

验证以下现有功能不受影响：
- Tier 筛选（点击 tier 标签正常切换）
- Sentiment 筛选（利好/利空/中性切换）
- Keyword 筛选（关键词标签点击）
- 日期筛选（日期弹窗预设 + 自定义范围）
- 分页导航
- 每页条数下拉

---

## 变更影响范围

```
web/templates/pages/hot_news.html   ← HTML: 删 search-spinner, 加 search-btn
                                    ← JS:   删 setTimeout 定时器, 加 triggerSearch()
web/static/css/app.css              ← CSS:  新增 .search-btn / .search-btn-wrap 样式
```

**不受影响的文件：**
- `web/app.py` — 后端路由 `/hot-news` 无需变更，`search` 参数处理逻辑不变
- `web/templates/base.html` — 共享 JS 不涉及搜索逻辑
- 其他页面（market_overview, news_detail）— 无搜索框
