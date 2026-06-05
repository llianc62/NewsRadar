# 数据库选型与同步方案设计

**日期**: 2026-06-05
**状态**: 已确认

---

## 1. 背景与问题

### 当前架构

- **Crawl 模块**：运行在 GitHub Action，每小时抓取新闻，存入每日 SQLite 文件（`news/YYYY-MM-DD.db`），上传至 S3（七牛 Kodo）
- **Web 服务**：FastAPI + Jinja2 SSR，运行在本地服务器，**当前使用纯 Mock 数据**，未连接任何数据库

### 核心问题

1. SQLite 不适合作为本地 Web 服务的数据库（并发写入瓶颈、数据量增长后性能下降、功能不足）
2. 云端 SQLite 仅保存原始抓取数据，本地需要更丰富的字段和分析能力
3. 需要一个可靠的两端数据同步机制

---

## 2. 数据库选型

### 选型结论：PostgreSQL

### 选型理由

| 考量维度 | PostgreSQL 优势 |
|---------|----------------|
| **全文搜索** | pg_trgm（模糊匹配）+ tsvector（中文分词），是新闻文本检索的核心能力 |
| **灵活扩展** | JSONB 支持动态增减字段（关键词、标签、实体等），不需要频繁改表 |
| **数值分析** | 窗口函数、统计聚合能力强，支撑后续股票 K 线和基本面分析 |
| **大表管理** | 分区表、BRIN 索引，适合按时间增长的年级别数据 |
| **数组类型** | `TEXT[]` 原生支持标签数组，配合 GIN 索引高效查询 |
| **生态** | SQLAlchemy + Alembic 与 FastAPI 结合最成熟 |

### 备选方案对比

| 方案 | 优点 | 缺点 |
|------|------|------|
| **PostgreSQL（选用）** | 全文搜索、JSONB、数组、窗口函数 | 需单独安装 |
| **MySQL/MariaDB** | 部署简单 | 中文全文搜索弱，JSON 函数不如 PG |
| **SQLite** | 零配置 | 并发写锁、大数据量性能差、功能不足 |

### 运行环境

- 本地服务器单机部署
- 数据库运行在本机，不需要分布式

---

## 3. 数据同步方案

### 3.1 核心原则

**以本地数据为主，云端数据做补充。**

本地 PostgreSQL 为主库（完整字段 + 分析数据），云端 SQLite 为轻量原始索引（仅保留基础字段）。

### 3.2 同步架构

```
┌──────────────────────────────────────────────────┐
│                  本地服务器                        │
│                                                  │
│  本地 Crawl（定时）   云端同步（定时）              │
│       │                   │                      │
│       ▼                   ▼                      │
│  ┌─────────────────────────────────────────┐     │
│  │         PostgreSQL (主库)                │     │
│  │         news_articles                    │     │
│  │                                          │     │
│  │  sync_status:                            │     │
│  │    'local' — 本地爬取写入                │     │
│  │    'cloud' — 云端同步补充                │     │
│  └─────────────────────────────────────────┘     │
│                                                  │
└──────────────────────────────────────────────────┘
         ▲                          │
         │         ┌────────────────▼──────────────┐
         │         │        S3 (七牛 Kodo)          │
         └─────────┤     news/YYYY-MM-DD.db         │
    云端下行同步   └────────────────────────────────┘
```

### 3.3 同步逻辑

**本地实时爬取 → PostgreSQL**：
1. 本地 Crawl 模块持续运行（类似 GitHub Action 上的逻辑，但本地执行）
2. 抓取结果 UPSERT 写入 `news_articles`
3. `sync_status = 'local'`

**云端 S3 下行同步 → PostgreSQL**：
1. 定时从 S3 下载当日前几天的 `news/{date}.db`
2. 遍历每条记录，按 `(source_id, url)` 或 `(source_id, guid)` 匹配
3. 已存在 → 跳过（本地数据优先）
4. 不存在 → INSERT，`sync_status = 'cloud'`

**为什么不需要"日终合并"**：
- 单表设计，不区分"历史表"和"临时表"
- 云端同步是纯粹的"补充缺失"操作
- 去重逻辑通过唯一索引自动处理，不需要额外的合并流程

### 3.4 原方案与最终方案对比

| 对比维度 | 原方案（双表） | 最终方案（单表） |
|---------|-------------|----------------|
| 表结构 | 历史表 + 当日临时表 | 单表 + sync_status 标记 |
| 跨表查询 | 每次 UNION | 直接查询 |
| 去重 | 跨表去重，逻辑复杂 | 唯一索引自动处理 |
| 云端同步 | 只写历史表 | 统一 UPSERT |
| 日终处理 | 需要合并流程 | 无需合并 |

---

## 4. 数据库表设计

### 4.1 主表：`news_articles`

```sql
CREATE TABLE news_articles (
    -- 主键
    id              BIGSERIAL PRIMARY KEY,

    -- 来源信息
    source_id       VARCHAR(100) NOT NULL,
    source_name     VARCHAR(200) NOT NULL,
    source_type     VARCHAR(10)  NOT NULL CHECK (source_type IN ('hotlist', 'rss')),
    tier            SMALLINT     NOT NULL DEFAULT 4 CHECK (tier BETWEEN 1 AND 4),
    priority        SMALLINT     NOT NULL DEFAULT 0,

    -- 链接
    url             TEXT DEFAULT '',
    mobile_url      TEXT DEFAULT '',
    guid            TEXT DEFAULT '',

    -- 内容
    title           TEXT NOT NULL,
    summary         TEXT DEFAULT '',
    content         TEXT DEFAULT '',   -- Markdown 格式，T1/T2 级别抓取全文

    -- 细粒度拆解（本地独有，云端 SQLite 不存）
    tags            TEXT[] DEFAULT '{}',
    keywords        JSONB DEFAULT '[]',   -- [{"word":"...","weight":0.9,"group":"..."}]
    entities        JSONB DEFAULT '{}',   -- {"companies":["..."],"people":["..."],"places":["..."]}

    -- 评分
    heat_score      INTEGER DEFAULT NULL CHECK (heat_score BETWEEN 0 AND 100),
    sentiment_score INTEGER DEFAULT NULL CHECK (sentiment_score BETWEEN 0 AND 100),
    confidence      INTEGER DEFAULT NULL CHECK (confidence BETWEEN 0 AND 100),

    -- 分类
    category        VARCHAR(50) DEFAULT NULL,
    rank            SMALLINT DEFAULT NULL,
    ranks           SMALLINT[] DEFAULT '{}',

    -- 状态
    sync_status     VARCHAR(10) NOT NULL DEFAULT 'local' CHECK (sync_status IN ('local', 'cloud')),
    is_analyzed     BOOLEAN NOT NULL DEFAULT FALSE,
    notified        BOOLEAN NOT NULL DEFAULT FALSE,

    -- 时间
    published_at     TIMESTAMPTZ DEFAULT NULL,
    first_crawled_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_crawled_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

### 4.2 关键索引

```sql
-- 去重索引（继承现有 SQLite 逻辑）
CREATE UNIQUE INDEX idx_dedup_hotlist
    ON news_articles (source_id, url)
    WHERE source_type = 'hotlist' AND url != '';

CREATE UNIQUE INDEX idx_dedup_rss
    ON news_articles (source_id, guid)
    WHERE source_type = 'rss' AND guid != '';

-- 查询索引
CREATE INDEX idx_published_at   ON news_articles (published_at DESC);
CREATE INDEX idx_tier_priority  ON news_articles (tier, priority DESC);
CREATE INDEX idx_heat_score     ON news_articles (heat_score DESC);
CREATE INDEX idx_category       ON news_articles (category);
CREATE INDEX idx_sync_status    ON news_articles (sync_status);
CREATE INDEX idx_is_analyzed    ON news_articles (is_analyzed);

-- GIN 索引（数组和全文搜索）
CREATE INDEX idx_tags_gin     ON news_articles USING GIN (tags);
CREATE INDEX idx_keywords_gin ON news_articles USING GIN (keywords);
CREATE INDEX idx_entities_gin ON news_articles USING GIN (entities);

-- 全文搜索索引
CREATE INDEX idx_fulltext ON news_articles
    USING GIN (to_tsvector('simple', title || ' ' || COALESCE(summary, '') || ' ' || COALESCE(content, '')));
```

### 4.3 图片表：`news_images`

图片存储使用 MinIO（本地部署的 S3 兼容对象存储），数据库只存 URL。

```sql
CREATE TABLE news_images (
    id           BIGSERIAL PRIMARY KEY,
    article_id   BIGINT NOT NULL REFERENCES news_articles(id) ON DELETE CASCADE,
    image_url    TEXT NOT NULL,           -- MinIO / 云 S3 URL
    original_url TEXT DEFAULT '',         -- 原始图片 URL
    width        INTEGER DEFAULT NULL,
    height       INTEGER DEFAULT NULL,
    file_size    INTEGER DEFAULT NULL,    -- bytes
    sort_order   SMALLINT DEFAULT 0,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_images_article ON news_images (article_id);
```

### 4.4 股票相关表（后续阶段）

```sql
-- 股票基础信息
CREATE TABLE stocks (
    id       BIGSERIAL PRIMARY KEY,
    code     VARCHAR(20)  NOT NULL UNIQUE,   -- 如 '600519'
    name     VARCHAR(100) NOT NULL,          -- 如 '贵州茅台'
    exchange VARCHAR(20)  DEFAULT NULL,      -- 'SH' / 'SZ' / 'HK' / 'NASDAQ'
    sector   VARCHAR(50)  DEFAULT NULL,
    industry VARCHAR(100) DEFAULT NULL
);

-- 日线数据
CREATE TABLE stock_daily_prices (
    id         BIGSERIAL PRIMARY KEY,
    stock_id   BIGINT NOT NULL REFERENCES stocks(id) ON DELETE CASCADE,
    trade_date DATE NOT NULL,
    open       NUMERIC(12, 4),
    high       NUMERIC(12, 4),
    low        NUMERIC(12, 4),
    close      NUMERIC(12, 4),
    volume     BIGINT,
    UNIQUE (stock_id, trade_date)
);

-- 基本面数据
CREATE TABLE stock_financials (
    id          BIGSERIAL PRIMARY KEY,
    stock_id    BIGINT NOT NULL REFERENCES stocks(id) ON DELETE CASCADE,
    report_date DATE NOT NULL,
    report_type VARCHAR(10) DEFAULT 'annual',  -- 'annual' / 'quarterly'
    revenue     NUMERIC(20, 2),
    net_income  NUMERIC(20, 2),
    eps         NUMERIC(10, 4),
    roe         NUMERIC(10, 4),
    total_assets NUMERIC(20, 2),
    UNIQUE (stock_id, report_date, report_type)
);
```

---

## 5. 云端 SQLite（不做改动）

云端 `schema.sql` 保持现有结构，**不添加全文、不添加分析字段**。

### 理由

- GitHub Action 运行时限制（10 分钟 timeout），抓取全文会超时
- S3 传输成本 — 加全文后 db 文件体积膨胀 10-50 倍
- 云端职责单一：一个轻量级的原始新闻索引备份
- 全文抓取和分析在本地进行，没有时间限制

### 云端字段对照

云端 SQLite 的 `news_items` 表字段 → 本地 PostgreSQL 的 `news_articles` 表字段：

| 云端 SQLite | 本地 PostgreSQL | 备注 |
|------------|----------------|------|
| `title`, `source_id`, `source_name`, `source_type` | 同名字段 | 直接映射 |
| `tier`, `priority` | 同名字段 | 直接映射 |
| `url`, `mobile_url`, `guid` | 同名字段 | 直接映射 |
| `rank` | `rank` | 直接映射 |
| `summary` | `summary` | 直接映射 |
| `published_at`, `author` | `published_at` | 直接映射 |
| `first_crawl_time`, `last_crawl_time` | `first_crawled_at`, `last_crawled_at` | 直接映射 |
| — | `content` | **仅本地** |
| — | `tags`, `keywords`, `entities` | **仅本地** |
| — | `heat_score`, `sentiment_score`, `confidence` | **仅本地** |
| — | `category`, `is_analyzed` | **仅本地** |
| — | `sync_status` | **仅本地，同步时写入** |

---

## 6. 关键字段说明

### 6.1 `content` — Markdown 格式

T1/T2 级别新闻抓取全文，以 Markdown 格式存储。Markdown 保留了原始排版结构（标题层级、加粗、列表、链接等），在前端渲染为 HTML 阅读时格式整洁，不会出现纯文本格式混乱的问题。

### 6.2 `sentiment_score` — 情感评分（0-100）

| 范围 | 含义 |
|------|------|
| 0-33 | 负面 |
| 34-66 | 中性 |
| 67-100 | 正面 |

来源可以是 AI 自动分析，也可以由用户在前端手动评估。前端根据 score 实时计算标签，不在数据库存储字符串标签，避免数据不一致。

### 6.3 `confidence` — 置信度/质量评分（0-100）

综合反映内容质量和分析结果的可靠性。来源可以是 AI 自动评估（根据实体歧义、情感一致性等维度打分），也可以由用户手动评分。用户阅读后如果觉得内容质量极差（如标题党、虚假信息），可以在前端直接打 0 分，后续查询默认过滤低质量内容：

```sql
WHERE confidence IS NULL OR confidence >= 20
```

### 6.4 `heat_score` — 热力值（0-100）

综合排名变化、出现频率、来源权威度等因素计算的综合热度评分。

---

## 7. 图片存储流程

```
本地 Crawl 抓取原文
    │
    ▼
提取正文中所有图片 URL
    │
    ▼
下载图片 → 上传至 MinIO → 获得 MinIO URL
    │
    ▼
INSERT INTO news_images (article_id, image_url, original_url, ...)
```

MinIO 使用 S3 兼容 API，后续如需切换到云 S3（七牛 Kodo、AWS S3 等），只需修改 endpoint 配置，无需改动代码逻辑。

---

## 8. 待确认 / 后续扩展

- AI 分析流程（关键词提取、实体识别、情感分析）的具体实现
- 用户手动评估（confidence / sentiment_score）的前端交互设计
- 数据清理策略：以年为单位的归档/删除机制
- 快讯类内容：暂不纳入本表，后续如需要可单独建模
