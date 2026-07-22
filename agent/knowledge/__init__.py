"""知识库子系统（Phase 3）：pgvector 语义检索 + 角色 prompt 注入。

对外入口 ``KnowledgeEngine``，组合 chunker + embedding + store。
详见 ``docs/agent/phase3-knowledge.md``。
"""

from .chunker import chunk_text
from .embedding import EmbeddingClient
from .engine import KnowledgeEngine
from .store import KnowledgeStore, PgVectorKnowledgeStore

__all__ = [
    "chunk_text",
    "EmbeddingClient",
    "KnowledgeEngine",
    "KnowledgeStore",
    "PgVectorKnowledgeStore",
]
