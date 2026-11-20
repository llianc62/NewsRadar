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

## WAF 站点抓取（crawl4ai）

`_WAF_DOMAINS = {xueqiu.com, huxiu.com, juejin.cn}` 三个阿里云 WAF 站点不走 requests，改由 crawl4ai（`browser_type="undetected"` + `magic` + `simulate_user`）驱动浏览器抓 HTML，拿到 HTML 后仍走现有 parser 提取正文。

- **浏览器复用**：`AsyncWebCrawler` 跑在常驻后台事件循环线程里，跨请求复用；`_crawl4ai_arun_lock` 串行化 `arun()`（crawl4ai 非 thread-safe）。
- **WAF 挑战检测**：`_looks_like_waf_challenge()` 命中滑块页特征 → 视为失败。
- **按会话拉黑**：阿里云 WAF 按浏览器 profile/cookie 拉黑，同浏览器内重试必再被挑战/超时 → `_recycle_crawl4ai()` 关旧浏览器、换全新 profile 再试（末次不回收）。
- **降频**：`_throttle_waf_request()` 在每次 `arun()` 前补足 `waf_request_interval`（秒，+1s 抖动）的最小间隔，把同轮数十篇的背靠背 burst 拉平，避免触发 WAF 频控。
- **错误透传**：`_download_with_crawl4ai()` 返回 `(html, error)`，失败时把真实原因（异常文本 / `result.error_message` / `"WAF challenge detected"`）写入 `failed_tasks.context.error`，不再是笼统的 `"crawl4ai fetch failed"`。
- **driver 回收**：`_close_crawl4ai()` 在 `crawler.close()` 卡住（坏浏览器卡 CDP）超时时，兜底 `_force_stop_crawl4ai_driver()` 显式 `playwright.stop()`，防止每次 recycle 泄漏一个 `run-driver` node 进程。

> **换入口调研（结论：搁置）**：huxiu 无低门槛全文入口——`m.huxiu.com` / `/api/` 均返回 405（同一 WAF），RSS 仅给 summary，RSSHub `?mode=fulltext` 本质仍抓文章页受同一 WAF 约束。故只能走"降频/退避"或未来的代理/IP 轮换（此前已 revert proxy，暂不做）。

## 图片处理

`ImageProcessor`（[news/images.py](news/images.py)）：
- `ThreadPoolExecutor` 并发下载（max 20 workers）
- 通过 `FileStorage` 后端保存（本地或 S3）
- 自动 Content-Type → 扩展名检测
- 图片路径替换：Markdown `![](https://...)` → `![](saved_path)`

## 失败重试

`failed_tasks` 表记录失败的 `content_fetch` / `image_download` 任务。`retry_failed_tasks()` 在每轮 `fetch_all` 后自动执行：
- 最大重试 3 次（`max_retry`）
- 成功后标记 `completed`，耗尽标记 `failed`
- 去重：`(task_type, url) WHERE status = 'pending'` 部分唯一索引
- 过期清理：每轮重试前删除超过 `failure_retention_days`（默认 7 天）的任务，防止表无限膨胀
- 批量上限：每轮每类型最多重试 `retry_batch_size`（默认 200）条，`ORDER BY created_at DESC` 新任务优先
- **指数退避**：`get_pending_failures()` 只取距上次尝试（`last_retry`，新任务取 `created_at`）已过 `retry_backoff_base_minutes * 2^retry_times` 分钟的任务（默认基数 60 → 1h/2h/4h…），避免每小时重复重锤同一批 backlog（WAF 频控的主要诱因）
- 异常隔离：单条任务的异常（含唯一键冲突，自动降级标 completed 让位）只跳过该条，不中断整轮重试

## 关键词提取

`JiebaAnalyzer.extract_keywords()` — 在 `_download_and_parse()` 中作为 tags 的 fallback。详见 [analyzer.md](analyzer.md)。

## Crawler 初始化

所有资源惰性初始化（Lazy Init）：
- HTTP session（requests）
- DB 连接
- ThreadPoolExecutor（2 workers）
- ImageProcessor
- Analyzer
- crawl4ai 浏览器（后台事件循环，仅 WAF 站点首次抓取时启动）

`Crawler` 实例通过 `create_app(crawler=web_crawler)` 注入 web 层，共享 fetch 逻辑。

### 相关配置（`config.yaml` → `crawler`）

| 键 | 默认 | 说明 |
|----|------|------|
| `timeout` | 30 | 单请求超时（秒） |
| `max_retry` | 3 | crawl4ai / 失败任务最大重试次数 |
| `max_workers` | 20 | 富化线程池并发 |
| `waf_request_interval` | 3 | WAF 浏览器请求最小间隔（秒，+1s 抖动） |
| `retry_backoff_base_minutes` | 60 | 失败重试指数退避基数（分钟，每次翻倍） |
| `retry_batch_size` | 200 | 每轮每类型重试上限 |
| `failure_retention_days` | 7 | failed_tasks 过期清理天数 |

## 关键文件

| 文件 | 用途 |
|------|------|
| `news/crawler.py` | Crawler 类 — 完整编排逻辑 |
| `news/fetcher/newsnow.py` | NewsnowFetcher — hot-list API（retry + jitter） |
| `news/fetcher/rss.py` | RssFetcher — RSS/Atom/JSON Feed |
| `news/images.py` | ImageProcessor — 图片下载 + 路径替换 |
| `news/models.py` | NewsItem / NewsData dataclass |
| `news/constants.py` | Tier 标签/颜色、来源类型、情感阈值 |
