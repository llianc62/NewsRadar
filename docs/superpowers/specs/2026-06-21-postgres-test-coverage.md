# PostgreSQL 测试覆盖设计

## 概述

`storage/postgres.py` 是 NewsRadar 核心数据层，承载新闻存储、查询、去重、迁移等功能。当前零测试覆盖，所有质量依赖手工验证。需要建立完整的测试体系，保障后续开发不破坏现有功能——尤其是刚修复的中文全文搜索。

## 测试策略

两层结构：

| 层 | 技术 | 测什么 | 环境要求 |
|---|------|--------|---------|
| **单元测试** | `unittest.mock` + `MagicMock` | SQL 正确性、参数传递、CJK 分流逻辑 | 无，纯 Python |
| **集成测试** | 真实 PostgreSQL（Docker Compose） | 实际数据库行为：去重、索引、FTS/ILIKE 分词匹配 | `docker compose up -d` |

单元测试是第一优先级——快、无依赖、覆盖所有方法签名和参数组合。集成测试覆盖核心数据流：写入 → 去重 → 查询 → 搜索命中。

## 功能清单与测试用例

### 模块级工具函数（3 个，无需 DB 连接）

#### 1. `_load_schema()`

| # | 用例 | 输入 | 预期 |
|---|------|------|------|
| 1.1 | 正常加载 | postgres.sql 存在 | 返回非空字符串，包含 `CREATE TABLE` |
| 1.2 | 文件缺失 | postgres.sql 被删除/移动 | `FileNotFoundError` |

#### 2. `_to_timestamptz(value, fallback_date)`

| # | 用例 | 输入 | 预期 |
|---|------|------|------|
| 2.1 | ISO 8601 完整时间 | `"2026-06-21T10:30:00+08:00"` | `datetime(2026,6,21,10,30, tz=+08:00)` |
| 2.2 | UTC Z 后缀 | `"2026-06-21T02:30:00Z"` | `datetime(2026,6,21,2,30, tz=UTC)` |
| 2.3 | HH:MM 格式 + fallback_date | `"10:30"`, `fallback="2026-06-21"` | `datetime(2026,6,21,10,30, tz=+08:00)` |
| 2.4 | 空字符串 | `""` | `None` |
| 2.5 | None | `None` | `None` |
| 2.6 | 无效格式 | `"not-a-time"` | `None` |
| 2.7 | HH:MM 无 fallback_date | `"10:30"`, `fallback=None` | `None` |
| 2.8 | 非法 HH 值 | `"99:99"` | `None`（捕获 ValueError） |

#### 3. `_contains_cjk(text)`

| # | 用例 | 输入 | 预期 |
|---|------|------|------|
| 3.1 | 纯中文 | `"英伟达"` | `True` |
| 3.2 | 混合中英文 | `"NVIDIA 英伟达 GPU"` | `True` |
| 3.3 | 日文汉字 | `"日本"` | `True` |
| 3.4 | 纯英文 | `"NVIDIA"` | `False` |
| 3.5 | 数字和符号 | `"GPT-4"` | `False` |
| 3.6 | 空字符串 | `""` | `False` |
| 3.7 | 中文标点 | `"，"` | `False`（标点不在 CJK 字符范围） |

---

### 生命周期（6 个方法）

#### 4. `__init__` + `connect()`

| # | 用例 | 预期 |
|---|------|------|
| 4.1 | 正常连接 | pool 创建，`is_connected == True` |
| 4.2 | 重复 connect | 幂等，不创建第二个 pool |
| 4.3 | 连接失败（错误 host） | `psycopg2.OperationalError` |

#### 5. `close()`

| # | 用例 | 预期 |
|---|------|------|
| 5.1 | 正常关闭 | `is_connected == False` |
| 5.2 | 未连接时 close | 幂等，不抛异常 |
| 5.3 | 重复 close | 幂等 |

#### 6. `init_schema()`

| # | 用例 | 预期 |
|---|------|------|
| 6.1 | 全新数据库 | 执行完整 DDL，打印 "Schema initialized" |
| 6.2 | Schema 已存在 | 跳过 DDL，打印 "already exists"，仍执行 migration |
| 6.3 | 调用后 table 存在 | `news_articles` 和 `news_images` 表可查询 |

#### 7. `_schema_ready()`

| # | 用例 | 预期 |
|---|------|------|
| 7.1 | 表存在 | 返回 `True` |
| 7.2 | 表不存在 | 返回 `False` |

#### 8. `_run_migrations()`

| # | 用例 | 预期 |
|---|------|------|
| 8.1 | 全新数据库 — 执行 migration 001 | `idx_fulltext` 被创建，含 content |
| 8.2 | 全新数据库 — 执行 migration 002 | `pg_trgm` 扩展安装，`idx_fulltext_trgm` 被创建 |
| 8.3 | 旧索引不含 content（001 触发） | DROP 旧索引 → 重建含 content 的索引 |
| 8.4 | 索引已含 content（001 跳过） | 不执行任何 DDL |
| 8.5 | trigram 索引已存在（002 跳过） | 不执行任何 DDL |
| 8.6 | 多次运行（幂等） | 第二次运行无变化 |

#### 9. `get_conn()` 上下文管理器

| # | 用例 | 预期 |
|---|------|------|
| 9.1 | 正常使用 | 返回连接，自动 commit |
| 9.2 | 异常时回滚 | 连接 rollback，异常向上传播 |
| 9.3 | 未连接时调用 | `RuntimeError` |
| 9.4 | 连接放回 pool | finally 中 `putconn` 被调用 |

---

### 写入操作（6 个方法）

#### 10. `save_news_data()` — 批量 UPSERT

| # | 用例 | 预期 |
|---|------|------|
| 10.1 | hotlist 首次插入 | INSERT 成功，processed=N |
| 10.2 | hotlist 重复插入（skip_existing=True） | ON CONFLICT DO NOTHING，skipped=N |
| 10.3 | hotlist 重复插入（skip_existing=False） | ON CONFLICT DO UPDATE，content 保留非空值 |
| 10.4 | rss 首次插入 | 使用 (source_id, guid) 去重 |
| 10.5 | rss 重复插入 | ON CONFLICT DO UPDATE |
| 10.6 | manual 首次插入 | 使用 (source_id, url) 去重，overwrite content |
| 10.7 | manual 重复插入 | DO UPDATE SET _UPDATE_SET_OVERWRITE（强制覆盖） |
| 10.8 | fallback 行（无 url 无 guid） | 简单 INSERT，无 ON CONFLICT |
| 10.9 | 空 NewsData | processed=0, skipped=0 |
| 10.10 | source_tiers 为空字典 | 默认 tier=4, priority=0 |
| 10.11 | source_tiers 提供 tier/priority | 使用提供的值 |
| 10.12 | 混合类型（hotlist + rss + manual + fallback） | 各走各的 SQL 模板，互不干扰 |
| 10.13 | crawled_from 参数 | 写入 `crawled_from` 字段 |
| 10.14 | 批量插入性能 | N 条数据在合理时间内完成 |

#### 11. `update_article_content()`

| # | 用例 | 预期 |
|---|------|------|
| 11.1 | 更新存在的文章 | 返回 True，content 被更新，updated_at 刷新 |
| 11.2 | 文章不存在 | 返回 False |
| 11.3 | 空字符串 content | 允许更新为空字符串 |

#### 12. `update_article_full()`

| # | 用例 | 预期 |
|---|------|------|
| 12.1 | 更新所有字段 | title/content/published_at/author/summary/category/tags 全部更新 |
| 12.2 | title 空字符串 | COALESCE 保留旧 title |
| 12.3 | published_at 为 None | COALESCE 保留旧值 |
| 12.4 | tags 为 None | COALESCE 保留旧 tags |
| 12.5 | 文章不存在 | 返回 False |

#### 13. `delete_news()`

| # | 用例 | 预期 |
|---|------|------|
| 13.1 | 删除存在的文章 | 返回 True，关联 images ON DELETE CASCADE |
| 13.2 | 文章不存在 | 返回 False |

#### 14. `save_article_image()`

| # | 用例 | 预期 |
|---|------|------|
| 14.1 | 保存图片记录 | 返回新 id |
| 14.2 | 所有可选字段为空 | width/height/file_size/original_url 允许 NULL |
| 14.3 | article_id 不存在 | 外键约束违反（或依赖应用层保证） |

---

### 查询方法 — 通用过滤器验证

以下过滤器出现在多个查询方法中，需要在一个代表性方法中验证全部组合，其他方法验证方法特有的行为。

#### 过滤器组合矩阵（以 `get_recent_news` 为代表）

| # | 过滤器 | 验证点 |
|---|--------|--------|
| 15.1 | 无过滤 | SQL 仅含 `TRUE` + 默认 confidence>=20 |
| 15.2 | tier=1 | `WHERE ... AND tier = %s`，参数=[1] |
| 15.3 | tier=4 | 同上 |
| 15.4 | category="tech" | `category = %s` |
| 15.5 | min_confidence=50 | `confidence >= %s` |
| 15.6 | 默认 confidence | `WHERE ... (confidence IS NULL OR confidence >= 20)` |
| 15.7 | sentiment=positive | `sentiment_score >= 67` |
| 15.8 | sentiment=negative | `sentiment_score <= 33` |
| 15.9 | sentiment=neutral | `33 < sentiment_score < 67` |
| 15.10 | keyword="芯片" | `%s = ANY(tags)` |
| 15.11 | date_from="2026-06-19" | `published_at >= %s::date` |
| 15.12 | date_to="2026-06-21" | `published_at < %s::date + interval '1 day'` |
| 15.13 | 全部过滤器同时使用 | 所有条件 AND 连接，参数顺序正确 |
| 15.14 | 分页 limit=20, offset=40 | `LIMIT %s OFFSET %s` 作为最后两个参数 |

#### 15.5 搜索过滤器（关键）

| # | 用例 | SQL 特征 | 参数 |
|---|------|---------|------|
| 15.5.1 | 中文搜索 "英伟达" | `ILIKE %s` | `%英伟达%` |
| 15.5.2 | 混合搜索 "NVIDIA芯片" | `ILIKE %s`（含 CJK） | `%NVIDIA芯片%` |
| 15.5.3 | 纯英文搜索 "NVIDIA" | `to_tsvector(...) @@ plainto_tsquery(...)` | `NVIDIA` |
| 15.5.4 | 空搜索字符串 "" | 按 CJK 判断走对应分支 | — |
| 15.5.5 | 日文搜索 "日本経済" | `ILIKE %s`（含 CJK） | `%日本経済%` |
| 15.5.6 | 韩文搜索 "삼성" | `ILIKE %s`（含 CJK 扩展区） | `%삼성%` |

---

### 查询方法 — 各方法特有行为

#### 16. `get_recent_news()`

| # | 用例 | 预期 |
|---|------|------|
| 16.1 | 默认分页 | limit=50, offset=0 |
| 16.2 | 返回字段 | 不含 content（性能：列表页不需要正文） |
| 16.3 | 排序 | `published_at DESC NULLS LAST, heat_score DESC NULLS LAST` |
| 16.4 | 无匹配 | 返回空列表 `[]` |
| 16.5 | RealDictCursor | 返回字典列表，键为列名 |

#### 17. `get_news_count()`

| # | 用例 | 预期 |
|---|------|------|
| 17.1 | 无过滤 | 返回整数（总条数） |
| 17.2 | 带过滤 | 返回匹配条数 |
| 17.3 | 默认 confidence>=20 | 与 get_recent_news 一致 |
| 17.4 | 无匹配 | 返回 0 |

#### 18. `get_sentiment_counts()`

| # | 用例 | 预期 |
|---|------|------|
| 18.1 | 默认 | 返回 `{positive: N, negative: N, neutral: N}` |
| 18.2 | 与查询方法过滤器一致 | 使用相同的 search/tier/keyword/date 参数 |

#### 19. `get_keyword_counts()`

| # | 用例 | 预期 |
|---|------|------|
| 19.1 | 默认 | 返回 `[{tag, cnt}, ...]`，按 cnt DESC |
| 19.2 | limit 参数 | 限制返回条数 |
| 19.3 | 结果排序 | `cnt DESC` |

#### 20. `get_high_impact_count()`

| # | 用例 | 预期 |
|---|------|------|
| 20.1 | 默认（无 date） | 额外 `published_at >= CURRENT_DATE` 过滤 |
| 20.2 | 有 date_from/date_to | 使用日期参数替代 CURRENT_DATE |
| 20.3 | heat_score >= 80 | 固定条件 |

#### 21. `get_stats()`

| # | 用例 | 预期 |
|---|------|------|
| 21.1 | 默认 | 返回 `{t1~t4_count, total_count, today_count, by_source: [...]}` |
| 21.2 | 带 date_from/date_to | 统计过滤后的数据 |
| 21.3 | by_source 排序 | `cnt DESC` |

#### 22. `search_articles()`

| # | 用例 | 预期 |
|---|------|------|
| 22.1 | 方法存在性 | 确认方法签名（如有） |

#### 23. `get_news_by_id()`

| # | 用例 | 预期 |
|---|------|------|
| 23.1 | 存在的文章 | 返回文章字典 + `images` 列表 |
| 23.2 | 不存在的文章 | 返回 None |
| 23.3 | 文章无图片 | `images` 为空列表 |
| 23.4 | 图片排序 | `ORDER BY sort_order` |

#### 24. `get_article_by_url()`

| # | 用例 | 预期 |
|---|------|------|
| 24.1 | 匹配的 URL | 返回 `{id, title, url}` |
| 24.2 | 无匹配 | 返回 None |
| 24.3 | 多条匹配 | 返回第一条（`ORDER BY id LIMIT 1`） |

#### 25. `get_articles_without_content()`

| # | 用例 | 预期 |
|---|------|------|
| 25.1 | 存在空 content 文章 | 返回列表，按 tier ASC, priority DESC |
| 25.2 | limit 参数生效 | 返回最多 limit 条 |
| 25.3 | url 为空的不返回 | `url != ''` |

#### 26. `get_latest_cloud_sync_date()`

| # | 用例 | 预期 |
|---|------|------|
| 26.1 | 存在 cloud 记录 | 返回最大 `crawled_at` |
| 26.2 | 无 cloud 记录 | 返回 None |

---

### 批量辅助方法

#### 27. `_build_row()`

| # | 用例 | 预期 |
|---|------|------|
| 27.1 | 完整 NewsItem | 19 元素元组，字段映射正确 |
| 27.2 | None category | category=None |
| 27.3 | 空 tags | tags=[] |
| 27.4 | None published_at | ts_pub=None |
| 27.5 | None crawled_at + crawl_date | ts_crawled 使用 crawl_date |

#### 28. `_execute_batch()` + `_execute_batch_retry()`

| # | 用例 | 预期 |
|---|------|------|
| 28.1 | 小于 page_size 的批次 | 一次 execute_values 调用，返回 (N, 0) |
| 28.2 | 大于 page_size 的批次 | 多次分页调用 |
| 28.3 | 单行失败 | page_size=1 时异常 → 返回 (0, 1)，不中断后续 |
| 28.4 | 整批失败 | 降级到 page_size=10 → 1 重试 |
| 28.5 | 空列表 | 返回 (0, 0) |

---

### 集成测试用例（需真实 PostgreSQL）

| # | 场景 | 验证点 |
|---|------|--------|
| I1 | 写入 → 查询 | 存一条新闻，用 get_recent_news / get_news_by_id 查回，字段完整 |
| I2 | 去重 hotlist | 同 source_id+url 再次写入，content 在 skip_existing=False 时保留旧值 |
| I3 | 去重 rss | 同 source_id+guid 再次写入 |
| I4 | 去重 manual | 同 source_id+url 再次写入，content 强制覆盖 |
| I5 | 中文搜索命中（ILIKE） | 存含 "英伟达发布新芯片" 的内容，search="英伟达" 能查到 |
| I6 | 英文搜索命中（FTS） | 存含 "NVIDIA released new GPU" 的内容，search="NVIDIA" 能查到 |
| I7 | 搜索不命中 | search="不存在的关键词" → 返回空 |
| I8 | 全部过滤器组合 | tier + sentiment + keyword + date + search 同时使用 |
| I9 | 日期范围 | date_from/date_to 过滤边界正确（含起始日全天，不含结束日次日） |
| I10 | 删除级联 | 删文章 → 关联 images 行一并删除 |
| I11 | 迁移幂等 | 多次运行 init_schema，无报错，索引存在 |
| I12 | 分页 | limit+offset 正确分页 |

---

## 测试文件组织

```
tests/
├── conftest.py                          # 现有：parser fixture
├── conftest_db.py                       # 新增：PostgreSQL test fixtures
├── test_postgres_utils.py               # _load_schema, _to_timestamptz, _contains_cjk
├── test_postgres_lifecycle.py           # connect, close, init_schema, migrations
├── test_postgres_write.py               # save_news_data, update_*, delete_*, save_image
├── test_postgres_query_filters.py       # 通用过滤器验证（tier, sentiment, keyword, date, search）
├── test_postgres_query_methods.py       # 各查询方法特有行为
├── test_postgres_batch.py               # _build_row, _execute_batch, _execute_batch_retry
└── test_postgres_integration.py         # 集成测试（需 PG，可标记 @pytest.mark.integration）
```

## 优先级

| 优先级 | 文件 | 原因 |
|--------|------|------|
| **P0** | `test_postgres_utils.py` | CJK 检测和 _to_timestamptz 是搜索和数据正确性的基础 |
| **P0** | `test_postgres_query_filters.py` | 搜索分流逻辑（CJK→ILIKE, ASCII→FTS）是刚修复的关键功能 |
| **P1** | `test_postgres_write.py` | 写入/去重是数据完整性的保障 |
| **P1** | `test_postgres_lifecycle.py` | 迁移的正确性影响索引和搜索功能 |
| **P2** | `test_postgres_query_methods.py` | 各方法特有逻辑 |
| **P2** | `test_postgres_batch.py` | 批量辅助方法 |
| **P3** | `test_postgres_integration.py` | 补充真实数据库行为验证 |

## 覆盖率目标

- 单元测试行覆盖率：≥ 80%（与项目标准一致）
- 搜索相关代码路径：100%（CJK/ASCII 两条分支必须全测）
- 迁移逻辑：100%（每个 migration 的触发/跳过路径）
