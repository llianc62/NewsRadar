# Storage — 双存储架构

NewsRadar 使用两种存储后端，分别服务于不同的运行模式。两者共享相同的 fetch 逻辑。

## 架构总览

```
storage/
├── postgres.py   # PostgreSQL 后端 — daemon 使用（~1272 行）
├── postgres.sql  # DDL schema
├── sqlite.py     # SQLite 后端 — Cloud CI 使用（~208 行）
├── sqlite.sql    # DDL schema
├── s3.py         # S3/MinIO 客户端（~301 行）
├── files.py      # FileStorage ABC — 本地 + S3 实现（~126 行）
└── __init__.py
```

## PostgreSQL（Canonical Store）

本地 daemon 的主存储。特性：

- **连接池**：`ThreadedConnectionPool`（psycopg2）
- **批量 UPSERT**：`execute_values` + `ON CONFLICT ... DO UPDATE`
- **去重**：hotlist 按 `source_id + url`，RSS 按 `source_id + guid`
- **全文搜索**：GIN 索引 + `pg_bigm`/`pg_trgm` 扩展，支持 CJK 的 `to_tsvector('simple', ...)` 和 `ILIKE` 模糊搜索
- **两种 UPSERT 模板**：`_UPDATE_SET` 保留已有 content，`_UPDATE_SET_OVERWRITE` 替换（manual/articles 需要完整刷新）
- **Schema 迁移**：`_run_migrations()` 幂等迁移（4 个 migration），在每次 `init_schema()` 时执行

### 迁移历史

| # | 内容 |
|---|------|
| 001 | 重建 `idx_fulltext` GIN 索引，加入 `content` 列 |
| 002 | 创建 `pg_trgm` 扩展 + `idx_fulltext_trgm` CJK 模糊搜索 |
| 003 | 创建 `failed_tasks` 表（失败重试） |
| 004 | `ranks` 列从 `SMALLINT[]` 改为 `JSONB`（热度评分历史） |

## SQLite + S3（Cloud CI Backend）

GitHub Actions 中的轻量级存储：

- **按日分库**：每天一个 `.db` 文件 → 上传 S3
- **去重**：`INSERT OR IGNORE`，每条每天只写一次
- **Notifier 下载**：notifier workflow 下载当日 DB → 关键词匹配 → 邮件

### 与 PG 的关键差异

| | PostgreSQL | SQLite |
|---|---|---|
| 热度计算 | 完整（增量 + 云端快照） | 仅百分位快照 |
| `ranks` 字段 | JSONB，完整历史轨迹 | 无 |
| 内容正文 | 有（daemon 下载） | 默认无（CI 不下载正文） |
| 搜索 | 全文搜索 + 模糊 | 简单 LIKE |

## 云端同步（Cloud Sync）

`Crawler.sync_from_cloud()` — daemon 启动/定时执行：

1. 从 S3 下载每日 SQLite DB 文件
2. 筛选 `crawled_at` 晚于 PG 最新云同步时间的行
3. 补下载正文 + 图片（`enrich_content`）
4. UPSERT 到 PG（`crawled_from="cloud"`）

云端快照的 heat_score 为百分位值（`total=0`，被 `_process_hotlist_heat` 跳过，原值保留）。后续 daemon 自己抓取时增量算法接管。

## FileStorage（图片/文件存储）

`storage/files.py` — `FileStorage` ABC，两个实现：

- **LocalStorage**：本地文件系统
- **S3Storage**：MinIO/S3 兼容对象存储

接口：`save(path, data)` / `get(path) → bytes | None`。由 `ImageProcessor` 使用。

S3 配置有两套（均在 `config.yaml` 的 `storage` 段）：

| 用途 | Env 前缀 | 说明 |
|------|----------|------|
| Cloud | `CLOUD_S3_*` | SQLite DB 文件传输 |
| Resource | `RESOURCE_S3_*` | 文章图片 + 项目文件 |

## 关键文件

| 文件 | 用途 |
|------|------|
| `storage/postgres.py` | PG 连接池、UPSERT、全文搜索、迁移 |
| `storage/postgres.sql` | DDL schema 定义 |
| `storage/sqlite.py` | SQLite 持久化 |
| `storage/sqlite.sql` | SQLite DDL |
| `storage/s3.py` | S3/MinIO 客户端封装 |
| `storage/files.py` | FileStorage ABC + Local/S3 实现 |
| `config/loader.py` | `_load_storage_config()` — S3 配置两套 |
