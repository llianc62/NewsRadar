# Failure Recording & Lazy Retry Mechanism

**Date:** 2026-06-21
**Status:** Draft

## Overview

当前 Crawler 在抓取新闻时，网页 HTTP 请求失败和图片下载失败仅输出到 console，无持久化记录、无重试。本设计引入：

1. **即时重试**（immediate retry）：单次调用内最多重试 N 次，处理瞬时网络抖动
2. **惰性重试**（lazy retry）：跨 crawl 周期的失败重试，失败任务持久化到 PostgreSQL `failed_tasks` 表，后续 crawl 自动附带 pending 任务

## Scope

| 维度 | 范围 |
|------|------|
| 失败类型 | `content_fetch`（网页 HTTP 下载失败）、`image_download`（图片下载/存储失败） |
| 正文提取失败 | **不纳入** — 提取失败重试无意义 |
| 存储后端 | 仅 PostgreSQL（与现有架构一致） |
| 即时重试上限 | 3 次/单次调用（可配置） |
| 惰性重试上限 | 3 个 crawl 周期（可配置） |

## Architecture

```
fetch_all()
│
├── 1. Hot-list + RSS 拉取 → all_items
│
├── 2. _retry_content_fetch_failures()  ← 查询 pending content_fetch 任务
│       └── 重建 item dict → 合并到 all_items
│
├── 3. enrich_content(*all_items, with_image)
│       ├── Phase 1: _download_and_parse       ← 即时重试 3 次，失败记录到 failed_tasks
│       └── Phase 2: _run_batch_image_download ← 即时重试 3 次，失败记录到 failed_tasks
│
├── 4. persist(*all_items)  → PostgreSQL
│
└── 5. _retry_image_download_failures()  ← 查询 pending image_download 任务
        └── 成功 → 替换文章 content 中的图片 URL → mark completed
            └── 失败 → retry_times++ / mark failed
```

### Key Design Decisions

**为什么 image_download retry 放在 persist 之后？**

- 图片下载失败时文章可能尚未持久化（首次 crawl），没有 article_id
- 惰性重试时文章已存在于 DB，可通过 content 中的旧图片 URL 反查文章并更新
- 统一的重试时机简化了代码：所有 image_download retry 都在文章已持久化的前提下进行

**为什么 `failed_tasks` 使用通用 JSONB context？**

- `task_type` 是自由文本，不限于新闻板块
- 所有业务数据（url、article_id、source_id 等）都在 context JSONB 中
- 固定字段仅维护重试状态机（retry_times、max_retry、status）

---

## 1. Database Schema

### 1.1 failed_tasks 表

```sql
CREATE TABLE IF NOT EXISTS failed_tasks (
    id              BIGSERIAL PRIMARY KEY,
    task_type       VARCHAR(50) NOT NULL,
    context         JSONB NOT NULL DEFAULT '{}',
    retry_times     INTEGER NOT NULL DEFAULT 0,
    max_retry       INTEGER NOT NULL DEFAULT 3,
    last_retry      TIMESTAMPTZ DEFAULT NULL,
    status          VARCHAR(20) NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'failed', 'completed')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 同类型同 URL 的 pending 任务去重，避免重复插入
CREATE UNIQUE INDEX IF NOT EXISTS idx_failed_tasks_dedup
    ON failed_tasks (task_type, (context->>'url'))
    WHERE status = 'pending';

-- 查询索引
CREATE INDEX IF NOT EXISTS idx_failed_tasks_status
    ON failed_tasks (status, task_type, retry_times);
```

### 1.2 context 结构

**content_fetch:**
```json
{
    "url": "https://example.com/article/123",
    "source_id": "thepaper",
    "source_type": "hotlist",
    "source_name": "澎湃新闻",
    "title": "文章标题",
    "rank": 1,
    "guid": "",
    "mobile_url": "",
    "published_at": "2026-06-21T10:00:00+08:00"
}
```

**image_download:**
```json
{
    "url": "https://example.com/images/photo.jpg",
    "target_dir": "news/2026-06-21/images"
}
```

### 1.3 Migration

通过 `PostgreSQL._run_migrations()` 执行 DDL，幂等（`IF NOT EXISTS`）。

---

## 2. Configuration

### 2.1 config.yaml

```yaml
crawler:
  max_immediate_retries: 3    # 单次调用的即时重试次数
  max_retry: 3                # 跨周期惰性重试最大次数（failed_tasks 默认值）
```

### 2.2 Environment Variables

| Env | Default | Description |
|-----|---------|-------------|
| `CRAWLER_MAX_IMMEDIATE_RETRIES` | `3` | 单次 HTTP/图片下载的即时重试次数 |
| `CRAWLER_MAX_RETRY` | `3` | 惰性重试最大周期数 |

### 2.3 Crawler 初始化

```python
cfg = config.get("crawler", {})
self.max_immediate_retries = cfg.get("max_immediate_retries", 3)
self.max_retry = cfg.get("max_retry", 3)
```

---

## 3. Immediate Retry（即时重试）

### 3.1 `_download_and_parse` (news/crawler.py)

在现有 `requests.get()` 外层包 for 循环，递增退避（1s, 2s, 4s）。

```
flow:
  for attempt in 1..max_immediate_retries:
      try GET url
        → success: break
        → failure: if last attempt → record failure, return False
                   else sleep(2^attempt) and continue
  parse html
  return True
```

### 3.1b `_run_batch_parse` 去重保护

已有 content 的 item 不应再次下载（例如刚从 `_retry_content_fetch_failures` 成功返回的 item）：

```python
# 只处理有 url 且无 content 的 item
valid = [it for it in items if it.get("url") and not it.get("content")]
```

失败记录只在 `_download_and_parse` 内部完成——调用方无需感知。

### 3.2 `ImageProcessor._download_and_save` (news/images.py)

同理，在 HTTP GET 和存储操作外层各包 for 循环。

注意：存储失败（`storage.save` 异常）也需要重试——可能是 MinIO 临时不可用。

```
flow:
  for attempt in 1..max_immediate_retries:
      try GET image_url
        → success: break
        → failure: if last attempt → return None
                   else sleep and continue
  
  for attempt in 1..max_immediate_retries:
      try storage.save(data, target_path, content_type)
        → success: return relative_path
        → failure: if last attempt → return None
                   else sleep and continue
```

### 3.3 ImageProcessor 构造函数扩展

```python
def __init__(self, max_workers=8, max_immediate_retries=3):
    self._max_workers = max_workers
    self._max_immediate_retries = max_immediate_retries
```

Crawler 创建 ImageProcessor 时传入配置值。

---

## 4. Failure Recording（失败记录）

### 4.1 PostgreSQL 新增方法

在 `storage/postgres.py` `PostgreSQL` 类中添加：

```python
def record_failure(
    self, task_type: str, context: dict,
    max_retry: int = 3,
) -> Optional[int]:
    """INSERT a failed task (ON CONFLICT DO NOTHING).
       Returns task id, or None if duplicate pending."""

def get_pending_failures(
    self, task_type: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """SELECT pending tasks where retry_times < max_retry."""

def article_has_content(self, url: str) -> bool:
    """Check if any article with this URL has non-empty content.
       Used to skip retry when content was already fetched via normal path."""

def mark_failure_completed(self, task_id: int) -> None:
    """UPDATE status = 'completed'."""

def mark_failure_retried(self, task_id: int, error: str = "") -> None:
    """UPDATE retry_times += 1, last_retry = NOW().
       If retry_times >= max_retry afterward, set status = 'failed'."""

def find_articles_by_image_url(self, image_url: str) -> List[int]:
    """SELECT id FROM news_articles WHERE content LIKE '%<url>%'."""

def update_article_image_url(
    self, article_id: int, old_url: str, new_path: str,
) -> None:
    """UPDATE news_articles SET content = REPLACE(content, old_url, new_path)."""
```

### 4.2 Crawler 新增方法

```python
def _record_content_fetch_failure(
    self, item: Dict[str, Any], error: str,
) -> None:
    """Record a failed content fetch to failed_tasks."""
    context = {
        "url": item["url"],
        "source_id": item.get("source_id", ""),
        "source_type": item.get("source_type", ""),
        "source_name": item.get("source_name", ""),
        "title": item.get("title", ""),
        "rank": item.get("rank", 0),
        "guid": item.get("guid", ""),
        "mobile_url": item.get("mobile_url", ""),
        "published_at": item.get("published_at", ""),
    }
    pg = self._get_pg_db()
    task_id = pg.record_failure("content_fetch", context, self.max_retry)
    if task_id:
        print(f"[Crawler] Recorded content_fetch failure: {item['url']}")
    # dedup: if record_failure returns None, a pending task already exists

def _record_image_download_failures(
    self, url_map: Dict[str, str], results: Dict[str, str],
) -> None:
    """For each image URL where result is '', record a failure."""
    for url, saved_path in results.items():
        if saved_path:
            continue
        target_dir = url_map.get(url, "")
        context = {"url": url, "target_dir": target_dir}
        pg = self._get_pg_db()
        pg.record_failure("image_download", context, self.max_retry)
```

### 4.3 记录时机

| 位置 | 触发条件 | 记录类型 |
|------|---------|---------|
| `_download_and_parse` | 所有即时重试耗尽 | `content_fetch` |
| `_run_batch_image_download` | processor.download 返回后遍历失败 URL | `image_download` |

---

## 5. Lazy Retry（惰性重试）

### 5.1 `_retry_content_fetch_failures` (Crawler)

```
query: SELECT * FROM failed_tasks
       WHERE task_type = 'content_fetch'
         AND status = 'pending'
         AND retry_times < max_retry

for each task:
    url = context["url"]

    # 防止重复下载：检查该 URL 的文章是否已有 content
    # （可能上一次 crawl 正常路径已成功，但失败记录未清理）
    if article_already_has_content(url):
        mark_failure_completed(task_id)
        continue

    reconstruct item dict from context JSONB
    success = _download_and_parse(item)  # includes immediate retries
    if success:
        mark_failure_completed(task_id)
        将 item 加入 all_items（后续 enrich 中 _run_batch_parse 会跳过已有 content 的 item）
    else:
        mark_failure_retried(task_id, error="HTTP failed after retries")
```

`article_already_has_content` 实现：
```sql
SELECT 1 FROM news_articles
WHERE url = %s AND content IS NOT NULL AND content != ''
LIMIT 1
```

### 5.2 `_retry_image_download_failures` (Crawler)

```
query: SELECT * FROM failed_tasks
       WHERE task_type = 'image_download'
         AND status = 'pending'
         AND retry_times < max_retry

for each task:
    url = context["url"]
    target_dir = context["target_dir"]

    result = image_processor.download({url: target_dir}, storage)

    if result[url]:  # success
        mark_failure_completed(task_id)

        # find articles containing this image URL
        article_ids = pg.find_articles_by_image_url(url)
        for article_id in article_ids:
            pg.update_article_image_url(article_id, url, result[url])
    else:
        mark_failure_retried(task_id, error="Image download failed")

    # 如果无任何文章包含此图片 URL（文章已删除），直接标记完成
    # 这通过在 _retry_image_download_failures 中检查 find_articles_by_image_url
    # 返回值决定：download 成功但无匹配文章 → mark completed（无需更新）
```

### 5.3 ImageProcessor 下载重试支持

`_retry_image_download_failures` 需要调用 `ImageProcessor.download()`。该方法接受 `url_map: Dict[str, str]`，可以传入单张图片的 map。

### 5.4 fetch_all 集成

```python
def fetch_all(self, output_style, with_content=False, with_image=False):
    all_items = []

    # Hot-list + RSS (unchanged)
    ...

    # Retry failed content fetches from previous cycles
    # Returns item dicts that succeeded this time
    retried = self._retry_content_fetch_failures()
    all_items.extend(retried)

    # Enrich (includes image download + failure recording)
    if with_content:
        self.enrich_content(*all_items, with_image=with_image)

    # Persist
    self.persist(*all_items, output_style=output_style)

    # Retry previously failed image downloads
    # Must be AFTER persist so articles exist in DB
    if with_image:
        self._retry_image_download_failures()

    return {...}
```

---

## 6. Error Handling & Edge Cases

### 6.1 Dedup

`idx_failed_tasks_dedup` 部分唯一索引确保同一 URL + task_type 只有一个 pending 记录。`record_failure` 使用 `INSERT ... ON CONFLICT DO NOTHING`，返回 None 表示已存在。

### 6.2 重复下载保护

- **`_run_batch_parse`** 跳过已有 content 的 item（`not it.get("content")`），防止 `_retry_content_fetch_failures` 刚成功的 item 被二次下载。
- **`_retry_content_fetch_failures`** 重试前检查文章是否已有 content（正常路径可能已成功），有则直接标记 completed 跳过。

### 6.3 跨文章共享图片

同一图片 URL 可能出现在多篇文章中（如图床公共资源）。`find_articles_by_image_url` 返回所有匹配的 article_id，逐一更新。若无匹配文章（文章已删除），直接标记 completed。

### 6.4 并发安全

`Crawler` 在 daemon 模式下是单线程事件循环，所有 DB 操作串行化。无并发竞争。

### 6.5 Daemon 重启

失败记录在 PostgreSQL 中持久化。重启后下次 crawl 自动拉取 pending 任务重试。

### 6.6 Cloud CI 模式

Cloud CI 使用 SQLite，不做惰性重试。即时重试在函数内部生效（通用代码路径）。

### 6.7 Context 缺失必需字段

重建 item dict 时，对缺失字段提供合理默认值（空字符串、0）。

---

## 7. File Changes Summary

| File | Change |
|------|--------|
| `news/crawler.py` | 新增 `max_immediate_retries`/`max_retry` 配置；`_download_and_parse` 加即时重试；新增 `_record_content_fetch_failure`、`_record_image_download_failures`、`_retry_content_fetch_failures`、`_retry_image_download_failures`；`fetch_all` 集成重试流程 |
| `news/images.py` | `ImageProcessor.__init__` 接受 `max_immediate_retries`；`_download_and_save` 加即时重试 |
| `storage/postgres.py` | 新增 `record_failure`、`get_pending_failures`、`mark_failure_completed`、`mark_failure_retried`、`find_articles_by_image_url`、`update_article_image_url` 方法；`_run_migrations` 新增 failed_tasks DDL |
| `config/loader.py` | 新增 `CRAWLER_MAX_IMMEDIATE_RETRIES`、`CRAWLER_MAX_RETRY` env 映射 |

---

## 8. Testing Strategy

### 8.1 Unit Tests (新文件 `tests/test_failure_retry.py`)

| Test | Description |
|------|-------------|
| `test_record_failure_inserts` | 正常记录失败任务 |
| `test_record_failure_dedup` | 同一 URL+task_type 不重复插入 |
| `test_get_pending_failures` | 查询 status=pending 且 retry_times < max_retry |
| `test_mark_failure_completed` | 标记完成 |
| `test_mark_failure_retried_permanent` | retry_times 达到 max_retry → status='failed' |
| `test_find_articles_by_image_url` | 反查包含图片 URL 的文章 |
| `test_update_article_image_url` | REPLACE content 中的图片 URL |
| `test_download_and_parse_retry_success` | 即时重试第二次成功 |
| `test_download_and_parse_retry_exhausted` | 即时重试耗尽，记录失败 |
| `test_image_download_and_save_retry` | 图片下载即时重试 |
| `test_retry_content_fetch_flow` | 惰性重试完整流程 |
| `test_retry_image_download_flow` | 图片惰性重试 + 文章 content 更新 |

### 8.2 Integration Tests

- `test_fetch_all_includes_pending_retries` — fetch_all 附带 pending 任务
- `test_image_retry_updates_article_content` — 图片重试成功后文章 content 中的 URL 被替换

---

## 9. Rollout

1. Schema migration 通过 `_run_migrations` 自动执行，幂等
2. 默认 `max_retry=3`、`max_immediate_retries=3`
3. 渐进：先上线记录功能，观察几个周期确认记录正常；即时重试和惰性重试同时启用
4. 监控：`SELECT status, count(*) FROM failed_tasks GROUP BY status` 观察失败比例
