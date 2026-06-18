# 通知栏优化设计

**日期**: 2026-06-18
**状态**: 已确认

## 背景

当前通知系统基于内存存储 (`_notifications` 列表，上限 50 条)，用于跟踪文章抓取/重抓取任务的状态。存在两个问题：

1. 点击通知消息跳转文章详情页时，未将通知标记为已读
2. 通知抽屉只展示未读消息，用户看不到历史通知

## 需求

1. 点击通知项跳转时，自动标记该通知为已读（fire-and-forget）
2. 打开通知抽屉时展示全部消息（非仅未读），已读/未读通过视觉样式区分

## 设计决策

- **方案选择**：纯前端改动，后端 API 不变
- **混合展示**：已读和未读消息在同一列表中，按时间倒序排列，仅通过样式区分
- **标记时机**：点击即标记已读，然后跳转

## 改动范围

### 前端 JS（web/templates/base.html）

**1. 抽屉获取全部通知**

```javascript
// 改前: fetch('/api/notifications?unread_only=true')
// 改后:
fetch('/api/notifications')
```

**2. 点击通知项先标记已读再跳转**

```javascript
// 改前: onclick="window.location.href='/news/...'"
// 改后:
function markAndGo(notifId, articleId) {
  fetch('/api/notifications/' + notifId + '/read', { method: 'POST' });
  window.location.href = '/news/' + articleId;
}
```

**3. 渲染时已读项加 `.is-read` class**

```javascript
var readClass = n.is_read ? ' is-read' : '';
html += '<div class="notify-item' + readClass + '" data-id="...">';
```

**4. `closeDrawer()` 保持现有逻辑**：遍历所有通知项逐条标记已读，角标归零。

### 前端 CSS（web/static/css/app.css）

新增一条规则：

```css
.notify-item.is-read {
  opacity: 0.45;
}
.notify-item.is-read:hover {
  opacity: 0.7;
}
```

另外为通知项加边框区分（现有 `.notify-item` 已有 `border-radius`，补充 `border` 和 `margin-bottom`）。

### 模板（web/templates/pages/hot_news.html）

- 状态文案改为"抓取成功 / 抓取失败 / 抓取中"
- 空状态文案改为"暂无消息"

### 后端

无改动。

## 数据流

```
抽屉打开 → GET /api/notifications（全部）→ 已读半透明，未读全亮
点击单项 → POST .../read（fire-and-forget）→ 跳转 /news/{id}
关闭抽屉 → 逐条 POST .../read + badge 归零（保持现有逻辑）
轮询     → 更新 badge 数字
```

## 测试要点

- 点击未读通知跳转后，该通知变为半透明（已读样式）
- 关闭抽屉后所有通知变为已读，badge 归零
- 有新抓取任务时，新通知以全亮状态出现在列表顶部
- 服务重启后通知列表清空，前端正确处理空列表状态
