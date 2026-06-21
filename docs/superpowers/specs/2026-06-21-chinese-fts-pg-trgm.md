# 中文全文搜索修复：pg_trgm 方案

## 问题

PostgreSQL 默认文本解析器将连续中文字符当作**单个 token**，"英伟达" 被嵌入更大的中文 token 中时（如 `"英伟达用财务担保..."`），FTS 查询 `plainto_tsquery('英伟达')` 永远匹配不上。

## 方案

引入 `pg_trgm` 扩展，对中文搜索走 ILIKE + trigram GIN 索引，英文搜索保持 FTS。

| 搜索类型 | 判断条件 | 查询方式 | 索引 |
|---------|---------|---------|------|
| 中文 | 搜索词含 CJK 字符 | `ILIKE '%kw%'` | GIN `gin_trgm_ops` |
| 英文 | 搜索词不含 CJK | `to_tsvector @@ plainto_tsquery` | GIN `to_tsvector`（现有） |

## 修改文件

### 1. `storage/postgres.sql`

新增：

```sql
-- pg_trgm 扩展
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- trigram GIN 索引（加速 ILIKE，含前后通配符）
CREATE INDEX IF NOT EXISTS idx_fulltext_trgm ON news_articles
    USING GIN ((title || ' ' || COALESCE(summary, '') || ' ' || COALESCE(content, '')) gin_trgm_ops);
```

### 2. `storage/postgres.py`

**新增 CJK 检测函数**（模块级工具函数）：

```python
import re

_CJK_RE = re.compile(r'[一-鿿㐀-䶿豈-﫿]')

def _contains_cjk(text: str) -> bool:
    """Return True if text contains any CJK character."""
    return bool(_CJK_RE.search(text))
```

**_run_migrations 追加**（migration 002）：

```python
# Migration 002: install pg_trgm extension + trigram index
cur.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
cur.execute(
    """CREATE INDEX IF NOT EXISTS idx_fulltext_trgm ON news_articles
       USING GIN ((title || ' ' || COALESCE(summary, '')
       || ' ' || COALESCE(content, '')) gin_trgm_ops)"""
)
```

**6 个查询方法各改一处**（`get_recent_news` / `get_news_count` / `get_sentiment_counts` / `get_keyword_counts` / `get_high_impact_count` / `search_articles`）：

将现有的：

```python
if search is not None:
    conditions.append(
        "to_tsvector('simple', title || ' ' || COALESCE(summary, '')"
        " || ' ' || COALESCE(content, ''))"
        " @@ plainto_tsquery('simple', %s)"
    )
    params.append(search)
```

改为：

```python
if search is not None:
    if _contains_cjk(search):
        conditions.append(
            "(title || ' ' || COALESCE(summary, '')"
            " || ' ' || COALESCE(content, '')) ILIKE %s"
        )
        params.append(f"%{search}%")
    else:
        conditions.append(
            "to_tsvector('simple', title || ' ' || COALESCE(summary, '')"
            " || ' ' || COALESCE(content, ''))"
            " @@ plainto_tsquery('simple', %s)"
        )
        params.append(search)
```

## 不修改的文件

| 文件 | 原因 |
|------|------|
| `web/app.py` | 查询接口不变 |
| `web/templates/` | 前端无变化 |
| `news/crawler.py` | 与搜索无关 |
| `config/` | 无新增配置 |

## 影响评估

| 维度 | 说明 |
|------|------|
| 兼容性 | `pg_trgm` 是 PostgreSQL 内置扩展，PostgreSQL 9.1+ 自带 |
| 索引大小 | 比纯 FTS GIN 索引大约 2-3x（trigram 数量多于 token 数量） |
| 查询性能 | 中文 ILIKE：trigram 索引过滤 → recheck，与 FTS 同数量级 |
| 数据迁移 | 无需重写现有数据，索引创建后自动生效 |
| 搜索行为 | 中文：子串匹配（`ILIKE`）；英文：词级匹配（FTS 不变） |

## 后续展望

如果后续安装 `zhparser` 扩展或引入 jieba 分词（`docs/superpowers/plans/2026-06-20-jieba-keyword-tags.md`），中文搜索可升级为真正的分词 FTS。`pg_trgm` 方案是当前最轻量的过渡方案。
