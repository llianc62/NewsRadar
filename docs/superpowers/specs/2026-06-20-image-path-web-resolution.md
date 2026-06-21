# 图片路径渲染层解析 — 设计文档

> **状态:** 草稿  
> **日期:** 2026-06-20  
> **目标:** 将图片路径的 `/media/` 前缀拼接从持久化层移到 Web 渲染层

## 问题

### 当前架构

```
ImageProcessor._download_and_save()
  → S3 key:  news/2026-06-20/images/abc.jpg
  → 返回值:  images/abc.jpg           ← 相对路径，不含日期

_run_batch_image_download()
  → content 中 URL 被替换为 images/abc.jpg

_persist_postgresql()                 ← 唯一做路径转换的地方
  → images/ → /media/news/2026-06-20/images/
  → 存入 PostgreSQL 的 content 字段
```

### 两个缺陷

1. **`_run_refetch` 绕过 persist** — refetch 调用 `enrich_content` + `update_article_content` 直接写库，跳过了 `_persist_postgresql` 的路径转换。refetch 后的文章图片路径是裸的 `images/abc.jpg`，浏览器按相对路径解析会 404。

2. **职责错位** — `_persist_postgresql` 是持久化层，不应关心 Web 路由前缀。`/media/` 是 FastAPI 的路由细节，归属 Web 层。

## 方案

**Content 保持存 `images/abc.jpg`（S3 key 去掉日期前缀后的路径）。渲染时由 Web 层用 `updated_at` 拼出完整 `/media/` URL。**

```
持久化层（crawler）               Web 渲染层
─────────────────                 ────────────
content: images/abc.jpg  ───►  读取 updated_at → 2026-06-20
                               替换 images/ → /media/news/2026-06-20/images/
                               输出到 HTML
```

### 数据流

```
┌─ 下载图片 ──────────────────────────────────────────┐
│  S3 key:  news/2026-06-20/images/abc.jpg            │
│  content: images/abc.jpg  (稳定，不随路由变)          │
└──────────────────────┬──────────────────────────────┘
                       │
                       ▼
┌─ 渲染 HTML ─────────────────────────────────────────┐
│  article.updated_at → "2026-06-20 14:30:00+08"      │
│  提取日期 → "2026-06-20"                             │
│  images/xxx → /media/news/2026-06-20/images/xxx     │
│  经过 markdown 渲染 → <img src="/media/...">         │
└─────────────────────────────────────────────────────┘
```

## 变更文件

| 文件 | 操作 | 说明 |
|------|------|------|
| `news/crawler.py:632-649` | 修改 | `_persist_postgresql` 删除 images→/media 转换逻辑 |
| `web/app.py` | 修改 | `news_detail` 路由渲染前做路径替换 |

## 详细设计

### 1. `_persist_postgresql` — 删除路径转换

```python
# 改前
def _persist_postgresql(self, data: NewsData) -> None:
    """Save to PostgreSQL.
    Transforms relative image paths … """
    # Transform image paths for web/S3 resolution
    date_str = format_date_folder()
    media_prefix = f"/media/news/{date_str}/images/"
    for items in data.items.values():
        for item in items:
            if item.content:
                item.content = item.content.replace("images/", media_prefix)
    db = self._get_pg_db()
    …

# 改后
def _persist_postgresql(self, data: NewsData) -> None:
    """Save to PostgreSQL."""
    db = self._get_pg_db()
    …
```

### 2. Web 渲染层 — `resolve_image_paths`

在 `news_detail` 路由中，渲染前处理 content：

```python
import re
from datetime import date

def _resolve_image_paths(content: str, updated_at) -> str:
    """将 content 中的 images/xxx 替换为 /media/news/YYYY-MM-DD/images/xxx"""
    if not content or 'images/' not in content:
        return content
    if updated_at:
        date_str = updated_at.strftime('%Y-%m-%d') if hasattr(updated_at, 'strftime') else str(updated_at)[:10]
    else:
        date_str = date.today().isoformat()
    media_prefix = f"/media/news/{date_str}/images/"
    return content.replace("images/", media_prefix)


@app.get("/news/{article_id}", response_class=HTMLResponse)
async def news_detail(request: Request, article_id: int):
    article = db.get_news_by_id(article_id)
    if article is None:
        …
    if article.get("content"):
        article["content"] = re.sub(r"^# .+?\n\n?", "", article["content"], count=1)
        article["content"] = _resolve_image_paths(
            article["content"],
            article.get("updated_at"),
        )
    …
```

这样 `_run_refetch` 和正常 `fetch_all` 两条路径产出的 content 都是 `images/abc.jpg`，渲染时统一处理。

## 不变

- S3 存储路径不变：`news/YYYY-MM-DD/images/xxx`
- `ImageProcessor._download_and_save` 返回值不变：`images/xxx`
- `_run_batch_image_download` 的 URL 替换逻辑不变
- `/media/{path}` 代理路由不变
- `_run_refetch` 无需改动（路径已在渲染时处理）

## 影响

- 旧数据（content 中已是 `/media/news/...` 格式）需要重建。用户已确认会清除全部旧数据。
