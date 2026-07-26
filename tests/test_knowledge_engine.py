"""单元测试 - 知识库引擎 / 存储 / embedding 客户端（mock，无网络无 PG）。"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from agent.data import Context
from agent.knowledge import (
    EmbeddingClient,
    KnowledgeEngine,
    KnowledgeStore,
    PgVectorKnowledgeStore,
)


# ═══════════════════════════════════════════════════════════════════
# Fakes
# ═══════════════════════════════════════════════════════════════════


class _FakeEmbedding:
    """记录调用、返回固定维度向量的假 embedding。"""

    def __init__(self, dim: int = 8):
        self.dim = dim
        self.calls: list[list[str]] = []

    def embed(self, texts, batch_size=64):
        self.calls.append(list(texts))
        return [[0.1] * self.dim for _ in texts]


class _FakeStore(KnowledgeStore):
    """记录 ingest/search 的假存储。"""

    def __init__(self):
        self.ingested: list[dict] = []
        self.deleted: list[str] = []
        self.search_calls: list[tuple] = []

    def ingest(self, chunks):
        self.ingested = chunks
        return len(chunks)

    def search(self, query_embedding, namespace, top_k):
        self.search_calls.append((query_embedding, namespace, top_k))
        return [
            {
                "source": c["source"],
                "namespace": c["namespace"],
                "content": c["content"],
                "metadata": c.get("metadata", {}),
                "similarity": 0.9,
            }
            for c in self.ingested[:top_k]
        ]

    def delete(self, namespace):
        self.deleted.append(namespace)
        return len(self.ingested)

    def count(self, namespace=""):
        if namespace:
            return sum(1 for c in self.ingested if c["namespace"] == namespace)
        return len(self.ingested)


# ═══════════════════════════════════════════════════════════════════
# EmbeddingClient
# ═══════════════════════════════════════════════════════════════════


class _FakeEmbItem:
    def __init__(self, index, emb):
        self.index = index
        self.embedding = emb


class _FakeEmbResp:
    def __init__(self, items):
        self.data = items


class TestEmbeddingClient:
    def test_empty_api_key_raises(self):
        with pytest.raises(ValueError, match="api_key"):
            EmbeddingClient(api_key="")

    def test_embed_empty_returns_empty(self):
        with patch("openai.OpenAI"):
            client = EmbeddingClient(api_key="k")
            assert client.embed([]) == []

    def test_embed_sorts_by_index(self):
        """OpenAI 可能乱序返回，embed 必须按 index 还原顺序。"""
        with patch("openai.OpenAI") as MockOpenAI:
            client_obj = MagicMock()
            MockOpenAI.return_value = client_obj
            client_obj.embeddings.create.return_value = _FakeEmbResp(
                [_FakeEmbItem(1, [0.2] * 4), _FakeEmbItem(0, [0.1] * 4)]
            )
            client = EmbeddingClient(api_key="k")
            result = client.embed(["a", "b"])
            assert result == [[0.1] * 4, [0.2] * 4]

    def test_embed_batches_large_input(self):
        """超过 batch_size 时分批调用 create。"""
        with patch("openai.OpenAI") as MockOpenAI:
            client_obj = MagicMock()
            MockOpenAI.return_value = client_obj
            client_obj.embeddings.create.side_effect = [
                _FakeEmbResp([_FakeEmbItem(i, [float(i)]) for i in range(2)])
                for _ in range(3)
            ]
            client = EmbeddingClient(api_key="k")
            result = client.embed(["t0", "t1", "t2", "t3", "t4", "t5"], batch_size=2)
            assert len(result) == 6
            assert client_obj.embeddings.create.call_count == 3


# ═══════════════════════════════════════════════════════════════════
# KnowledgeEngine
# ═══════════════════════════════════════════════════════════════════


class TestKnowledgeEngineConstruction:
    def test_invalid_top_k_raises(self):
        with pytest.raises(ValueError, match="top_k"):
            KnowledgeEngine(store=_FakeStore(), embedding=_FakeEmbedding(), top_k=0)

    def test_defaults(self):
        engine = KnowledgeEngine(store=_FakeStore(), embedding=_FakeEmbedding())
        assert engine._top_k == 5


class TestIngestDocuments:
    def test_ingest_chunks_and_embeds_and_stores(self):
        store = _FakeStore()
        emb = _FakeEmbedding()
        engine = KnowledgeEngine(store=store, embedding=emb)

        # 两个长段落（各 700 字），默认 max_chars=1200 软切成 2 片
        content = "甲" * 700 + "\n\n" + "乙" * 700
        n = engine.ingest_documents(
            [{"source": "doc.md", "content": content, "metadata": {"type": "doc"}}],
            namespace="investing/buffett",
        )
        assert n == 2
        # embedding 收到的是切片内容（2 片）
        assert len(emb.calls[0]) == 2
        # store 收到 2 片，每片含 embedding + namespace + source
        assert len(store.ingested) == 2
        for c in store.ingested:
            assert c["namespace"] == "investing/buffett"
            assert c["source"] == "doc.md"
            assert c["metadata"] == {"type": "doc"}
            assert "embedding" in c
            assert len(c["embedding"]) == 8

    def test_ingest_empty_content_returns_zero(self):
        store = _FakeStore()
        emb = _FakeEmbedding()
        engine = KnowledgeEngine(store=store, embedding=emb)
        n = engine.ingest_documents(
            [{"source": "empty.md", "content": ""}], namespace="ns"
        )
        assert n == 0
        assert emb.calls == []  # 没调 embedding
        assert store.ingested == []

    def test_ingest_metadata_defaults_to_empty_dict(self):
        store = _FakeStore()
        engine = KnowledgeEngine(store=store, embedding=_FakeEmbedding())
        engine.ingest_documents(
            [{"source": "d.md", "content": "一段文字"}], namespace="ns"
        )
        assert store.ingested[0]["metadata"] == {}


class TestRetrieve:
    def test_retrieve_calls_embedding_and_store(self):
        store = _FakeStore()
        emb = _FakeEmbedding()
        engine = KnowledgeEngine(store=store, embedding=emb, top_k=3)
        results = engine.retrieve("query", namespace="ns")
        # embedding 收到单条 query
        assert emb.calls[-1] == ["query"]
        # store.search 收到 (q_emb, ns, top_k)
        q_emb, ns, top_k = store.search_calls[-1]
        assert len(q_emb) == 8
        assert ns == "ns"
        assert top_k == 3
        assert len(results) == 0  # _FakeStore 空 -> []

    def test_retrieve_empty_query_returns_empty(self):
        store = _FakeStore()
        emb = _FakeEmbedding()
        engine = KnowledgeEngine(store=store, embedding=emb)
        assert engine.retrieve("", namespace="ns") == []
        assert engine.retrieve("   ", namespace="ns") == []
        assert emb.calls == []  # 空 query 不调 embedding

    def test_retrieve_top_k_override(self):
        store = _FakeStore()
        engine = KnowledgeEngine(store=store, embedding=_FakeEmbedding(), top_k=5)
        engine.retrieve("q", namespace="ns", top_k=2)
        assert store.search_calls[-1][2] == 2


class TestRetrieveRender:
    def test_render_formats_with_source_and_similarity(self):
        store = _FakeStore()
        emb = _FakeEmbedding()
        engine = KnowledgeEngine(store=store, embedding=emb)
        # 预填 2 片
        store.ingested = [
            {"source": "a.md", "namespace": "ns", "content": "内容甲", "metadata": {}},
            {"source": "b.md", "namespace": "ns", "content": "内容乙", "metadata": {}},
        ]
        text = engine.retrieve_render("q", namespace="ns", top_k=2)
        assert "[a.md]" in text
        assert "[b.md]" in text
        assert "相关度 0.90" in text
        assert "内容甲" in text
        assert "内容乙" in text
        assert "---" in text

    def test_render_empty_when_no_results(self):
        store = _FakeStore()
        engine = KnowledgeEngine(store=store, embedding=_FakeEmbedding())
        assert engine.retrieve_render("q", namespace="ns") == ""


# ═══════════════════════════════════════════════════════════════════
# PgVectorKnowledgeStore 委托
# ═══════════════════════════════════════════════════════════════════


class TestPgVectorKnowledgeStoreDelegation:
    def test_ingest_delegates(self):
        db = MagicMock()
        db.ingest_knowledge.return_value = 3
        store = PgVectorKnowledgeStore(db)
        chunks = [{"source": "s", "content": "c", "embedding": [0.1], "namespace": "n"}]
        assert store.ingest(chunks) == 3
        db.ingest_knowledge.assert_called_once_with(chunks)

    def test_search_delegates(self):
        db = MagicMock()
        db.search_knowledge.return_value = [{"content": "x"}]
        store = PgVectorKnowledgeStore(db)
        result = store.search([0.1, 0.2], "ns", 5)
        assert result == [{"content": "x"}]
        db.search_knowledge.assert_called_once_with([0.1, 0.2], "ns", 5)

    def test_delete_delegates(self):
        db = MagicMock()
        db.delete_knowledge.return_value = 7
        store = PgVectorKnowledgeStore(db)
        assert store.delete("ns") == 7
        db.delete_knowledge.assert_called_once_with("ns")

    def test_count_delegates_with_and_without_namespace(self):
        db = MagicMock()
        db.count_knowledge.return_value = 42
        store = PgVectorKnowledgeStore(db)
        assert store.count("ns") == 42
        db.count_knowledge.assert_called_with("ns")
        store.count()
        db.count_knowledge.assert_called_with("")


# ═══════════════════════════════════════════════════════════════════
# KnowledgeEngine.search(ctx) -- Task 5
# ═══════════════════════════════════════════════════════════════════


@pytest.fixture
def mock_engine():
    """KnowledgeEngine with namespace + retrieve_render stubbed to return text."""
    engine = KnowledgeEngine(
        store=_FakeStore(),
        embedding=_FakeEmbedding(),
        namespace="investing/buffett",
    )
    engine.retrieve_render = MagicMock(
        return_value="[doc.md] (相关度 0.90)\n知识片段"
    )
    return engine


@pytest.fixture
def mock_engine_no_ns():
    """KnowledgeEngine without namespace -> search early-returns."""
    engine = KnowledgeEngine(
        store=_FakeStore(),
        embedding=_FakeEmbedding(),
    )
    engine.retrieve_render = MagicMock(return_value="should-not-be-called")
    return engine


@pytest.mark.asyncio
async def test_knowledge_search_fills_memories(mock_engine):
    # mock_engine: KnowledgeEngine with namespace + retrieve_render stubbed
    ctx = Context(user_input="投资策略")
    await mock_engine.search(ctx)
    assert len(ctx.memories) == 1
    assert ctx.memories[0].source == "knowledge"
    assert ctx.memories[0].order == 20
    assert ctx.memories[0].title == "知识库"


@pytest.mark.asyncio
async def test_knowledge_search_no_namespace(mock_engine_no_ns):
    ctx = Context(user_input="x")
    await mock_engine_no_ns.search(ctx)
    assert ctx.memories == []
