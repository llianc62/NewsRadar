"""集成测试 - 知识库 pgvector 全链路往返。

需要运行中的 PostgreSQL（pgvector/pgvector:pg16 镜像）+ 测试库
（``PG_TEST_DATABASE`` 环境变量，默认 ``newsradar_test``）。

    pytest -m integration tests/test_knowledge_integration.py -v

用 ``FakeEmbeddingClient``（确定性 token 哈希向量）避免依赖 embedding API，
专注验证 pgvector 存储/检索/namespace 隔离的 plumbing。
"""

from __future__ import annotations

import hashlib
import os
from copy import deepcopy

import pytest

from agent.knowledge import KnowledgeEngine, PgVectorKnowledgeStore
from config import load_config
from storage.postgres import PostgreSQL


class FakeEmbeddingClient:
    """确定性 embedding：token 哈希到固定维度，共享 token 越多越相似。

    无网络调用，用于集成测试验证 pgvector 往返（不测真实语义质量）。
    """

    model = "fake"

    def __init__(self, dim: int = 1536):
        self._dim = dim

    def embed(self, texts, batch_size=64):
        out = []
        for t in texts:
            vec = [0.0] * self._dim
            for tok in t.split():
                h = int(hashlib.md5(tok.encode("utf-8")).hexdigest(), 16) % self._dim
                vec[h] += 1.0
            norm = sum(v * v for v in vec) ** 0.5 or 1.0
            out.append([v / norm for v in vec])
        return out


@pytest.fixture(scope="module")
def knowledge_pg_db():
    """PG 测试库（模块级）。不可用或无 pgvector 时 skip。"""
    config = load_config("config/config.yaml")
    pg_config = deepcopy(config["postgresql"])
    pg_config["database"] = os.environ.get("PG_TEST_DATABASE", "newsradar_test")
    db = PostgreSQL(pg_config)
    try:
        db.connect()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"PostgreSQL 不可用: {e}")
    try:
        db.init_schema()  # 幂等：建表 + CREATE EXTENSION vector
    except Exception as e:  # noqa: BLE001
        db.close()
        pytest.skip(f"pgvector 扩展不可用（需 pgvector/pgvector:pg16 镜像）: {e}")
    yield db
    db.close()


@pytest.fixture
def engine(knowledge_pg_db):
    """每测试一个 KnowledgeEngine，测试后清理命名空间。"""
    ns = "test-int"
    knowledge_pg_db.delete_knowledge(ns)  # 干净起点
    eng = KnowledgeEngine(
        store=PgVectorKnowledgeStore(knowledge_pg_db),
        embedding=FakeEmbeddingClient(),
        top_k=5,
    )
    yield eng, ns
    knowledge_pg_db.delete_knowledge(ns)


@pytest.mark.integration
class TestKnowledgeEngineIntegration:
    def test_ingest_then_count(self, engine):
        eng, ns = engine
        n = eng.ingest_documents(
            [{"source": "a.md", "content": "first paragraph\n\nsecond paragraph"}],
            namespace=ns,
        )
        assert n >= 1
        assert eng._store.count(ns) == n

    def test_search_retrieves_relevant_chunk(self, engine):
        """ingest 两篇不同主题文档，查询应召回相关的那篇。"""
        eng, ns = engine
        eng.ingest_documents(
            [
                {
                    "source": "buffett.md",
                    "content": "Warren Buffett value investing margin of safety long term",
                },
                {
                    "source": "taleb.md",
                    "content": "Nassim Taleb black swan tail risk antifragility volatility",
                },
            ],
            namespace=ns,
        )
        results = eng.retrieve("buffett value investing", namespace=ns, top_k=2)
        assert len(results) >= 1
        assert results[0]["source"] == "buffett.md"
        assert "similarity" in results[0]

    def test_retrieve_render_returns_text(self, engine):
        eng, ns = engine
        eng.ingest_documents(
            [{"source": "doc.md", "content": "value investing margin of safety"}],
            namespace=ns,
        )
        text = eng.retrieve_render("value investing", namespace=ns)
        assert "[doc.md]" in text
        assert "相关度" in text
        assert "value investing" in text

    def test_namespace_isolation(self, engine, knowledge_pg_db):
        """ns1 的内容在 ns2 检索不到。"""
        eng, _ns = engine
        eng.ingest_documents(
            [{"source": "x.md", "content": "value investing margin of safety"}],
            namespace="test-int",
        )
        results = eng.retrieve("value investing", namespace="other-ns", top_k=5)
        assert results == []

    def test_delete_clears_namespace(self, engine):
        eng, ns = engine
        eng.ingest_documents(
            [{"source": "d.md", "content": "value investing margin of safety"}],
            namespace=ns,
        )
        assert eng._store.count(ns) >= 1
        deleted = eng._store.delete(ns)
        assert deleted >= 1
        assert eng._store.count(ns) == 0

    def test_empty_query_returns_empty(self, engine):
        eng, ns = engine
        eng.ingest_documents(
            [{"source": "d.md", "content": "value investing"}], namespace=ns
        )
        assert eng.retrieve("", namespace=ns) == []
