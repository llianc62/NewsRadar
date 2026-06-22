# heat_score 云端快照设计

## 1. 背景

### 1.1 当前状态

NewsRadar 有两套运行模式：

| 模式 | 存储 | 热度计算 |
|------|------|----------|
| **Cloud CI** (GitHub Actions) | SQLite → S3 | 无 |
| **Local Daemon** | PostgreSQL | `_process_hotlist_heat` 增量计算 |

Local daemon 的热度计算 (`_process_hotlist_heat`) 依赖 `rank` 和 `total` 做百分位转
换及跨轮次增量调整。Cloud CI 只存原始数据，不参与热度计算。

### 1.2 问题

Local daemon 长时间未运行时，通过 `sync_from_cloud` 从 S3 同步下来的历史数据
`heat_score` 始终为 0。原因：

1. Cloud CI 抓取时不计算热度，SQLite 中无此字段。
2. `_rows_to_newsdata` 未从 SQLite 行读取 `heat_score`，`NewsItem` 默认为 0。
3. 同步数据没有 `total`（未存未传），被 `_process_hotlist_heat` 的
   `valid_items` 过滤跳过，无法在 PG 侧补算。

结果：同步下来的历史条目热度为 0，后续也不在榜，热度永远为 0。

### 1.3 目标

让 cloud CI 抓取时算好一个**百分位热度快照**存下来，同步到 PG 时作为 baseline。
后续 daemon 抓取如果在榜，增量算法自然接管。

## 2. 设计

### 2.1 核心公式

```
heat_score = round(clamp((1 - rank / total) * 100, 0, 100))
```

- `rank`：条目在榜单中的排名（1-based）
- `total`：榜单总条目数
- 结果：0-100 的整数，排名越靠前值越高

这是 `_process_hotlist_heat` 中 `new_urls` 分支的同款计算，保持一致性。

### 2.2 数据流

```
┌─────────────────────────────────────────────────────────────────┐
│ Cloud CI                                                        │
│                                                                  │
│  fetcher ──► rank, total                                        │
│                │                                                 │
│                ▼                                                 │
│           计算 heat_score = round((1-rank/total)*100)             │
│                │                                                 │
│                ▼                                                 │
│           _to_newsdata ──► NewsItem(heat_score=85, total=150)    │
│                │                                                 │
│                ▼                                                 │
│           Sqlite.save_news_data ──► INSERT ... heat_score=85     │
│                │                                                 │
│                ▼                                                 │
│           S3 upload (.db file)                                   │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│ Sync (daemon 启动时)                                             │
│                                                                  │
│  S3 download ──► .db file                                        │
│                   │                                              │
│                   ▼                                              │
│              _read_sqlite_db ──► rows (含 heat_score=85)         │
│                   │                                              │
│                   ▼                                              │
│              enrich_content (补 body + images)                    │
│                   │                                              │
│                   ▼                                              │
│              _rows_to_newsdata ──► NewsItem(heat_score=85,       │
│                                             total=0)              │
│                   │                                              │
│                   ▼                                              │
│              PG save_news_data                                    │
│                   │                                              │
│                   ▼                                              │
│              _process_hotlist_heat                                │
│                valid_items = [it for it in items if it.total>0]  │
│                → total=0, 跳过                                    │
│                → heat_score 保持 85 ✓                             │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│ 后续 daemon 抓取（同一条仍在榜）                                    │
│                                                                  │
│  fetcher ──► rank=5, total=150                                   │
│                │                                                 │
│                ▼                                                 │
│              _to_newsdata ──► NewsItem(heat_score=97, total=150) │
│                │                                                 │
│                ▼                                                 │
│              PG save_news_data                                    │
│                │                                                 │
│                ▼                                                 │
│              _process_hotlist_heat                                │
│                total=150 > 0, 走 existing_urls 分支                │
│                → _calc_heat_score(prev_heat=85, new_rank=5,      │
│                                    new_total=150)                 │
│                → 增量调整 ✓                                       │
└─────────────────────────────────────────────────────────────────┘
```

### 2.3 为什么不在同步时由 `_process_hotlist_heat` 重新计算

同步数据本质上是**历史快照的批量导入**，而不是当轮抓取：

- 同一天可能有多个时间点的数据合并进来，但没有轮次概念
- 没有 `ranks` 历史（`[[rank, total], ...]`），无法做增量调整
- 没有 `total` 字段，无法参与百分位计算

因此让 cloud CI 在抓取时算好百分位快照，同步时直接使用，是最简单、最正确的方案。

## 3. 实施

### 3.1 修改文件清单

| # | 文件 | 改动 | 说明 |
|---|------|------|------|
| 1 | `news/fetcher/newsnow.py` | 在 item dict 中加入 `heat_score` | fetcher 输出带百分位快照 |
| 2 | `storage/sqlite.sql` | 加 `heat_score INTEGER DEFAULT NULL` 列 | SQLite 持久化 |
| 3 | `storage/sqlite.py` | INSERT 增加 `heat_score` | 写入 DB |
| 4 | `news/crawler.py` | `_rows_to_newsdata` 读取 `heat_score` | 同步时透传 |

### 3.2 详细改动

#### 3.2.1 `news/fetcher/newsnow.py`

在 `fetch()` 方法中，每条 item dict 的构建处增加：

```python
# 现有
"rank": ranks[0] if ranks else 99,
"ranks": ranks,
"total": total,

# 新增
"heat_score": round(max(0, min(100, (1 - rank/total) * 100))) if total > 0 else 0,
```

注意：需要 `total > 0` 保护，`total` 可能为 0（接口异常时）。

#### 3.2.2 `storage/sqlite.sql`

在 `rank INTEGER` 行后增加：

```sql
heat_score INTEGER DEFAULT NULL,
```

#### 3.2.3 `storage/sqlite.py`

`save_news_data` 方法中：

INSERT 列清单追加 `heat_score`：

```python
"""INSERT OR IGNORE INTO news_items
   (title, source_id, source_name, source_type,
    tier, priority, url, mobile_url, rank,
    heat_score,
    guid, published_at, summary, author,
    category, tags, created_at)
   VALUES (
    ?, ?, ?, ?, ?, ?, ?, ?, ?,
    ?,
    ?, ?, ?, ?, ?, ?, ?
   )"""
```

VALUES 绑定追加 `item.heat_score`：

```python
(
    item.title,
    ...
    item.rank,
    item.heat_score,   # ← 新增
    item.guid,
    ...
)
```

#### 3.2.4 `news/crawler.py` — `_rows_to_newsdata`

`NewsItem(...)` 构造中增加：

```python
item = NewsItem(
    ...
    rank=row.get("rank") or 0,
    heat_score=row.get("heat_score") or 0,   # ← 新增
    guid=row.get("guid", ""),
    ...
)
```

### 3.3 无需改动的部分

| 组件 | 原因 |
|------|------|
| `_to_newsdata` | 已从 dict 读取 `d.get("heat_score", 0)`，无需改动 |
| `_process_hotlist_heat` | `valid_items` 过滤逻辑不变；同步数据 `total=0` 被跳过，保留快照值 |
| `_calc_heat_score` | 算法不变 |
| `save_news_data` (PG) | `_build_row` 和 `_UPDATE_SET` 已包含 `heat_score` |
| `NewsItem` 模型 | `heat_score: int = 0` 已存在 |
| RSS fetcher | RSS 无榜单概念，`total=0`，`heat_score` 保持默认 0 |

## 4. 边界情况

### 4.1 `total = 0`

新闻热榜接口异常时可能返回 `total=0`。此时 `heat_score = 0`，不做除法。
对应 `_process_hotlist_heat` 中的 `valid_items` 也会跳过该项。

### 4.2 `rank = 0` 或 > `total`

`rank` 可能为 0（未排名）或超过 `total`（接口数据异常）。
`clamp(0, 100)` 保证结果始终在合法范围。

### 4.3 同一条目多个日期

同一个 URL 可能在不同日期出现在榜单上。每天独立计算快照，不做跨天追踪。
跨天热度演化由 daemon 的 `_process_hotlist_heat` 处理（每天查询 `CURRENT_DATE`
的 DB 记录作为 prev snapshot）。

### 4.4 RSS 条目

RSS 条目没有 `rank`/`total` 概念，`heat_score` 为 NULL（SQLite）或 0（NewsItem 默认）。
同步到 PG 后 `_process_hotlist_heat` 只处理 `source_type='hotlist'`，不受影响。

### 4.5 存量 SQLite 文件

已有的 `.db` 文件没有 `heat_score` 列。`_init_tables` 中的 migration 模式
（`ALTER TABLE ADD COLUMN` try/except）会自动补列，存量行该字段为 NULL，
`_rows_to_newsdata` 中 `row.get("heat_score") or 0` 会转为 0。

## 5. 测试计划

### 5.1 单元测试

| 测试 | 文件 | 说明 |
|------|------|------|
| `test_heat_snapshot_rank1_total100` | `tests/test_heat_score.py` | rank=1, total=100 → 99 |
| `test_heat_snapshot_rank100_total100` | 同上 | rank=100, total=100 → 0 |
| `test_heat_snapshot_rank50_total100` | 同上 | rank=50, total=100 → 50 |
| `test_heat_snapshot_total_zero` | 同上 | total=0 → 0（安全 fallback）|

### 5.2 集成测试

| 测试 | 说明 |
|------|------|
| SQLite INSERT 含 heat_score | 写入后读回，验证字段正确 |
| `_rows_to_newsdata` 读取 heat_score | 模拟 SQLite row dict，验证 NewsItem 构造 |
| 同步数据经 `_process_hotlist_heat` | 构造 `total=0, heat_score=85` 的 NewsItem，验证不被覆盖 |
| 存量 SQLite 文件兼容 | 无 `heat_score` 列的 SQLite row → NewsItem.heat_score = 0 |
