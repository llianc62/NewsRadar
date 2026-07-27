# coding=utf-8
"""PostgreSQL database layer — connection pool, schema init, CRUD.

Wraps ``psycopg2`` ``ThreadedConnectionPool`` inside a ``PostgreSQL``
class (no more module-level global ``_pool``).
"""

import json
import os
import re
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

import psycopg2
import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool

from agent.data import AgentDefinition, AgentKnowledge

# Detect CJK characters for search routing:
#   CJK search  → ILIKE + pg_trgm GIN index
#   ASCII search → FTS (to_tsvector @@ plainto_tsquery)
_CJK_RE = re.compile(r"[一-鿿㐀-䶿豈-﫿]")


def _contains_cjk(text: str) -> bool:
    """Return True if *text* contains any CJK character."""
    return bool(_CJK_RE.search(text))


# 知识库 embedding 维度（pgvector vector 列长度，建表时固化）。
# 从 env KNOWLEDGE_EMBEDDING_DIM 读（与 config 同源），默认对齐
# OpenAI text-embedding-3-small（1536）。切换维度需 DROP 重建表。
KNOWLEDGE_EMBEDDING_DIM = int(os.environ.get("KNOWLEDGE_EMBEDDING_DIM", "1536"))


def _vec_to_str(vec: list[float]) -> str:
    """将浮点向量序列化为 pgvector 字面量 ``'[v1,v2,...]'``。

    psycopg2 原生不识别 pgvector 类型，零依赖方案：转成字符串后用
    ``::vector`` 强制转换（见 ingest/search_knowledge）。
    """
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"

# Register JSONB adapter
psycopg2.extras.register_default_jsonb(loads=json.loads)

# Module-level timezone default (used for HH:MM date parsing)
_timezone_offset: str = "+08:00"

# ═══════════════════════════════════════════════════════════════════
# Batch UPSERT SQL templates (used by save_news_data)
# ═══════════════════════════════════════════════════════════════════

_COLUMNS = """title, source_id, source_name, source_type,
        tier, priority, url, mobile_url, rank,
        guid, published_at, crawled_at, summary, author,
        content, category, tags,
        crawled_from,
        ranks, heat_score,
        sentiment_score"""

_INSERT_PREFIX = f"INSERT INTO news_articles ({_COLUMNS}) VALUES %s"

_UPDATE_SET = """title = EXCLUDED.title,
        rank = EXCLUDED.rank,
        mobile_url = EXCLUDED.mobile_url,
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

_UPDATE_SET_OVERWRITE = """title = EXCLUDED.title,
        rank = EXCLUDED.rank,
        mobile_url = EXCLUDED.mobile_url,
        updated_at = NOW(),
        priority = EXCLUDED.priority,
        tier = EXCLUDED.tier,
        summary = EXCLUDED.summary,
        category = EXCLUDED.category,
        tags = EXCLUDED.tags,
        content = EXCLUDED.content"""

_HOTLIST_INSERT_SQL = f"""{_INSERT_PREFIX}
ON CONFLICT (url)
WHERE source_type = 'hotlist' AND url != ''
DO NOTHING"""

_RSS_INSERT_SQL = f"""{_INSERT_PREFIX}
ON CONFLICT (source_id, guid)
WHERE source_type = 'rss' AND guid != ''
DO UPDATE SET {_UPDATE_SET}"""

_RSS_INSERT_SKIP_SQL = f"""{_INSERT_PREFIX}
ON CONFLICT (source_id, guid)
WHERE source_type = 'rss' AND guid != ''
DO NOTHING"""

_MANUAL_INSERT_SQL = f"""{_INSERT_PREFIX}
ON CONFLICT (source_id, url)
WHERE source_type = 'manual' AND url != ''
DO UPDATE SET {_UPDATE_SET_OVERWRITE}"""

_MANUAL_INSERT_SKIP_SQL = f"""{_INSERT_PREFIX}
ON CONFLICT (source_id, url)
WHERE source_type = 'manual' AND url != ''
DO NOTHING"""

_FALLBACK_INSERT_SQL = f"{_INSERT_PREFIX}"


def _load_schema() -> str:
    """Read the PostgreSQL schema DDL from schema_postgres.sql."""
    schema_path = Path(__file__).parent / "postgres.sql"
    if not schema_path.exists():
        raise FileNotFoundError(
            f"PostgreSQL schema file not found: {schema_path}"
        )
    return schema_path.read_text(encoding="utf-8")


def _to_timestamptz(value: str, fallback_date: Optional[str]) -> Optional[datetime]:
    """Convert a time string (HH:MM or ISO 8601) to a datetime."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        pass
    if ":" in value and len(value.split(":")[0]) <= 2 and fallback_date:
        try:
            return datetime.fromisoformat(
                f"{fallback_date}T{value}:00{_timezone_offset}"
            )
        except (ValueError, TypeError):
            pass
    return None


class PostgreSQL:
    """PostgreSQL connection pool and CRUD operations.

    Usage::

        db = PostgreSQL({"host": "localhost", "port": 5432, ...})
        db.connect()
        db.init_schema()
        db.save_news_data(news_data, source_tiers)
        articles = db.get_recent_news(limit=10)
        db.close()
    """

    def __init__(self, pg_config: Dict[str, Any]):
        self._config = pg_config
        self._pool: Optional[ThreadedConnectionPool] = None

    # ── Lifecycle ──────────────────────────────────────────────────

    def connect(self) -> None:
        """Create the connection pool."""
        if self._pool is not None:
            return
        self._pool = ThreadedConnectionPool(
            minconn=self._config.get("min_connections", 2),
            maxconn=self._config.get("max_connections", 10),
            host=self._config["host"],
            port=self._config["port"],
            dbname=self._config["database"],
            user=self._config["user"],
            password=self._config["password"],
            options="-c timezone=Asia/Shanghai",
        )

    def close(self) -> None:
        """Close the connection pool."""
        if self._pool is not None:
            self._pool.closeall()
            self._pool = None
            print("[DB] Connection pool closed")

    def init_schema(self) -> None:
        """Run schema DDL if tables do not yet exist, then apply migrations."""
        if not self._schema_ready():
            conn = self._pool.getconn()
            try:
                with conn.cursor() as cur:
                    cur.execute(_load_schema())
                conn.commit()
                print("[DB] Schema initialized successfully")
            finally:
                self._pool.putconn(conn)
        else:
            print("[DB] Schema already exists — running migrations.")

        # Always run migrations (idempotent)
        self._run_migrations()
        self._init_agent_schema()

    def _run_migrations(self) -> None:
        """Idempotent schema migrations."""
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                # Migration 001: rebuild full-text index to include content
                # (previous index may have been dropped or never included content)
                cur.execute(
                    """SELECT EXISTS (
                        SELECT 1 FROM pg_indexes
                        WHERE indexname = 'idx_fulltext'
                          AND indexdef LIKE '%COALESCE(content%'
                    )"""
                )
                has_content_in_index = cur.fetchone()[0]
                if not has_content_in_index:
                    print("[DB] Migrating: rebuilding idx_fulltext to include content...")
                    cur.execute("DROP INDEX IF EXISTS idx_fulltext")
                    cur.execute(
                        """CREATE INDEX idx_fulltext ON news_articles
                           USING GIN (to_tsvector('simple',
                               title || ' ' || COALESCE(summary, '') || ' '
                               || COALESCE(content, '')))"""
                    )
                    conn.commit()
                    print("[DB] Migration complete: idx_fulltext rebuilt with content.")

                # Migration 002: create pg_trgm extension + trigram index for CJK search
                cur.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
                cur.execute(
                    """SELECT EXISTS (
                        SELECT 1 FROM pg_indexes
                        WHERE indexname = 'idx_fulltext_trgm'
                    )"""
                )
                has_trgm_index = cur.fetchone()[0]
                if not has_trgm_index:
                    print("[DB] Migrating: creating idx_fulltext_trgm for CJK ILIKE search...")
                    cur.execute(
                        """CREATE INDEX idx_fulltext_trgm ON news_articles
                           USING GIN ((title || ' ' || COALESCE(summary, '')
                           || ' ' || COALESCE(content, '')) gin_trgm_ops)"""
                    )
                    conn.commit()
                    print("[DB] Migration complete: idx_fulltext_trgm created.")

                # Migration 003: create failed_tasks table for failure recording & lazy retry
                cur.execute(
                    """SELECT EXISTS (
                        SELECT 1 FROM information_schema.tables
                        WHERE table_schema = 'public'
                          AND table_name = 'failed_tasks'
                    )"""
                )
                has_failed_tasks = cur.fetchone()[0]
                if not has_failed_tasks:
                    print("[DB] Migrating: creating failed_tasks table...")
                    cur.execute(
                        """CREATE TABLE IF NOT EXISTS failed_tasks (
                            id              BIGSERIAL PRIMARY KEY,
                            task_type       VARCHAR(50) NOT NULL,
                            context         JSONB NOT NULL DEFAULT '{}',
                            retry_times     INTEGER NOT NULL DEFAULT 0,
                            max_retry       INTEGER NOT NULL DEFAULT 3,
                            last_retry      TIMESTAMPTZ DEFAULT NULL,
                            status          VARCHAR(20) NOT NULL DEFAULT 'pending'
                                                CHECK (status IN ('pending', 'failed', 'completed')),
                            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                            updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
                        )"""
                    )
                    cur.execute(
                        """CREATE UNIQUE INDEX IF NOT EXISTS idx_failed_tasks_dedup
                           ON failed_tasks (task_type, (context->>'url'))
                           WHERE status = 'pending'"""
                    )
                    cur.execute(
                        """CREATE INDEX IF NOT EXISTS idx_failed_tasks_status
                           ON failed_tasks (status, task_type, retry_times)"""
                    )
                    conn.commit()
                    print("[DB] Migration complete: failed_tasks table created.")

                # Migration 004: change ranks column from SMALLINT[] to JSONB
                # for heat_score tracking with [rank, total] snapshots.
                cur.execute(
                    """SELECT data_type
                       FROM information_schema.columns
                       WHERE table_schema = 'public'
                         AND table_name = 'news_articles'
                         AND column_name = 'ranks'"""
                )
                col_type = cur.fetchone()
                if col_type and col_type[0] == 'ARRAY':
                    print("[DB] Migrating: changing ranks from SMALLINT[] to JSONB...")
                    cur.execute(
                        """ALTER TABLE news_articles
                           ALTER COLUMN ranks TYPE JSONB USING '[]'::jsonb"""
                    )
                    cur.execute(
                        """ALTER TABLE news_articles
                           ALTER COLUMN ranks SET DEFAULT '[]'::jsonb"""
                    )
                    conn.commit()
                    print("[DB] Migration complete: ranks column converted to JSONB.")

                # Migration 006: change hotlist dedup from (source_id, url)
                # to (url) only, so same article from different source_ids
                # of the same provider is not duplicated.
                cur.execute(
                    """SELECT EXISTS (
                        SELECT 1 FROM pg_indexes
                        WHERE indexname = 'idx_dedup_hotlist'
                          AND indexdef LIKE '%source_id%'
                    )"""
                )
                if cur.fetchone()[0]:
                    print("[DB] Migrating: rebuilding idx_dedup_hotlist on (url) only...")
                    cur.execute("DROP INDEX IF EXISTS idx_dedup_hotlist")
                    cur.execute(
                        """CREATE UNIQUE INDEX idx_dedup_hotlist
                           ON news_articles (url)
                           WHERE source_type = 'hotlist' AND url != ''"""
                    )
                    conn.commit()
                    print("[DB] Migration complete: idx_dedup_hotlist rebuilt on (url).")

                # Migration 007: add crawled_at column for original crawl timestamp
                cur.execute(
                    """SELECT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name = 'news_articles'
                          AND column_name = 'crawled_at'
                    )"""
                )
                if not cur.fetchone()[0]:
                    print("[DB] Migrating: adding crawled_at column...")
                    cur.execute(
                        """ALTER TABLE news_articles
                           ADD COLUMN crawled_at TIMESTAMPTZ DEFAULT NULL"""
                    )
                    conn.commit()
                    print("[DB] Migration complete: crawled_at column added.")

                # Migration 008 (removed): news_images table was unused dead code

                # Migration 009: add agent_id + model_version to agent_messages
                cur.execute(
                    """SELECT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name = 'agent_messages'
                          AND column_name = 'agent_id'
                    )"""
                )
                if not cur.fetchone()[0]:
                    print("[DB] Migrating: adding agent_id + model_version to agent_messages...")
                    cur.execute(
                        """ALTER TABLE agent_messages
                           ADD COLUMN agent_id TEXT NOT NULL DEFAULT '0',
                           ADD COLUMN model_version TEXT NOT NULL DEFAULT ''"""
                    )
                    conn.commit()
                    print("[DB] Migration complete: agent_messages extended with agent_id + model_version.")

        finally:
            self._pool.putconn(conn)

    def _init_agent_schema(self) -> None:
        """初始化 agent 子系统所需的所有表（幂等）。"""
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS agent_sessions (
                        id SERIAL PRIMARY KEY,
                        title TEXT NOT NULL DEFAULT '新会话',
                        message_count INTEGER DEFAULT 0,
                        created_at TIMESTAMPTZ DEFAULT NOW(),
                        updated_at TIMESTAMPTZ DEFAULT NOW()
                    );
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS agent_messages (
                        id SERIAL PRIMARY KEY,
                        session_id INTEGER NOT NULL REFERENCES agent_sessions(id) ON DELETE CASCADE,
                        role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                        agent_id TEXT NOT NULL DEFAULT '0',
                        model_version TEXT NOT NULL DEFAULT '',
                        content TEXT NOT NULL,
                        created_at TIMESTAMPTZ DEFAULT NOW()
                    );
                """)
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_agent_messages_session
                        ON agent_messages(session_id, created_at);
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS agent_memories (
                        id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                        session_id  TEXT NOT NULL,
                        agent_name  TEXT NOT NULL DEFAULT '',
                        memory_type TEXT NOT NULL DEFAULT 'summary',
                        content     TEXT NOT NULL,
                        created_at  TIMESTAMPTZ DEFAULT now(),
                        updated_at  TIMESTAMPTZ DEFAULT now()
                    );
                """)
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_memories_session
                        ON agent_memories (session_id, created_at DESC);
                """)
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_memories_search
                        ON agent_memories USING GIN (to_tsvector('english', content));
                """)
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_memories_search_trgm
                        ON agent_memories USING GIN (content gin_trgm_ops);
                """)
                # ── 知识库（Phase 3，pgvector 语义检索） ────────────────
                # pgvector 扩展（pgvector/pgvector:pg16 镜像预装，trusted，
                # 非 superuser 的库 owner 可直接 CREATE EXTENSION）
                cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
                cur.execute(f"""
                    CREATE TABLE IF NOT EXISTS knowledge_chunks (
                        id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                        source      TEXT NOT NULL,
                        namespace   TEXT NOT NULL DEFAULT '',
                        content     TEXT NOT NULL,
                        embedding   vector({KNOWLEDGE_EMBEDDING_DIM}),
                        metadata    JSONB DEFAULT '{{}}'::jsonb,
                        created_at  TIMESTAMPTZ DEFAULT NOW()
                    );
                """)
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_knowledge_namespace
                        ON knowledge_chunks (namespace);
                """)
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_knowledge_embedding
                        ON knowledge_chunks USING hnsw (embedding vector_cosine_ops);
                """)
                # ── 角色系统（agent_definitions + agent_knowledge） ────────
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS agent_definitions (
                        id           TEXT PRIMARY KEY,
                        name         TEXT NOT NULL,
                        description  TEXT NOT NULL DEFAULT '',
                        system_prompt TEXT NOT NULL,
                        tools        JSONB NOT NULL DEFAULT '[]',
                        knowledge_id TEXT,
                        metadata     JSONB NOT NULL DEFAULT '{}',
                        created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS agent_knowledge (
                        id          TEXT PRIMARY KEY,
                        name        TEXT NOT NULL,
                        description TEXT NOT NULL DEFAULT '',
                        namespace   TEXT NOT NULL UNIQUE,
                        created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                """)
                # agent_sessions 加 agent_id 列（如不存在）
                cur.execute("""
                    DO $$
                    BEGIN
                        IF NOT EXISTS (
                            SELECT 1 FROM information_schema.columns
                            WHERE table_name='agent_sessions' AND column_name='agent_id'
                        ) THEN
                            ALTER TABLE agent_sessions ADD COLUMN agent_id TEXT;
                        END IF;
                    END $$;
                """)
                # ── 新闻源（已废弃，数据来自 config.yaml） ──────────────
            conn.commit()
            print("[DB] Agent schema initialized")
        finally:
            self._pool.putconn(conn)

    def create_agent_session(self, title: str = "新会话") -> int:
        """创建新会话，返回 session_id。"""
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO agent_sessions (title) VALUES (%s) RETURNING id",
                    (title,),
                )
                session_id = cur.fetchone()[0]
            conn.commit()
            return session_id
        finally:
            self._pool.putconn(conn)

    def get_agent_sessions(self, limit: int = 20, offset: int = 0) -> list[dict]:
        """获取会话列表（按 updated_at 倒序）。"""
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT id, title, message_count, created_at, updated_at
                       FROM agent_sessions
                       ORDER BY updated_at DESC
                       LIMIT %s OFFSET %s""",
                    (limit, offset),
                )
                rows = cur.fetchall()
                return [
                    {
                        "id": r[0], "title": r[1], "message_count": r[2],
                        "created_at": r[3].isoformat() if r[3] else None,
                        "updated_at": r[4].isoformat() if r[4] else None,
                    }
                    for r in rows
                ]
        finally:
            self._pool.putconn(conn)

    def delete_agent_session(self, session_id: int) -> bool:
        """删除会话及其消息（CASCADE）。返回是否成功删除。"""
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM agent_sessions WHERE id = %s",
                    (session_id,),
                )
                deleted = cur.rowcount > 0
            conn.commit()
            return deleted
        finally:
            self._pool.putconn(conn)

    def get_agent_messages(self, session_id: int, limit: int = 50) -> list[dict]:
        """获取某会话的消息列表（按时间正序）。"""
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT id, session_id, role, agent_id, model_version, content, created_at
                       FROM agent_messages
                       WHERE session_id = %s
                       ORDER BY created_at ASC
                       LIMIT %s""",
                    (session_id, limit),
                )
                rows = cur.fetchall()
                return [
                    {
                        "id": r[0], "session_id": r[1], "role": r[2],
                        "agent_id": r[3],
                        "model_version": r[4],
                        "content": r[5],
                        "created_at": r[6].isoformat() if r[6] else None,
                    }
                    for r in rows
                ]
        finally:
            self._pool.putconn(conn)

    def save_agent_message(self, session_id: int, role: str, content: str,
                           agent_id: str = "0", model_version: str = "") -> int:
        """保存一条消息，返回 message_id。同时更新会话的 message_count 和 updated_at。

        Args:
            agent_id: 智能体 ID（0 = 默认助手）。
            model_version: 模型版本字符串，如 "deepseek-v4-pro"。
        """
        if role not in ("user", "assistant"):
            raise ValueError(f"role must be 'user' or 'assistant', got {role!r}")
        if not content or not content.strip():
            raise ValueError("content must not be empty")
        if session_id < 1:
            raise ValueError(f"session_id must be positive, got {session_id}")
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO agent_messages (session_id, role, content, agent_id, model_version)
                       VALUES (%s, %s, %s, %s, %s) RETURNING id""",
                    (session_id, role, content, agent_id, model_version),
                )
                msg_id = cur.fetchone()[0]
                cur.execute(
                    """UPDATE agent_sessions
                       SET message_count = message_count + 1,
                           updated_at = NOW()
                       WHERE id = %s""",
                    (session_id,),
                )
                # 如果是首条用户消息，更新标题
                # 使用默认标题作为哨兵，确保即使首条消息是 assistant 也能触发
                if role == "user":
                    cur.execute(
                        """UPDATE agent_sessions
                           SET title = LEFT(%s, 30)
                           WHERE id = %s
                             AND title = '新会话'""",
                        (content, session_id),
                    )
            conn.commit()
            return msg_id
        finally:
            self._pool.putconn(conn)

    # ── Knowledge base (Phase 3, pgvector) ───────────────────────────

    def ingest_knowledge(self, chunks: list[dict]) -> int:
        """批量写入知识切片。

        每个 chunk::

            {"source": str, "namespace": str, "content": str,
             "embedding": list[float], "metadata": dict}

        embedding 以 pgvector 字面量字符串插入（``::vector`` 强制转换）。
        返回插入行数。
        """
        if not chunks:
            return 0
        rows = [
            (
                c["source"],
                c.get("namespace", ""),
                c["content"],
                _vec_to_str(c["embedding"]),
                psycopg2.extras.Json(c.get("metadata") or {}),
            )
            for c in chunks
        ]
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                psycopg2.extras.execute_values(
                    cur,
                    """
                    INSERT INTO knowledge_chunks
                        (source, namespace, content, embedding, metadata)
                    VALUES %s
                    """,
                    rows,
                    template="(%s, %s, %s, %s::vector, %s)",
                )
        return len(rows)

    def search_knowledge(
        self, query_embedding: list[float], namespace: str, top_k: int = 5
    ) -> list[dict]:
        """向量近邻检索（余弦距离），返回 Top-K 切片。

        返回每条 ``{id, source, namespace, content, metadata, similarity}``，
        ``similarity = 1 - 余弦距离``（越大越相关，范围 0~1）。
        """
        q = _vec_to_str(query_embedding)
        with self.get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT id, source, namespace, content, metadata,
                           1 - (embedding <=> %s::vector) AS similarity
                    FROM knowledge_chunks
                    WHERE namespace = %s
                    ORDER BY embedding <=> %s::vector
                    LIMIT %s
                    """,
                    (q, namespace, q, top_k),
                )
                return [dict(r) for r in cur.fetchall()]

    def delete_knowledge(self, namespace: str) -> int:
        """按命名空间清空知识切片，返回删除行数。"""
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM knowledge_chunks WHERE namespace = %s",
                    (namespace,),
                )
                return cur.rowcount

    def count_knowledge(self, namespace: str = "") -> int:
        """统计切片数；namespace 为空则统计全部。"""
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                if namespace:
                    cur.execute(
                        "SELECT COUNT(*) FROM knowledge_chunks WHERE namespace = %s",
                        (namespace,),
                    )
                else:
                    cur.execute("SELECT COUNT(*) FROM knowledge_chunks")
                return cur.fetchone()[0]

    # ── AgentDefinition CRUD ──────────────────────────────────────────

    def create_agent_definition(self, defn: AgentDefinition) -> str:
        """写入角色定义，返回 id。"""
        defn.id = defn.id or str(uuid4())
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO agent_definitions (id, name, description, system_prompt, tools, knowledge_id, metadata) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (defn.id, defn.name, defn.description, defn.system_prompt,
                     json.dumps(defn.tools), defn.knowledge_id, json.dumps(defn.metadata)),
                )
        return defn.id

    def get_agent_definition(self, id: str) -> AgentDefinition | None:
        """按 ID 查询角色定义。"""
        with self.get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT * FROM agent_definitions WHERE id = %s", (id,))
                row = cur.fetchone()
        if not row:
            return None
        row["tools"] = json.loads(row["tools"]) if isinstance(row["tools"], str) else (row["tools"] or [])
        row["metadata"] = json.loads(row["metadata"]) if isinstance(row["metadata"], str) else (row["metadata"] or {})
        return AgentDefinition(**row)

    def list_agent_definitions(self) -> list[AgentDefinition]:
        """列出所有角色定义。"""
        with self.get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT * FROM agent_definitions ORDER BY created_at DESC")
                rows = cur.fetchall()
        result = []
        for row in rows:
            row["tools"] = json.loads(row["tools"]) if isinstance(row["tools"], str) else (row["tools"] or [])
            row["metadata"] = json.loads(row["metadata"]) if isinstance(row["metadata"], str) else (row["metadata"] or {})
            result.append(AgentDefinition(**row))
        return result

    def update_agent_definition(self, defn: AgentDefinition) -> bool:
        """更新角色定义。"""
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE agent_definitions SET name=%s, description=%s, system_prompt=%s, "
                    "tools=%s, knowledge_id=%s, metadata=%s, updated_at=NOW() WHERE id=%s",
                    (defn.name, defn.description, defn.system_prompt,
                     json.dumps(defn.tools), defn.knowledge_id, json.dumps(defn.metadata), defn.id),
                )
                updated = cur.rowcount
        return updated > 0

    def delete_agent_definition(self, id: str) -> bool:
        """删除角色定义。"""
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM agent_definitions WHERE id = %s", (id,))
                deleted = cur.rowcount
        return deleted > 0

    # ── AgentKnowledge CRUD ───────────────────────────────────────────

    def create_agent_knowledge(self, kb: AgentKnowledge) -> str:
        """创建知识库定义，自动生成 namespace。"""
        kb.id = kb.id or str(uuid4())
        kb.namespace = kb.namespace or f"kb_{kb.id}"
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO agent_knowledge (id, name, description, namespace) VALUES (%s, %s, %s, %s)",
                    (kb.id, kb.name, kb.description, kb.namespace),
                )
        return kb.id

    def get_agent_knowledge(self, id: str) -> AgentKnowledge | None:
        """按 ID 查询知识库。"""
        with self.get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT * FROM agent_knowledge WHERE id = %s", (id,))
                row = cur.fetchone()
        return AgentKnowledge(**row) if row else None

    def list_agent_knowledge(self) -> list[AgentKnowledge]:
        """列出所有知识库（含切片计数）。"""
        with self.get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT k.*, COALESCE(c.cnt, 0) AS chunk_count
                    FROM agent_knowledge k
                    LEFT JOIN (SELECT namespace, COUNT(*) AS cnt FROM knowledge_chunks GROUP BY namespace) c
                        ON k.namespace = c.namespace
                    ORDER BY k.created_at DESC
                """)
                rows = cur.fetchall()
        result = []
        for row in rows:
            chunk_count = row.pop("chunk_count", 0)
            kb = AgentKnowledge(**row)
            kb._chunk_count = chunk_count
            result.append(kb)
        return result

    def delete_agent_knowledge(self, id: str) -> bool:
        """删除知识库（同时清理切片）。"""
        kb = self.get_agent_knowledge(id)
        if not kb:
            return False
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM knowledge_chunks WHERE namespace = %s", (kb.namespace,))
                cur.execute("DELETE FROM agent_knowledge WHERE id = %s", (id,))
        return True

    # ── Agent sessions by agent_id ────────────────────────────────────

    def get_agent_sessions_by_agent(self, agent_id: str, limit: int = 20, offset: int = 0) -> list[dict]:
        """按 agent_id 获取会话列表（按 updated_at 倒序）。"""
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT id, title, message_count, created_at, updated_at
                       FROM agent_sessions
                       WHERE agent_id = %s
                       ORDER BY updated_at DESC
                       LIMIT %s OFFSET %s""",
                    (agent_id, limit, offset),
                )
                rows = cur.fetchall()
                return [
                    {
                        "id": r[0], "title": r[1], "message_count": r[2],
                        "created_at": r[3].isoformat() if r[3] else None,
                        "updated_at": r[4].isoformat() if r[4] else None,
                    }
                    for r in rows
                ]

    def _schema_ready(self) -> bool:
        """Check whether the schema tables already exist."""
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT EXISTS (
                        SELECT FROM information_schema.tables
                        WHERE table_schema = 'public'
                          AND table_name = 'news_articles'
                    )"""
                )
                return cur.fetchone()[0]
        finally:
            self._pool.putconn(conn)

    @property
    def is_connected(self) -> bool:
        return self._pool is not None

    # ── Connection context manager ─────────────────────────────────

    @contextmanager
    def get_conn(self):
        """Yield a connection from the pool with auto commit/rollback."""
        if self._pool is None:
            raise RuntimeError("PostgreSQL not connected. Call connect() first.")
        conn = self._pool.getconn()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)

    # ── Save news data ─────────────────────────────────────────────

    def save_news_data(
        self,
        news_data,           # NewsData from news.models
        source_tiers: Optional[Dict[str, Dict[str, int]]] = None,
        crawled_from: str = "local",
        skip_existing: bool = False,
    ) -> Dict[str, int]:
        """Save NewsData to PostgreSQL with batch UPSERT logic.

        Items are partitioned by ON CONFLICT target (hotlist / rss /
        manual / fallback) and inserted in batches using
        ``execute_values``.

        Dedup: hotlist on (source_id, url), rss on (source_id, guid),
        manual on (source_id, url).
        Content is preserved via CASE WHEN on conflict for hotlist/rss;
        manual always overwrites content.
        """
        if source_tiers is None:
            source_tiers = {}

        # Partition items by conflict-target type
        hotlist_rows: List[Tuple] = []
        rss_rows: List[Tuple] = []
        manual_rows: List[Tuple] = []
        fallback_rows: List[Tuple] = []

        for source_id, news_list in news_data.items.items():
            tier_info = source_tiers.get(source_id, {})
            tier = tier_info.get("tier", 4)
            priority = tier_info.get("priority", 0)

            for item in news_list:
                row = self._build_row(
                    item, source_id, tier, priority, crawled_from,
                )
                if item.source_type == "hotlist" and item.url:
                    hotlist_rows.append(row)
                elif item.source_type == "rss" and item.guid:
                    rss_rows.append(row)
                elif item.source_type == "manual" and item.url:
                    manual_rows.append(row)
                else:
                    fallback_rows.append(row)

        t0 = time.time()
        processed = 0
        skipped = 0

        with self.get_conn() as conn:
            with conn.cursor() as cur:
                if hotlist_rows:
                    sql = _HOTLIST_INSERT_SQL  # always DO NOTHING on URL conflict
                    n, s = self._execute_batch(cur, sql, hotlist_rows)
                    processed += n
                    skipped += s

                if rss_rows:
                    sql = (
                        _RSS_INSERT_SKIP_SQL if skip_existing
                        else _RSS_INSERT_SQL
                    )
                    n, s = self._execute_batch(cur, sql, rss_rows)
                    processed += n
                    skipped += s

                if manual_rows:
                    sql = (
                        _MANUAL_INSERT_SKIP_SQL if skip_existing
                        else _MANUAL_INSERT_SQL
                    )
                    n, s = self._execute_batch(cur, sql, manual_rows)
                    processed += n
                    skipped += s

                if fallback_rows:
                    n, s = self._execute_batch(
                        cur, _FALLBACK_INSERT_SQL, fallback_rows,
                    )
                    processed += n
                    skipped += s

        elapsed = time.time() - t0
        msg = (
            f"[DB] Saved {processed} items in {elapsed:.2f}s"
            f" (crawled_from={crawled_from})"
        )
        if skipped:
            msg += f", skipped {skipped}"
        print(msg)
        return {"processed": processed, "skipped": skipped}

    # ── Batch helpers ──────────────────────────────────────────────

    @staticmethod
    def _build_row(
        item,
        source_id: str,
        tier: int,
        priority: int,
        crawled_from: str,
    ) -> Tuple:
        """Convert a NewsItem into a 21-element tuple for batch INSERT."""
        ts_published = _to_timestamptz(item.published_at, None)
        ts_crawled = _to_timestamptz(item.crawled_at, None)

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
            ts_published,
            ts_crawled,
            item.summary,
            item.author,
            item.content,
            item.category if item.category else None,
            item.tags if item.tags else [],
            crawled_from,
            json.dumps(item.ranks) if item.ranks else '[]',
            item.heat_score,
            item.sentiment_score,
        )

    def _execute_batch(
        self,
        cur,
        sql: str,
        items: List[Tuple],
        page_size: int = 100,
    ) -> Tuple[int, int]:
        """Execute batch INSERT via ``execute_values``.

        On batch failure, retries with progressively smaller sub-batches
        (100 → 10 → 1) with savepoint isolation at the single-row level.
        This avoids one bad row forcing the entire batch into the slow path.

        Returns:
            ``(processed, skipped)`` counts.
        """
        processed = 0
        skipped = 0
        conn = cur.connection

        for i in range(0, len(items), page_size):
            batch = items[i:i + page_size]
            n, s = self._execute_batch_retry(
                cur, conn, sql, batch, page_size,
            )
            processed += n
            skipped += s

        return processed, skipped

    def _execute_batch_retry(
        self,
        cur,
        conn,
        sql: str,
        batch: List[Tuple],
        page_size: int,
    ) -> Tuple[int, int]:
        """Attempt a batch INSERT; on failure, divide and retry.

        Each attempt wraps the batch in a savepoint (``with conn:``) so
        a single bad row rolls back only that savepoint — previously
        inserted rows in the same transaction survive.
        """
        try:
            with conn:  # savepoint — auto-rolled-back on failure
                psycopg2.extras.execute_values(
                    cur, sql, batch, page_size=page_size,
                )
            return len(batch), 0
        except psycopg2.Error as e:
            if page_size <= 1:
                print(f"[DB]   Row failed: {e}")
                return 0, 1

            # Batch failed — divide into smaller sub-batches.
            # Each sub-batch gets its own savepoint via recursion.
            next_size = max(1, min(10, page_size // 10))
            print(
                f"[DB] Batch of {len(batch)} failed: {e}"
                f" — retrying with page_size={next_size}"
            )
            processed = 0
            skipped = 0
            for j in range(0, len(batch), next_size):
                sub = batch[j:j + next_size]
                n, s = self._execute_batch_retry(
                    cur, conn, sql, sub, next_size,
                )
                processed += n
                skipped += s
            return processed, skipped

    # ── Query methods ──────────────────────────────────────────────

    def get_urls_with_content(self, urls: List[str]) -> set:
        """Return the subset of *urls* that already have non-empty content.

        Used before content enrichment to skip downloading articles
        that have already been fetched and parsed.
        """
        if not urls:
            return set()
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT url FROM news_articles
                       WHERE url = ANY(%s)
                         AND content IS NOT NULL
                         AND content != ''""",
                    (urls,),
                )
                return {row[0] for row in cur.fetchall()}

    def get_recent_news(
        self,
        limit: int = 50,
        offset: int = 0,
        tier: Optional[int] = None,
        category: Optional[str] = None,
        min_confidence: Optional[int] = None,
        sentiment: Optional[str] = None,
        keywords: Optional[List[str]] = None,
        search: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Return recent news articles with optional filters."""
        conditions = ["TRUE"]
        params: List[Any] = []

        if tier is not None:
            conditions.append("tier = %s")
            params.append(tier)
        if category is not None:
            conditions.append("category = %s")
            params.append(category)
        if min_confidence is not None:
            conditions.append("(confidence IS NULL OR confidence >= %s)")
            params.append(min_confidence)
        else:
            conditions.append("(confidence IS NULL OR confidence >= 20)")
        if sentiment == "positive":
            conditions.append("sentiment_score >= 67")
        elif sentiment == "negative":
            conditions.append("sentiment_score <= 33")
        elif sentiment == "neutral":
            conditions.append("sentiment_score > 33 AND sentiment_score < 67")
        if keywords:
            for kw in keywords:
                conditions.append(
                    "(title || ' ' || COALESCE(summary, '')"
                    " || ' ' || COALESCE(content, '')"
                    " || ' ' || array_to_string(tags, ' ')) ILIKE %s"
                )
                params.append(f"%{kw}%")
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
        # Date filtering: crawled_at within [date_from, date_to] inclusive full days
        if date_from is not None:
            conditions.append("COALESCE(crawled_at, created_at) >= %s::date")
            params.append(date_from)
        if date_to is not None:
            conditions.append("COALESCE(crawled_at, created_at) < %s::date + interval '1 day'")
            params.append(date_to)

        where = " AND ".join(conditions)

        with self.get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    f"""SELECT id, title, source_id, source_name, source_type,
                               tier, priority, url, mobile_url, summary,
                               tags, heat_score, sentiment_score,
                               crawled_from, is_analyzed,
                               published_at, created_at
                        FROM news_articles
                        WHERE {where}
                        ORDER BY created_at DESC NULLS LAST, heat_score DESC NULLS LAST
                        LIMIT %s OFFSET %s""",
                    params + [limit, offset],
                )
                return cur.fetchall()

    def search_news(
        self,
        query: str,
        limit: int = 10,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """全量搜索新闻，按热度排序，无时间限制。匹配标题、摘要和正文。

        CJK 查询按空白拆成多个关键词，OR 语义匹配（任一命中即返回），
        避免 "WAIC 世界人工智能大会" 这类多词查询被当成单个精确子串
        而漏掉只含部分关键词的相关新闻。

        Args:
            query: 搜索关键词
            limit: 返回条数上限（默认 10）
            offset: 偏移量
        """
        fields = (
            "title || ' ' || COALESCE(summary, '')"
            " || ' ' || COALESCE(content, '')"
        )
        if _contains_cjk(query):
            keywords = [kw for kw in query.split() if kw] or [query]
            or_clauses = " OR ".join([f"({fields}) ILIKE %s" for _ in keywords])
            condition = f"({or_clauses})"
            params: List[Any] = [f"%{kw}%" for kw in keywords]
        else:
            condition = (
                f"to_tsvector('simple', {fields})"
                " @@ plainto_tsquery('simple', %s)"
            )
            params = [query]

        with self.get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    f"""SELECT id, title, source_id, source_name, source_type,
                               tier, priority, url, mobile_url, summary,
                               tags, heat_score, sentiment_score,
                               crawled_from, is_analyzed,
                               published_at, created_at
                        FROM news_articles
                        WHERE {condition}
                        ORDER BY heat_score DESC NULLS LAST, created_at DESC NULLS LAST
                        LIMIT %s OFFSET %s""",
                    params + [limit, offset],
                )
                return cur.fetchall()

    def get_news_count(
        self,
        tier: Optional[int] = None,
        category: Optional[str] = None,
        min_confidence: Optional[int] = None,
        sentiment: Optional[str] = None,
        keywords: Optional[List[str]] = None,
        search: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
    ) -> int:
        """Return total count of news articles matching filters."""
        conditions: List[str] = []
        params: List[Any] = []

        if min_confidence is not None:
            conditions.append("(confidence IS NULL OR confidence >= %s)")
            params.append(min_confidence)
        else:
            conditions.append("(confidence IS NULL OR confidence >= 20)")

        if tier is not None:
            conditions.append("tier = %s")
            params.append(tier)
        if category is not None:
            conditions.append("category = %s")
            params.append(category)
        if sentiment == "positive":
            conditions.append("sentiment_score >= 67")
        elif sentiment == "negative":
            conditions.append("sentiment_score <= 33")
        elif sentiment == "neutral":
            conditions.append("sentiment_score > 33 AND sentiment_score < 67")
        if keywords:
            for kw in keywords:
                conditions.append(
                    "(title || ' ' || COALESCE(summary, '')"
                    " || ' ' || COALESCE(content, '')"
                    " || ' ' || array_to_string(tags, ' ')) ILIKE %s"
                )
                params.append(f"%{kw}%")
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
        if date_from is not None:
            conditions.append("COALESCE(crawled_at, created_at) >= %s::date")
            params.append(date_from)
        if date_to is not None:
            conditions.append("COALESCE(crawled_at, created_at) < %s::date + interval '1 day'")
            params.append(date_to)

        where = " AND ".join(conditions)

        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT COUNT(*) FROM news_articles WHERE {where}",
                    params,
                )
                return cur.fetchone()[0]

    def get_sentiment_counts(
        self,
        tier: Optional[int] = None,
        keywords: Optional[List[str]] = None,
        search: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
    ) -> Dict[str, int]:
        """Return {positive, negative, neutral} counts for sentiment bar."""
        conditions = ["(confidence IS NULL OR confidence >= 20)"]
        params: List[Any] = []

        if tier is not None:
            conditions.append("tier = %s")
            params.append(tier)
        if keywords:
            for kw in keywords:
                conditions.append(
                    "(title || ' ' || COALESCE(summary, '')"
                    " || ' ' || COALESCE(content, '')"
                    " || ' ' || array_to_string(tags, ' ')) ILIKE %s"
                )
                params.append(f"%{kw}%")
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
        if date_from is not None:
            conditions.append("COALESCE(crawled_at, created_at) >= %s::date")
            params.append(date_from)
        if date_to is not None:
            conditions.append("COALESCE(crawled_at, created_at) < %s::date + interval '1 day'")
            params.append(date_to)

        where = " AND ".join(conditions)

        with self.get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    f"""SELECT
                          COUNT(*) FILTER (WHERE sentiment_score >= 67) AS positive,
                          COUNT(*) FILTER (WHERE sentiment_score <= 33) AS negative,
                          COUNT(*) FILTER (WHERE sentiment_score > 33 AND sentiment_score < 67) AS neutral
                        FROM news_articles WHERE {where}""",
                    params,
                )
                return dict(cur.fetchone())

    def get_keyword_counts(
        self,
        tier: Optional[int] = None,
        sentiment: Optional[str] = None,
        search: Optional[str] = None,
        limit: int = 30,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Return [{tag, cnt}] for keyword cloud, sorted by frequency."""
        conditions = ["(confidence IS NULL OR confidence >= 20)"]
        params: List[Any] = []

        if tier is not None:
            conditions.append("tier = %s")
            params.append(tier)
        if sentiment == "positive":
            conditions.append("sentiment_score >= 67")
        elif sentiment == "negative":
            conditions.append("sentiment_score <= 33")
        elif sentiment == "neutral":
            conditions.append("sentiment_score > 33 AND sentiment_score < 67")
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
        if date_from is not None:
            conditions.append("COALESCE(crawled_at, created_at) >= %s::date")
            params.append(date_from)
        if date_to is not None:
            conditions.append("COALESCE(crawled_at, created_at) < %s::date + interval '1 day'")
            params.append(date_to)

        where = " AND ".join(conditions)

        with self.get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    f"""SELECT unnest(tags) AS tag, COUNT(*) AS cnt
                        FROM news_articles WHERE {where}
                        GROUP BY tag ORDER BY cnt DESC LIMIT %s""",
                    params + [limit],
                )
                return cur.fetchall()

    def get_high_impact_count(
        self,
        tier: Optional[int] = None,
        keywords: Optional[List[str]] = None,
        search: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
    ) -> int:
        """Return count of high-heat articles (proxy for 'immediate impact')."""
        conditions = [
            "(confidence IS NULL OR confidence >= 20)",
            "heat_score >= 80",
        ]
        params: List[Any] = []

        if tier is not None:
            conditions.append("tier = %s")
            params.append(tier)
        if keywords:
            for kw in keywords:
                conditions.append(
                    "(title || ' ' || COALESCE(summary, '')"
                    " || ' ' || COALESCE(content, '')"
                    " || ' ' || array_to_string(tags, ' ')) ILIKE %s"
                )
                params.append(f"%{kw}%")
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
        # Use date parameters instead of hardcoded CURRENT_DATE
        if date_from is not None:
            conditions.append("COALESCE(crawled_at, created_at) >= %s::date")
            params.append(date_from)
        if date_to is not None:
            conditions.append("COALESCE(crawled_at, created_at) < %s::date + interval '1 day'")
            params.append(date_to)
        # Fall back to today when no date params given (backward compatible)
        if date_from is None and date_to is None:
            conditions.append("COALESCE(crawled_at, created_at) >= CURRENT_DATE")

        where = " AND ".join(conditions)

        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT COUNT(*) FROM news_articles WHERE {where}",
                    params,
                )
                return cur.fetchone()[0]

    def get_latest_cloud_sync_date(self):
        """Return the latest ``created_at`` timestamp for cloud-synced
        records, or None if no cloud records exist.

        Used by :meth:`Crawler.sync_from_cloud` to decide which cloud
        storage files need to be downloaded and which rows are incremental.
        """
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT MAX(created_at)
                       FROM news_articles
                       WHERE crawled_from = 'cloud'"""
                )
                row = cur.fetchone()
                if row and row[0] is not None:
                    return row[0]  # datetime with timezone
                return None

    def get_news_by_id(self, article_id: int) -> Optional[Dict[str, Any]]:
        """Return a single article by ID, including content."""
        with self.get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT * FROM news_articles WHERE id = %s",
                    (article_id,),
                )
                article = cur.fetchone()
                if article:
                    article["images"] = []
                return article

    def get_stats(self, date_from: Optional[str] = None, date_to: Optional[str] = None,
                  search: Optional[str] = None) -> Dict[str, Any]:
        """Return dashboard stats: counts by tier, source, and today's new."""
        conditions = ["(confidence IS NULL OR confidence >= 20)"]
        params: list = []
        if date_from:
            conditions.append("COALESCE(crawled_at, created_at) >= %s::date")
            params.append(date_from)
        if date_to:
            conditions.append("COALESCE(crawled_at, created_at) < %s::date + interval '1 day'")
            params.append(date_to)
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
        where_clause = " WHERE " + " AND ".join(conditions)

        with self.get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    f"""SELECT
                         COUNT(*) FILTER (WHERE tier = 1) AS t1_count,
                         COUNT(*) FILTER (WHERE tier = 2) AS t2_count,
                         COUNT(*) FILTER (WHERE tier = 3) AS t3_count,
                         COUNT(*) FILTER (WHERE tier = 4) AS t4_count,
                         COUNT(*) AS total_count,
                         COUNT(*) FILTER (WHERE COALESCE(crawled_at, created_at) >= CURRENT_DATE
                                          AND COALESCE(crawled_at, created_at) < CURRENT_DATE + interval '1 day')
                           AS today_count
                       FROM news_articles{where_clause}""",
                    params,
                )
                stats = dict(cur.fetchone())

                cur.execute(
                    f"SELECT source_name, COUNT(*) AS cnt FROM news_articles{where_clause} GROUP BY source_name ORDER BY cnt DESC",
                    params,
                )
                stats["by_source"] = cur.fetchall()

                return stats

    def get_article_by_url(self, url: str) -> Optional[Dict[str, Any]]:
        """Return the first article matching *url*, or None."""
        with self.get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT id, title, url FROM news_articles WHERE url = %s ORDER BY id LIMIT 1",
                    (url,),
                )
                return cur.fetchone()

    def get_articles_without_content(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Return articles where content is NULL/empty, ordered by priority."""
        with self.get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """SELECT id, title, url, source_name, tier
                       FROM news_articles
                       WHERE (content IS NULL OR content = '')
                         AND url != ''
                       ORDER BY tier ASC, priority DESC
                       LIMIT %s""",
                    (limit,),
                )
                return cur.fetchall()

    def update_article_content(self, article_id: int, content: str) -> bool:
        """Update an article's content field directly. Returns True if a row was updated."""
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE news_articles SET content = %s, updated_at = NOW() WHERE id = %s",
                    (content, article_id),
                )
                return cur.rowcount > 0

    def update_article_full(
        self,
        article_id: int,
        title: str = "",
        content: str = "",
        published_at=None,
        author: str = "",
        summary: str = "",
        category: str = "",
        tags: list | None = None,
    ) -> bool:
        """Update all content and metadata fields after a refetch.

        Unlike the UPSERT path (which preserves non-empty content on
        conflict), this unconditionally overwrites every field so the
        DB stays consistent with what the parser extracted — including
        ``published_at`` which drives the ``/media/`` image path
        resolution in the web layer.
        """
        # Normalise published_at so the SQL parameter stays type-safe.
        #
        # psycopg2 binds Python datetime → timestamptz, and None → NULL.
        # The SQL uses plain COALESCE(%s, published_at): when the value is
        # NULL the column keeps its existing value; when it is a datetime
        # it replaces the column.  This avoids the NULLIF(%s, '') pattern
        # which breaks when psycopg2 and PostgreSQL disagree on the
        # parameter type (text ↔ timestamptz mismatch).
        if isinstance(published_at, str):
            published_at = _to_timestamptz(published_at, None)
        elif not isinstance(published_at, datetime):
            published_at = None

        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE news_articles
                       SET title = COALESCE(NULLIF(%s, ''), title),
                           content = %s,
                           published_at = COALESCE(%s, published_at),
                           author = COALESCE(NULLIF(%s, ''), author),
                           summary = COALESCE(NULLIF(%s, ''), summary),
                           category = COALESCE(NULLIF(%s, ''), category),
                           tags = COALESCE(%s, tags),
                           updated_at = NOW()
                       WHERE id = %s""",
                    (title, content, published_at, author, summary,
                     category, tags, article_id),
                )
                return cur.rowcount > 0

    def delete_news(self, article_id: int) -> bool:
        """Delete an article by ID.

        Returns True if a row was deleted, False if no article had that ID.
        """
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM news_articles WHERE id = %s", (article_id,))
                return cur.rowcount > 0

    def set_sentiment_score(self, article_id: int, score: int) -> bool:
        """Set the sentiment score for an article (user override).

        Args:
            article_id: Article ID.
            score: Sentiment score (0, 30, 60, 80, 100).

        Returns:
            True if the article was found and updated, False otherwise.
        """
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE news_articles SET sentiment_score = %s WHERE id = %s",
                    (score, article_id),
                )
                return cur.rowcount > 0

    # ── Failed tasks (failure recording & lazy retry) ──────────────

    def record_failure(
        self,
        task_type: str,
        context: dict,
        max_retry: int = 3,
    ) -> Optional[int]:
        """Record a failed task for later retry.

        Uses ``INSERT ... ON CONFLICT DO NOTHING`` so duplicate pending
        tasks for the same URL + task_type are silently ignored.

        Returns:
            The new task ``id``, or ``None`` if a pending task for the
            same URL + task_type already exists.
        """
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO failed_tasks (task_type, context, max_retry)
                       VALUES (%s, %s, %s)
                       ON CONFLICT (task_type, (context->>'url'))
                       WHERE status = 'pending'
                       DO NOTHING
                       RETURNING id""",
                    (task_type, json.dumps(context), max_retry),
                )
                row = cur.fetchone()
                return row[0] if row else None

    def get_pending_failures(
        self,
        task_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Return pending failed tasks where retry_times < max_retry.

        Args:
            task_type: Optional filter.  When ``None``, returns all types.
        """
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                if task_type:
                    cur.execute(
                        """SELECT id, task_type, context, retry_times, max_retry,
                                  last_retry, status, created_at, updated_at
                           FROM failed_tasks
                           WHERE status = 'pending'
                             AND retry_times < max_retry
                             AND task_type = %s
                           ORDER BY created_at""",
                        (task_type,),
                    )
                else:
                    cur.execute(
                        """SELECT id, task_type, context, retry_times, max_retry,
                                  last_retry, status, created_at, updated_at
                           FROM failed_tasks
                           WHERE status = 'pending'
                             AND retry_times < max_retry
                           ORDER BY created_at"""
                    )
                rows = cur.fetchall()
                return [
                    {
                        "id": r[0],
                        "task_type": r[1],
                        "context": r[2],
                        "retry_times": r[3],
                        "max_retry": r[4],
                        "last_retry": r[5],
                        "status": r[6],
                        "created_at": r[7],
                        "updated_at": r[8],
                    }
                    for r in rows
                ]

    def article_has_content(self, url: str) -> bool:
        """Check whether any article with *url* already has non-empty content.

        Used to skip content_fetch retry when the article was already
        fetched successfully through the normal crawl path.
        """
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT 1 FROM news_articles
                       WHERE url = %s
                         AND content IS NOT NULL
                         AND content != ''
                       LIMIT 1""",
                    (url,),
                )
                return cur.fetchone() is not None

    def mark_failure_completed(self, task_id: int) -> None:
        """Mark a failed task as successfully retried."""
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE failed_tasks
                       SET status = 'completed',
                           updated_at = NOW()
                       WHERE id = %s
                         AND status = 'pending'""",
                    (task_id,),
                )

    def mark_failure_retried(self, task_id: int, error: str = "") -> None:
        """Increment retry_times and set last_retry.

        If retry_times reaches max_retry after increment, set status to
        ``'failed'`` (permanent).
        """
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE failed_tasks
                       SET retry_times = retry_times + 1,
                           last_retry = NOW(),
                           updated_at = NOW(),
                           status = CASE
                               WHEN retry_times + 1 >= max_retry
                               THEN 'failed'
                               ELSE 'pending'
                           END
                       WHERE id = %s
                         AND status = 'pending'""",
                    (task_id,),
                )

    def find_articles_by_image_url(self, image_url: str) -> List[int]:
        """Return article IDs whose content contains *image_url*."""
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT id FROM news_articles
                       WHERE position(%s in content) > 0""",
                    (image_url,),
                )
                return [r[0] for r in cur.fetchall()]

    def update_article_image_url(
        self,
        article_id: int,
        old_url: str,
        new_path: str,
    ) -> None:
        """Replace *old_url* with *new_path* in an article's content."""
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE news_articles
                       SET content = REPLACE(content, %s, %s),
                           updated_at = NOW()
                       WHERE id = %s""",
                    (old_url, new_path, article_id),
                )
