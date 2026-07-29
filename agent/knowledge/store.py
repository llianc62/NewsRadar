"""知识库存储抽象 + pgvector 实现。

``KnowledgeStore`` 是与 ``storage/postgres.py`` 解耦的 ABC，
便于测试注入 mock。``PgVectorKnowledgeStore`` 委托 PostgreSQL
的 ``ingest/search/delete/count_knowledge`` 方法。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class KnowledgeStore(ABC):
    """知识切片的持久化存储抽象。"""

    @abstractmethod
    def ingest(self, chunks: list[dict]) -> int:
        """批量写入切片，返回写入数。

        每个 chunk::

            {"source": str, "namespace": str, "content": str,
             "embedding": list[float], "metadata": dict}
        """
        ...

    @abstractmethod
    def search(
        self, query_embedding: list[float], namespace: str, top_k: int
    ) -> list[dict]:
        """向量近邻检索，返回 Top-K 切片（含 similarity 字段）。"""
        ...

    @abstractmethod
    def delete(self, namespace: str) -> int:
        """按命名空间清空，返回删除数。"""
        ...

    @abstractmethod
    def count(self, namespace: str = "") -> int:
        """统计切片数；namespace 为空统计全部。"""
        ...


class PgVectorKnowledgeStore(KnowledgeStore):
    """pgvector 实现 - 委托 ``storage/postgres.py`` 的知识库方法。

    包装同步 psycopg2 ``PostgreSQL`` 实例。在异步上下文中调用方应自行
    ``asyncio.to_thread`` 包裹（与 ``PgMemoryStorage`` 一致）。
    """

    def __init__(self, db: Any):
        self._db = db

    def ingest(self, chunks: list[dict]) -> int:
        return self._db.ingest_knowledge(chunks)

    def search(
        self, query_embedding: list[float], namespace: str, top_k: int
    ) -> list[dict]:
        return self._db.search_knowledge(query_embedding, namespace, top_k)

    def delete(self, namespace: str) -> int:
        return self._db.delete_knowledge(namespace)

    def count(self, namespace: str = "") -> int:
        return self._db.count_knowledge(namespace)
