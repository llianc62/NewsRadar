-- NewsNow Crawler — single-table schema

CREATE TABLE IF NOT EXISTS news_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_name TEXT NOT NULL,
    source_type TEXT NOT NULL CHECK(source_type IN ('hotlist', 'rss')),
    tier INTEGER NOT NULL DEFAULT 4,
    priority INTEGER NOT NULL DEFAULT 0,
    url TEXT DEFAULT '',
    mobile_url TEXT DEFAULT '',
    rank INTEGER,
    guid TEXT,
    published_at TEXT,
    summary TEXT,
    author TEXT,
    category TEXT DEFAULT '',
    tags TEXT DEFAULT '',  -- JSON array string, e.g. '["tag1","tag2"]'
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Dedup: hot-list by (source_id, url)
CREATE UNIQUE INDEX IF NOT EXISTS idx_dedup_hotlist
    ON news_items(source_id, url) WHERE source_type = 'hotlist' AND url != '';

-- Dedup: RSS by (source_id, guid)
CREATE UNIQUE INDEX IF NOT EXISTS idx_dedup_rss
    ON news_items(source_id, guid) WHERE source_type = 'rss' AND guid != '';

-- Query indexes
CREATE INDEX IF NOT EXISTS idx_tier_priority ON news_items(tier, priority DESC);

CREATE INDEX IF NOT EXISTS idx_source_type ON news_items(source_type);
