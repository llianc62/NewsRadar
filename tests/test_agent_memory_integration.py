"""Integration tests for PgMemoryStorage — 验证 PG 全文搜索的实际召回效果。

需要运行中的 PostgreSQL 实例（docker compose up -d），
测试数据库名通过 PG_TEST_DATABASE 环境变量配置，默认 newsradar_test。

运行: pytest -m integration tests/test_agent_memory_integration.py -v
"""

from __future__ import annotations

import os
from copy import deepcopy

import pytest

from agent.memory import PgMemoryStorage
from config.loader import load_config
from storage.postgres import PostgreSQL


@pytest.fixture(scope="module")
def integration_pg_db():
    """PostgreSQL 测试数据库实例（模块级，所有测试共享）。"""
    config = load_config("config.yaml")
    pg_config = deepcopy(config["postgresql"])
    pg_config["database"] = os.environ.get("PG_TEST_DATABASE", "newsradar_test")
    db = PostgreSQL(pg_config)
    db.connect()
    db.init_schema()
    yield db
    db.close()


@pytest.fixture
def storage(integration_pg_db):
    """返回一个 PgMemoryStorage 实例，每测试后清理数据。"""
    _storage = PgMemoryStorage(db=integration_pg_db, agent_name="test_agent")
    yield _storage
    # 测试后清理：删除本 agent 的所有记忆
    with integration_pg_db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM agent_memories WHERE agent_name = %s",
                ("test_agent",),
            )


@pytest.mark.integration
class TestPgMemoryStorageIntegration:
    """PgMemoryStorage 的 PG 全文搜索召回集成测试。"""

    @pytest.mark.asyncio
    async def test_search_empty_returns_empty(self, storage):
        """未存储任何记忆时，搜索应返回空列表。"""
        results = await storage.search("python", top_k=5)
        assert results == []

    @pytest.mark.asyncio
    async def test_save_and_search_english(self, storage):
        """保存英文记忆后，全文搜索应正确命中。"""
        await storage.save(
            session_id="sess-int-1",
            content="The user prefers Python programming for data analysis",
            memory_type="fact",
        )
        results = await storage.search("python programming", top_k=5)
        assert len(results) >= 1
        assert "Python" in results[0]["content"]
        assert results[0]["session_id"] == "sess-int-1"
        assert results[0]["memory_type"] == "fact"

    @pytest.mark.asyncio
    async def test_save_and_search_chinese(self, storage):
        """保存中文记忆后，全文搜索应正确命中。"""
        await storage.save(
            session_id="sess-int-2",
            content="用户喜欢人工智能和机器学习",
            memory_type="fact",
        )
        results = await storage.search("人工智能", top_k=5)
        assert len(results) >= 1
        assert "人工智能" in results[0]["content"]

    @pytest.mark.asyncio
    async def test_search_multiple_memories(self, storage):
        """同 session 的多条记忆都应被检索到。"""
        await storage.save(
            session_id="sess-rank",
            content="Python is a programming language",
            memory_type="fact",
        )
        await storage.save(
            session_id="sess-rank",
            content="The user loves Python for data science",
            memory_type="fact",
        )
        # "python" 能命中两条记录（两条都包含 python）
        results = await storage.search("python", top_k=5)
        assert len(results) >= 2

    @pytest.mark.asyncio
    async def test_search_respects_agent_name(self, storage, integration_pg_db):
        """不同 agent_name 应彼此隔离，互不可见。"""
        other = PgMemoryStorage(db=integration_pg_db, agent_name="other_agent")
        results = await other.search("python", top_k=5)
        assert len(results) == 0

    @pytest.mark.asyncio
    async def test_batch_save(self, storage):
        """批量保存多条记忆后，搜索应能命中。"""
        records = [
            {"session_id": "sess-batch", "content": "User likes coffee", "memory_type": "fact"},
            {"session_id": "sess-batch", "content": "User dislikes tea", "memory_type": "fact"},
        ]
        await storage.batch_save(records)
        results = await storage.search("coffee", top_k=5)
        assert len(results) >= 1

    @pytest.mark.asyncio
    async def test_search_with_no_match(self, storage):
        """搜索无关词汇应返回空结果。"""
        results = await storage.search("xyznonexistentterm12345", top_k=5)
        assert results == []

    @pytest.mark.asyncio
    async def test_save_then_save_again(self, storage):
        """多次存储同 session 的记忆，应都能检索到。"""
        await storage.save(
            session_id="sess-multi", content="First memory about Rust", memory_type="fact",
        )
        await storage.save(
            session_id="sess-multi", content="Second memory about Python", memory_type="fact",
        )
        results = await storage.search("Rust", top_k=5)
        assert len(results) >= 1
        results = await storage.search("Python", top_k=5)
        assert len(results) >= 1

    @pytest.mark.asyncio
    async def test_search_chinese_multi_keyword(self, storage):
        """中文多关键词搜索用 OR 语义，匹配任一关键词即返回。"""
        await storage.save(
            session_id="sess-multi-zh",
            content="用户喜欢人工智能和机器学习",
            memory_type="fact",
        )
        # "机器 学习 喜欢" 任一命中即返回
        results = await storage.search("机器 学习 喜欢", top_k=5)
        assert len(results) >= 1
        assert "人工智能" in results[0]["content"]

    @pytest.mark.asyncio
    async def test_search_english_multi_keyword(self, storage):
        """英文多关键词搜索用 OR 语义，匹配任一即返回。"""
        await storage.save(
            session_id="sess-multi-en",
            content="Python is great for data science",
            memory_type="fact",
        )
        # "python" 和 "about" 用 OR 连接，"python" 应该命中
        results = await storage.search("python about", top_k=5)
        assert len(results) >= 1
