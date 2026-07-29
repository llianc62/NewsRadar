"""知识库引擎 - 切片 -> embedding -> 存 pgvector -> 语义检索 -> render。

仿 ai-hedge-fund ``FundamentalsSnapshot`` 模式：检索知识、render 成文本
喂给任意 agent。注入点见 ``docs/agent/phase3-knowledge.md``
（``ctx.memories`` -> ``## 知识库`` 块），角色定义走
``agent/agents/*.md`` -> CLI -> DB -> ``AgentFactory.build``。
"""

from __future__ import annotations

import asyncio

from ..data import MemoryBlock
from .chunker import chunk_text
from .embedding import EmbeddingClient
from .store import KnowledgeStore


class KnowledgeEngine:
    """知识库引擎：编排 chunker + embedding + store。

    用法::

        engine = KnowledgeEngine(
            store=PgVectorKnowledgeStore(db),
            embedding=EmbeddingClient(api_key=...),
        )
        engine.ingest_documents(
            [{"source": "buffett.md", "content": "..."}],
            namespace="investing/buffett",
        )
        text = engine.retrieve_render("价值投资", namespace="investing/buffett")
    """

    def __init__(
        self,
        store: KnowledgeStore,
        embedding: EmbeddingClient,
        top_k: int = 5,
        namespace: str = "",
    ):
        if top_k < 1:
            raise ValueError("top_k must be >= 1")
        self._store = store
        self._embedding = embedding
        self._top_k = top_k
        self._namespace = namespace      # 构造时绑定

    def ingest_documents(self, docs: list[dict], namespace: str) -> int:
        """``docs=[{source, content, metadata?}]`` -> 切片 -> embed -> 存库。

        返回写入的切片数。空文档 / 无内容返回 0。
        """
        chunks: list[dict] = []
        for doc in docs:
            for piece in chunk_text(doc["content"]):
                chunks.append(
                    {
                        "source": doc["source"],
                        "namespace": namespace,
                        "content": piece,
                        "metadata": doc.get("metadata") or {},
                    }
                )
        if not chunks:
            return 0

        embeddings = self._embedding.embed([c["content"] for c in chunks])
        if len(embeddings) != len(chunks):
            raise RuntimeError(
                f"embedding 数量({len(embeddings)})与切片数({len(chunks)})不匹配"
            )
        for chunk, emb in zip(chunks, embeddings):
            chunk["embedding"] = emb

        self._store.ingest(chunks)
        return len(chunks)

    def retrieve(
        self, query: str, namespace: str, top_k: int | None = None
    ) -> list[dict]:
        """语义检索 Top-K 片段。空 query 返回 ``[]``。"""
        if not query or not query.strip():
            return []
        q_emb = self._embedding.embed([query])[0]
        return self._store.search(q_emb, namespace, top_k or self._top_k)

    def retrieve_render(
        self, query: str, namespace: str, top_k: int | None = None
    ) -> str:
        """检索 + 格式化成文本块（仿 ``FundamentalsSnapshot.render()``）。

        无结果返回空串。每个片段格式::

            [来源] (相关度 0.87)
            片段正文
        """
        results = self.retrieve(query, namespace, top_k)
        if not results:
            return ""
        blocks = [
            f"[{r['source']}] (相关度 {float(r['similarity']):.2f})\n{r['content']}"
            for r in results
        ]
        return "\n\n---\n\n".join(blocks)

    async def search(self, ctx) -> None:
        """检索知识 -> ctx.memories.append(MemoryBlock, source="knowledge")。

        无 ``namespace`` 或 ``ctx.user_input`` 时直接返回（不检索）。
        检索到非空文本时追加 ``MemoryBlock(title="知识库", source="knowledge",
        order=20)`` 到 ``ctx.memories``。
        """
        if not self._namespace or not ctx.user_input:
            return
        text = await asyncio.to_thread(
            self.retrieve_render, ctx.user_input, self._namespace
        )
        if text:
            ctx.memories.append(MemoryBlock(
                title="知识库", source="knowledge", content=text, order=20,
            ))
