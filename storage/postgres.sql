-- NewsRadar PostgreSQL schema
-- News articles table
CREATE TABLE IF NOT EXISTS news_articles (
    id              BIGSERIAL PRIMARY KEY,
    source_id       VARCHAR(100) NOT NULL,
    source_name     VARCHAR(200) NOT NULL,
    source_type     VARCHAR(10)  NOT NULL CHECK (source_type IN ('hotlist', 'rss', 'manual')),
    tier            SMALLINT     NOT NULL DEFAULT 4 CHECK (tier BETWEEN 1 AND 4),
    priority        SMALLINT     NOT NULL DEFAULT 0,
    url             TEXT DEFAULT '',
    mobile_url      TEXT DEFAULT '',
    guid            TEXT DEFAULT '',
    title           TEXT NOT NULL,
    summary         TEXT DEFAULT '',
    content         TEXT DEFAULT '',
    author          TEXT DEFAULT '',
    tags            TEXT[] DEFAULT '{}',
    keywords        JSONB DEFAULT '[]',
    entities        JSONB DEFAULT '{}',
    heat_score      INTEGER DEFAULT NULL CHECK (heat_score BETWEEN 0 AND 100),
    sentiment_score INTEGER DEFAULT NULL CHECK (sentiment_score BETWEEN 0 AND 100),
    confidence      INTEGER DEFAULT NULL CHECK (confidence BETWEEN 0 AND 100),
    category        VARCHAR(50) DEFAULT NULL,
    rank            SMALLINT DEFAULT NULL,
    ranks           JSONB DEFAULT '[]',
    crawled_from    VARCHAR(10) NOT NULL DEFAULT 'local' CHECK (crawled_from IN ('local', 'cloud')),
    is_analyzed     BOOLEAN NOT NULL DEFAULT FALSE,
    published_at     TIMESTAMPTZ DEFAULT NULL,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- News images table
CREATE TABLE IF NOT EXISTS news_images (
    id           BIGSERIAL PRIMARY KEY,
    article_id   BIGINT NOT NULL REFERENCES news_articles(id) ON DELETE CASCADE,
    image_url    TEXT NOT NULL,
    original_url TEXT DEFAULT '',
    width        INTEGER DEFAULT NULL,
    height       INTEGER DEFAULT NULL,
    file_size    INTEGER DEFAULT NULL,
    sort_order   SMALLINT DEFAULT 0,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Dedup indexes (partial unique, matching SQLite logic)
CREATE UNIQUE INDEX IF NOT EXISTS idx_dedup_hotlist
    ON news_articles (url)
    WHERE source_type = 'hotlist' AND url != '';

CREATE UNIQUE INDEX IF NOT EXISTS idx_dedup_rss
    ON news_articles (source_id, guid)
    WHERE source_type = 'rss' AND guid != '';

CREATE UNIQUE INDEX IF NOT EXISTS idx_dedup_manual
    ON news_articles (source_id, url)
    WHERE source_type = 'manual' AND url != '';

-- Query indexes
CREATE INDEX IF NOT EXISTS idx_published_at   ON news_articles (published_at DESC);
CREATE INDEX IF NOT EXISTS idx_tier_priority  ON news_articles (tier, priority DESC);
CREATE INDEX IF NOT EXISTS idx_heat_score     ON news_articles (heat_score DESC);
CREATE INDEX IF NOT EXISTS idx_category       ON news_articles (category);
CREATE INDEX IF NOT EXISTS idx_crawled_from   ON news_articles (crawled_from);
CREATE INDEX IF NOT EXISTS idx_is_analyzed    ON news_articles (is_analyzed);

-- GIN indexes
CREATE INDEX IF NOT EXISTS idx_tags_gin     ON news_articles USING GIN (tags);
CREATE INDEX IF NOT EXISTS idx_keywords_gin ON news_articles USING GIN (keywords);
CREATE INDEX IF NOT EXISTS idx_entities_gin ON news_articles USING GIN (entities);

-- Full-text search index for English/Latin text (token-based)
CREATE INDEX IF NOT EXISTS idx_fulltext ON news_articles
    USING GIN (to_tsvector('simple', title || ' ' || COALESCE(summary, '') || ' ' || COALESCE(content, '')));

-- pg_trgm extension for CJK ILIKE acceleration
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- Trigram GIN index for Chinese/Japanese/Korean ILIKE search
CREATE INDEX IF NOT EXISTS idx_fulltext_trgm ON news_articles
    USING GIN ((title || ' ' || COALESCE(summary, '') || ' ' || COALESCE(content, '')) gin_trgm_ops);

-- Images index
CREATE INDEX IF NOT EXISTS idx_images_article ON news_images (article_id);
