# Heat Score 设计方案

## 1. 当前状态

### 1.1 数据库 Schema

`news_articles` 表已定义但**未填充**的字段：

| 字段 | 类型 | 索引 | 数据状态 |
|------|------|------|----------|
| `rank` | SMALLINT | — | 已有数据（最新排名） |
| `ranks` | SMALLINT[] | — | 废数据（仅首次排名，永不更新） |
| `heat_score` | INTEGER (0-100) | `idx_heat_score DESC` | NULL |
| `sentiment_score` | INTEGER (0-100) | — | NULL |
| `confidence` | INTEGER (0-100) | — | NULL |

### 1.2 数据流现状

```
NewsNow API ──► NewsFetcher.crawl_websites() ──► NewsnowFetcher.fetch() ──► Crawler ──► PostgreSQL
                  ↓                                  ↓
           每个 item 只有:                    每个 item dict:
           title, url, mobileUrl             rank=index, ranks=[index]
                                             但 total 未传递
```

### 1.3 关键缺陷

1. `ranks` 是 `SMALLINT[]`，UPSERT 时 `_UPDATE_SET` 不包含它 → 跨轮次永不更新
2. 未捕获 `total`（榜单总条数），无法计算百分位
3. `heat_score` 无写入逻辑

---

## 2. 数据模型变更

### 2.1 ranks 字段类型变更

```sql
-- 旧
ranks SMALLINT[] DEFAULT '{}'

-- 新：每轮一个 [rank, total] 数组
ranks JSONB DEFAULT '[]'
```

### 2.2 数据结构

```
ranks (JSONB):  [[7,20], [5,20], [2,20]]
                 ↑            ↑        ↑
              第1轮        第2轮    第3轮(最新)
rank (SMALLINT): 2          ← 保留，最新排名，方便 SQL 排序
heat_score (INTEGER): 73    ← 独立列，已有索引
```

三个字段各司其职：`ranks` 存纯净轨迹，`rank` 供快速排序，`heat_score` 供热度排序。

### 2.3 NewsItem 新增字段

```python
@dataclass
class NewsItem:
    # ... 现有字段 ...
    total: int = 0        # 新增：榜单总条数
    heat_score: int = 0   # 新增：热度值（首次上榜用百分位，后续在旧值上增量调整）
```

---

## 3. 热度计算公式

### 3.1 首次上榜

```
percentile = (1 − rank / total) × 100
heat_score = round(percentile)
```

### 3.2 后续爬取

拿 `ranks` 的最后一条 `[last_r, last_t]` 和本轮 `[new_r, new_t]` 计算百分位差：

```
percentile_new = (1 − new_r / new_t) × 100
percentile_old = (1 − last_r / last_t) × 100
delta = percentile_new − percentile_old
```

| 事件 | 判定条件 | 公式 |
|------|----------|------|
| **仍在榜** | URL 在本轮抓取结果中 | `heat_score += round(delta × 0.3)` |
| **掉出榜单** | URL 不在本轮的 source 结果中 | `heat_score = round(heat_score × 0.7)` |

最终 `clamp(heat_score, 0, 100)`。

### 3.3 阻尼系数说明

- **0.3**：升/降榜调整系数。单轮调整上限约 ±30 分。避免单轮波动过大，热度需多轮积累。
- **0.7**：掉榜衰减系数。每次不在榜，热度保留 70%。连续 3 轮掉榜后降至 ~34%，自然冷却。

### 3.4 轨迹示例

```
第1轮: #10/20, p=50   → heat = 50          首次上榜
第2轮: #7/20,  p=65   → heat = 50+4.5=55   升榜 (+15×0.3)
第3轮: #3/20,  p=85   → heat = 55+6=61     升榜 (+20×0.3)
第4轮: 不在榜          → heat = 61×0.7=43   掉出榜单
第5轮: 不在榜          → heat = 43×0.7=30   再次掉出
第6轮: #5/20,  p=75   → heat = 30+18=48    重新上榜，在衰减值上调整 (+60×0.3)
第7轮: #1/20,  p=95   → heat = 48+6=54     升榜 (+20×0.3)
第8轮: #1/20,  p=95   → heat = 54+0=54     稳榜（delta=0）
```

### 3.5 RSS 来源

RSS 无排名信号，`heat_score = NULL`，后续单独设计（可能基于发布时间衰减的"新鲜度"）。

---

## 4. 实现计划

### 4.1 文件变更清单

| 文件 | 变更 | 说明 |
|------|------|------|
| `news/fetcher/newsnow.py` | `crawl_websites` 传 `total`，`fetch` 输出 `total` | 数据源头 |
| `news/models.py` | `NewsItem` 新增 `total`、`heat_score` | 数据模型 |
| `storage/postgres.sql` | `ranks SMALLINT[]` → `ranks JSONB DEFAULT '[]'` | Schema |
| `storage/postgres.py` | 新增 `_calc_heat_score`、`_process_hotlist_heat`，改 `save_news_data`、`_build_row`、`_UPDATE_SET` | 核心逻辑 |
| `tests/test_heat_score.py` | 单元测试 | 验证 |

### 4.2 整体数据流

```
save_news_data(news_data):
    // 1. 按现有逻辑分区 (hotlist/rss/manual/fallback)

    // 2. 对 hotlist：按源逐一处理热度
    for each source_id in hotlist:
        _process_hotlist_heat(source_id, this_round_items)

    // 3. 正常走 _build_row → batch UPSERT
    // 4. _UPDATE_SET 新增 ranks, heat_score
```

### 4.3 核心流程：`_process_hotlist_heat`

```
_process_hotlist_heat(source_id, items):
    // ① 查询当天 DB 中该源的所有 hotlist 记录（作为上轮快照）
    SELECT url, heat_score, rank, ranks
    FROM news_articles
    WHERE source_id = $1
      AND source_type = 'hotlist'
      AND crawled_at::date = CURRENT_DATE

    → db_map = {url: {heat_score, rank, ranks}}

    // ② 对比分类
    this_urls = {item.url for item in items}
    db_urls  = set(db_map.keys())

    new_urls     = this_urls - db_urls   // 首次上榜
    existing_urls = this_urls & db_urls   // 仍在榜
    dropped_urls  = db_urls - this_urls   // 掉出榜单

    // ③ 首次上榜 → 百分位计算
    for item in items where item.url in new_urls:
        item.heat_score = round((1 - item.rank / item.total) * 100)
        item.ranks = [[item.rank, item.total]]

    // ④ 仍在榜 → 增量调整
    for item in items where item.url in existing_urls:
        prev = db_map[item.url]
        item.heat_score = _calc_heat_score(
            prev_heat  = prev["heat_score"],
            prev_ranks = prev["ranks"],
            new_rank   = item.rank,
            new_total  = item.total,
        )
        item.ranks = prev["ranks"] + [[item.rank, item.total]]

    // ⑤ 掉出榜单 → ×0.7 衰减
    UPDATE news_articles
    SET heat_score = ROUND(GREATEST(0, LEAST(100, heat_score * 0.7))),
        ranks = ranks || '[]'::jsonb   // 追加空标记，不追加排名
    WHERE source_id = $1
      AND source_type = 'hotlist'
      AND url = ANY(dropped_urls)
```

### 4.4 核心函数

```python
def _calc_heat_score(
    prev_heat: int | None,
    prev_ranks: list,       # [[7,20], [5,20]]
    new_rank: int,
    new_total: int,
) -> int:
    """计算热度值，返回 0-100。"""
    if not prev_ranks or prev_heat is None:
        return round(clamp((1 - new_rank / new_total) * 100, 0, 100))

    last_r, last_t = prev_ranks[-1]
    last_pct = (1 - last_r / last_t) * 100
    new_pct  = (1 - new_rank / new_total) * 100
    delta    = (new_pct - last_pct) * 100

    return round(clamp(prev_heat + delta * 0.3, 0, 100))
```

### 4.5 _UPDATE_SET 变更

`ranks` 和 `heat_score` 加入 UPSERT 更新：

```python
_UPDATE_SET = """title = EXCLUDED.title,
        rank = EXCLUDED.rank,
        mobile_url = EXCLUDED.mobile_url,
        crawled_at = EXCLUDED.crawled_at,
        updated_at = NOW(),
        priority = EXCLUDED.priority,
        tier = EXCLUDED.tier,
        summary = EXCLUDED.summary,
        category = EXCLUDED.category,
        tags = EXCLUDED.tags,
        ranks = EXCLUDED.ranks,
        heat_score = EXCLUDED.heat_score,
        content = CASE
            WHEN news_articles.content IS NULL OR news_articles.content = ''
            THEN EXCLUDED.content
            ELSE news_articles.content
        END"""
```

### 4.6 _build_row 变更

新增 `ranks` 和 `heat_score` 到 21 元素 tuple：

```python
return (
    item.title,
    source_id,
    item.source_name,
    item.source_type,
    tier,
    priority,
    item.url,
    item.mobile_url,
    item.rank,
    item.guid,
    ts_pub,
    item.summary,
    item.author,
    item.content,
    item.category if item.category else None,
    item.tags if item.tags else [],
    crawled_from,
    ts_crawled,
    item.ranks if item.ranks else [],
    item.heat_score,           # 新增
)
```

### 4.7 步骤拆解

| 步骤 | 内容 |
|------|------|
| 1 | `NewsItem` 加 `total`、`heat_score` 字段 |
| 2 | `NewsnowFetcher.fetch()` 输出 `total` |
| 3 | Schema: `ranks` 类型改为 JSONB |
| 4 | 实现 `_calc_heat_score()` 计算函数 |
| 5 | 实现 `_process_hotlist_heat()` 按源对比逻辑 |
| 6 | `_build_row` 新增 `ranks`、`heat_score` 输出 |
| 7 | `_UPDATE_SET` 新增 `ranks`、`heat_score`、`_COLUMNS` 更新 |
| 8 | 写单元测试 |

---

## 5. 数据库 Migration

旧数据由用户清除，无需 migration 脚本。仅需改 schema：

```sql
-- 直接改列类型（旧数据已清除）
ALTER TABLE news_articles ALTER COLUMN ranks TYPE JSONB USING '[]'::jsonb;
ALTER TABLE news_articles ALTER COLUMN ranks SET DEFAULT '[]'::jsonb;
```

---

## 6. 排序策略

前端列表页 `get_recent_news` 默认排序增加 `heat_score` 权重（已有 `idx_heat_score` 索引）：

```sql
ORDER BY COALESCE(heat_score, 0) DESC, COALESCE(published_at, crawled_at) DESC
```

如果后续需要热度与时效性混合排序，可改为：

```sql
ORDER BY COALESCE(heat_score, 0) * 0.6 + EXTRACT(EPOCH FROM AGE(NOW(), COALESCE(published_at, crawled_at))) * -0.4 DESC
```

---

## 7. Edge Cases

| 场景 | 处理 |
|------|------|
| 同一标题不同 URL（跨平台同名新闻） | 不变 — 仍按 `source_id + url` 去重 |
| 榜单 total 变化（如从 20 条变 50 条） | 自然处理：百分位用**每轮的 t** 各自计算 |
| 同轮 URL 第一次出现（新上榜） | 按首次公式，不受老数据影响 |
| 掉榜多轮后重新上榜 | DB 里查得到（heat 已被衰减过），在衰减值上继续调整 |
| 跨天：昨天上榜今天不在，今天又重新上榜 | 只查当天数据，昨天记录不参与对比，按首次公式重新计算 |
| RSS 来源 | `heat_score = NULL`，不参与热度排序 |
| 同一 source 一天内多次爬取 | 每次爬取查询当天已有记录，自然累积排名轨迹 |
