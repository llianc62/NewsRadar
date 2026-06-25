# Crawler — 新闻爬取管线

`Crawler` 类位于 [news/crawler.py](news/crawler.py)，是 fetch → enrich → analyze → persist 的核心编排者。

## 公共 API

| 方法 | 用途 |
|------|------|
| `fetch(url, output_style)` | 单 URL 抓取 → 解析 → 持久化 |
| `fetch_all(with_content, output_style)` | 全量：hot-list + RSS → enrich → analyze → persist |
| `enrich_content(news_data)` | 补下载正文 + 图片（共享于 fetch_all + cloud sync） |
| `sync_from_cloud()` | 从 S3 下载 SQLite → enrich → UPSERT PG |
| `retry_failed_tasks()` | 重试失败的 content_fetch / image_download 任务 |

## fetch_all 完整管线

```
1. NewsnowFetcher.fetch()  ──► Hot-list API（带 retry + jitter）
2. RssFetcher.fetch()      ──► RSS/Atom/JSON Feed
3. persist()               ──► 先存元数据（content 可为空）
4. enrich_content()        ──► ThreadPoolExecutor 下载 HTML → parse → tags
    │                           └── ImageProcessor 并发下载图片
5. analyzer.analyze_sentiment() ──► 情感分析
6. analyzer.analyze_heat()      ──► 热度评分（仅 hotlist）
7. persist()               ──► UPSERT 完整数据
```

## 内容富化（enrich_content）

`_run_batch_parse()` — ThreadPoolExecutor 并发下载 HTML → parser 提取正文：

```
下载 HTML → parser.parse(html, url) → markdown + metadata
    │
    ├─ tags = parsed.tags（trafilatura meta keywords）
    └─ if not tags: tags = jieba TextRank 提取 5 个关键词
```

`_run_batch_image_download()` — `ImageProcessor` 并发下载图片，返回 `{url: saved_path}` 映射，替换 Markdown 中的远程图片链接。

## 图片处理

`ImageProcessor`（[news/images.py](news/images.py)）：
- `ThreadPoolExecutor` 并发下载（max 20 workers）
- 通过 `FileStorage` 后端保存（本地或 S3）
- 自动 Content-Type → 扩展名检测
- 图片路径替换：Markdown `![](https://...)` → `![](saved_path)`

## 失败重试

`failed_tasks` 表记录失败的 `content_fetch` / `image_download` 任务。`retry_failed_tasks()` 在每轮 `fetch_all` 后自动执行：
- 最大重试 3 次
- 成功后标记 `completed`，耗尽标记 `failed`
- 去重：`(task_type, url) WHERE status = 'pending'` 部分唯一索引

## 关键词提取

`JiebaAnalyzer.extract_keywords()` — 在 `_download_and_parse()` 中作为 tags 的 fallback。详见 [analyzer.md](analyzer.md)。

## Crawler 初始化

所有资源惰性初始化（Lazy Init）：
- HTTP session（requests）
- DB 连接
- ThreadPoolExecutor（2 workers）
- ImageProcessor
- Analyzer

`Crawler` 实例通过 `create_app(crawler=web_crawler)` 注入 web 层，共享 fetch 逻辑。

## 关键文件

| 文件 | 用途 |
|------|------|
| `news/crawler.py` | Crawler 类 — 完整编排逻辑 |
| `news/fetcher/newsnow.py` | NewsnowFetcher — hot-list API（retry + jitter） |
| `news/fetcher/rss.py` | RssFetcher — RSS/Atom/JSON Feed |
| `news/images.py` | ImageProcessor — 图片下载 + 路径替换 |
| `news/models.py` | NewsItem / NewsData dataclass |
| `news/constants.py` | Tier 标签/颜色、来源类型、情感阈值 |
