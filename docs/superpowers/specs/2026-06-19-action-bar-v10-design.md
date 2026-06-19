# Action Bar v10 — 功能按钮组设计

## 概述

在热点新闻页面 (`/hot-news`) 的 action bar 右侧添加两个功能按钮（手动抓取、云端同步），同时将现有"提交新闻"按钮简化为图标按钮。

## 当前状态

Action bar 位于 `web/templates/pages/hot_news.html:117-143`，包含：
- 搜索框（左侧，flex: 1）
- "提交新闻"按钮（右侧，含 `+` 图标 + "提交新闻" 文字标签）

## 目标设计

三个等大正方形按钮（40×40px）组成按钮组，通过竖线分隔符与搜索框隔开：

```
[搜索框 ────────────] │ [+] [🐛] [☁]
                       加号  虫子  云朵
```

### 按钮规格

| 按钮 | CSS class | 功能 | API | Toast |
|------|-----------|------|-----|-------|
| 加号 `+` | `act-btn--plus` | 提交新闻链接抓取 | 打开已有 submit modal | — |
| 虫子 🐛 | `act-btn--crawl` | 手动触发新闻爬取 | `POST /api/trigger/crawl` | "抓取任务已触发" |
| 云朵 ☁ | `act-btn--sync` | 手动触发云端同步 | `POST /api/trigger/sync` | "同步任务已触发" |

### 样式规范

- 基础尺寸：40×40px 正方形，`border-radius: var(--radius)` (10px)
- 边框：`1px solid var(--border)`，hover 时变 `var(--accent)`
- 背景：`var(--surface)`，hover 时变 `var(--accent-light)`
- 加号按钮内部：22×22px 圆形 accent 背景 + 白色 `+` 字，hover 旋转 90°
- 虫子按钮 hover：图标 wiggle 动画（±8° 旋转）
- 云朵按钮 hover：图标 bounce 动画（上下浮动）
- Active 状态：`transform: scale(0.93)`
- Tooltip：hover 时在按钮上方显示（纯 CSS `::after` 实现）

### 分隔符

- 1px 宽 × 28px 高的竖线，颜色 `var(--border-light)`
- 位于搜索框和按钮组之间

### 响应式 (≤640px)

- Action bar 变为纵向排列
- 按钮组居中分布在搜索框下方

## 涉及文件

| 文件 | 变更 |
|------|------|
| `web/templates/pages/hot_news.html` | 替换 action bar HTML + 添加 JS 事件处理 |
| `web/static/css/app.css` | 新增 `.act-btn`、`.action-divider`、`.action-btn-group` 样式，替换 `.submit-btn` 样式 |

## 交互流程

1. 用户点击虫子/云朵按钮
2. 前端 `fetch()` 调用 `POST /api/trigger/crawl` 或 `/api/trigger/sync`
3. 后端 `signal.set()` 触发异步任务
4. 前端收到 `{ok: true}` 后显示 toast（复用 `showAppToast`）
5. 任务在后台执行，完成后通过通知铃铛查看结果

## 向后兼容

- Submit modal 功能不变，仅触发方式从文字按钮变为图标按钮
- 搜索框功能完全不变
- 通知系统（toast + drawer + polling）完全复用
