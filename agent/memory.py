"""Memory module — 记忆系统的 ABC 与内置实现。"""

from __future__ import annotations

import asyncio
import re
from abc import ABC, abstractmethod
from typing import Any

import jieba.analyse
import jieba.posseg
import psycopg2.extras

# CJK 字符检测（与 storage/postgres.py 保持一致）
_CJK_RE = re.compile(r"[一-鿿㐀-䶿豈-﫿]")


# ── Module 层 ──────────────────────────────────────────────────


class MemoryModule(ABC):
    """记忆模块基类。

    Executor 在推理循环的适当时机调用 hook 方法。
    """

    @abstractmethod
    async def on_before_execute(self, ctx: Any) -> None:
        """执行前：检索相关记忆，注入 Context.memory_context。"""
        ...

    @abstractmethod
    async def on_after_execute(self, ctx: Any) -> None:
        """执行后：存储本次交互到记忆。"""
        ...


class NullMemory(MemoryModule):
    """空记忆——什么都不做，用于显式关闭记忆功能。"""

    async def on_before_execute(self, ctx: Any) -> None:
        pass

    async def on_after_execute(self, ctx: Any) -> None:
        pass


class ShortTermMemory(MemoryModule):
    """短期记忆——内存级滑动窗口，重启即失。

    存储原始消息列表 [{role, content}, ...]，直接注入 Context。
    超出 window_size 轮后丢弃最旧的消息对。

    适用于：不需要跨会话记忆的场景（单页 AI 助手、一次性对话）。
    """

    def __init__(self, window_size: int = 20):
        if window_size < 1:
            raise ValueError("window_size must be >= 1")
        self._window: list[dict] = []
        self._window_size = window_size

    async def on_before_execute(self, ctx: Any) -> None:
        """将历史消息列表注入 Context。"""
        ctx.memory_context = list(self._window)  # 浅拷贝，防止外部修改

    async def on_after_execute(self, ctx: Any) -> None:
        self._window.append({"role": "user", "content": ctx.user_input})
        self._window.append({"role": "assistant", "content": ctx.assistant_output})
        if len(self._window) > self._window_size * 2:
            self._window = self._window[-(self._window_size * 2):]

    @property
    def turn_count(self) -> int:
        """当前窗口中的对话轮数。"""
        return len(self._window) // 2

    def clear(self) -> None:
        """清空记忆。"""
        self._window.clear()


# ── Storage 层（LongTermMemory 专用） ──────────────────────────


class MemoryStorage(ABC):
    """记忆持久化存储基类。"""

    @abstractmethod
    async def search(self, query: str, top_k: int = 5) -> list[dict]:
        """搜索相关记忆。"""
        ...

    @abstractmethod
    async def save(self, session_id: str, content: str,
                   memory_type: str = "summary", **meta: Any) -> None:
        """保存一条记忆。"""
        ...

    @abstractmethod
    async def batch_save(self, records: list[dict]) -> None:
        """批量保存。"""
        ...


class PgMemoryStorage(MemoryStorage):
    """PostgreSQL 实现的记忆存储。

    包装项目现有的同步 psycopg2 PostgreSQL 实例，
    通过 asyncio.to_thread 桥接到异步接口。

    表结构:
        agent_memories (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            session_id  TEXT NOT NULL,
            agent_name  TEXT NOT NULL DEFAULT '',
            memory_type TEXT NOT NULL DEFAULT 'summary',
            content     TEXT NOT NULL,
            created_at  TIMESTAMPTZ DEFAULT now(),
            updated_at  TIMESTAMPTZ DEFAULT now()
        )

    索引:
        - idx_memories_session: (session_id, created_at DESC)
        - idx_memories_search: GIN (to_tsvector('simple', content))
    """

    def __init__(self, db, agent_name: str = ""):
        self._db = db
        self._agent_name = agent_name

    async def search(self, query: str, top_k: int = 5) -> list[dict]:
        """使用 PostgreSQL 全文搜索检索相关记忆。"""
        return await asyncio.to_thread(self._search_sync, query, top_k)

    async def save(self, session_id: str, content: str,
                   memory_type: str = "summary", **meta: Any) -> None:
        return await asyncio.to_thread(
            self._save_sync, session_id, content, memory_type,
        )

    async def batch_save(self, records: list[dict]) -> None:
        if not records:
            return
        return await asyncio.to_thread(self._batch_save_sync, records)

    # ── 同步实现（在 asyncio.to_thread 中执行） ──────────────

    def _search_sync(self, query: str, top_k: int) -> list[dict]:
        with self._db.get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                if _CJK_RE.search(query):
                    # CJK 搜索：拆成单个关键词，OR 语义（ILIKE ANY）
                    keywords = query.split()
                    if not keywords:
                        return []
                    patterns = [f"%{kw}%" for kw in keywords]
                    cur.execute(
                        """
                        SELECT id, session_id, memory_type, content, created_at
                        FROM agent_memories
                        WHERE agent_name = %s
                          AND content ILIKE ANY(%s)
                        ORDER BY created_at DESC
                        LIMIT %s
                        """,
                        (self._agent_name, patterns, top_k),
                    )
                else:
                    # ASCII 搜索：拆成 OR 连接，PG 的 english 配置处理停用词
                    keywords = query.split()
                    if not keywords:
                        return []
                    tsq_parts = " | ".join(keywords)
                    cur.execute(
                        """
                        SELECT id, session_id, memory_type, content, created_at
                        FROM agent_memories
                        WHERE agent_name = %s
                          AND to_tsvector('english', content) @@ to_tsquery('english', %s)
                        ORDER BY ts_rank(to_tsvector('english', content), to_tsquery('english', %s)) DESC
                        LIMIT %s
                        """,
                        (self._agent_name, tsq_parts, tsq_parts, top_k),
                    )
                return [dict(r) for r in cur.fetchall()]

    def _save_sync(self, session_id: str, content: str,
                   memory_type: str = "summary") -> None:
        with self._db.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO agent_memories
                        (session_id, agent_name, memory_type, content)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (session_id, self._agent_name, memory_type, content),
                )

    def _batch_save_sync(self, records: list[dict]) -> None:
        with self._db.get_conn() as conn:
            with conn.cursor() as cur:
                psycopg2.extras.execute_values(
                    cur,
                    """
                    INSERT INTO agent_memories
                        (session_id, agent_name, memory_type, content)
                    VALUES %s
                    """,
                    [
                        (r["session_id"], self._agent_name,
                         r.get("memory_type", "summary"), r["content"])
                        for r in records
                    ],
                )


# ── LongTermMemory ─────────────────────────────────────────────


class LongTermMemory(MemoryModule):
    """长期记忆——持久化存储，跨会话保留。

    记忆提取策略:
    - 周期性: 每 N 轮对话后自动合并记忆
    - 触发性: 检测到关键信息时即时存储

    extractor 是可选的 LLM 调用函数，用于从对话中提取可记忆的信息。
    不传 extractor 时只做原始文本存储，不做智能提取。
    """

    def __init__(
        self,
        storage: MemoryStorage,
        extract_interval: int = 5,
        extractor: callable | None = None,
    ):
        if extract_interval < 1:
            raise ValueError("extract_interval must be >= 1")
        self._storage = storage
        self._extract_interval = extract_interval
        self._extractor = extractor
        self._turn_count = 0

    async def on_before_execute(self, ctx: Any) -> None:
        """语义检索相关记忆。"""
        if not ctx.user_input:
            return
        query = self._build_search_query(ctx.user_input)
        if not query:
            return
        memories = await self._storage.search(query, top_k=5)
        if memories:
            ctx.memory_context = self._format_memories(memories)

    async def on_after_execute(self, ctx: Any) -> None:
        self._turn_count += 1
        if self._should_extract(ctx):
            await self._extract_and_store(ctx)
        elif self._turn_count % self._extract_interval == 0:
            await self._batch_merge(ctx)

    def _build_search_query(self, user_input: str) -> str:
        """提取检索关键词。

        中文：用 jieba TF-IDF 提取关键词，自动压低通用词权重（的、是等）。
        英文：跳过 jieba（其 IDF 模型没有英文词权重），由 PG FTS 处理停用词 + stemming。
        """
        cleaned = re.sub(r'[^\w\s一-鿿]', ' ', user_input)
        stripped = cleaned.strip()
        if not stripped:
            return ""

        if _CJK_RE.search(cleaned):
            # 中文 → jieba TF-IDF 提取关键词
            keywords = jieba.analyse.tfidf(cleaned, topK=10, withWeight=False)
            return ' '.join(keywords) if keywords else stripped
        else:
            # 英文 → 跳过 jieba，PG FTS 自己处理
            return stripped

    def _should_extract(self, ctx: Any) -> bool:
        """判断本次对话是否需要触发记忆提取。

        触发条件（任一满足即可）:
        1. 用户输入包含命名实体（人名、地名、机构名），值得记住
        2. assistant 输出较长（>100 字符），可能包含值得记住的信息
        """
        if self._has_notable_entities(ctx.user_input):
            return True
        return len(ctx.assistant_output) > 100

    @staticmethod
    def _has_notable_entities(text: str) -> bool:
        """用 jieba POS 标签检测是否包含值得记忆的命名实体。"""
        if not text or len(text) < 3:
            return False
        words = jieba.posseg.lcut(text)
        for w in words:
            if w.flag in ("nr", "ns", "nt") and len(w.word) >= 2:
                return True
        return False

    async def _extract_and_store(self, ctx: Any) -> None:
        """提取关键信息并存储。"""
        if self._extractor:
            summary = await self._extractor(ctx)
        else:
            summary = ctx.assistant_output[:200]

        await self._storage.save(
            session_id=ctx.session_id,
            content=summary,
            memory_type="fact",
        )

    async def _batch_merge(self, ctx: Any) -> None:
        """合并最近对话并存储摘要。"""
        await self._storage.save(
            session_id=ctx.session_id,
            content=ctx.assistant_output[:200],
            memory_type="summary",
        )

    @staticmethod
    def _format_memories(memories: list[dict]) -> str:
        """将记忆列表格式化为可注入 Context 的文本。"""
        lines = ["## 相关记忆"]
        for m in memories:
            lines.append(f"- [{m.get('memory_type', 'note')}] {m['content']}")
        return "\n".join(lines)
