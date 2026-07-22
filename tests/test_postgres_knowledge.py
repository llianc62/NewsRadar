"""单元测试 - storage/postgres.py 的知识库 CRUD（mock cursor，无真实 PG）。

仿 test_agent_db.py 的 conftest_db mock 模式：断言生成的 SQL 而非返回值。
"""

from __future__ import annotations

from unittest.mock import patch

import psycopg2.extras
import pytest

from storage.postgres import _vec_to_str
from tests.conftest_db import capture_sql


class TestIngestKnowledge:
    def test_empty_chunks_returns_zero_without_db(self, db, mock_cursor):
        """空列表直接返回 0，不触碰连接池。"""
        assert db.ingest_knowledge([]) == 0
        mock_cursor.execute.assert_not_called()

    def test_ingest_uses_execute_values_with_vector_cast(self, db):
        """批量插入走 execute_values，模板含 ::vector 强制转换。"""
        chunks = [
            {
                "source": "a.md",
                "namespace": "ns",
                "content": "内容甲",
                "embedding": [0.1, 0.2, 0.3],
                "metadata": {"type": "doc"},
            }
        ]
        with patch("psycopg2.extras.execute_values") as mock_ev:
            n = db.ingest_knowledge(chunks)
        assert n == 1
        mock_ev.assert_called_once()
        args, kwargs = mock_ev.call_args
        sql = args[1]
        rows = args[2]
        assert "INSERT INTO knowledge_chunks" in sql
        assert "::vector" in kwargs["template"]
        # 行结构 (source, namespace, content, vec_str, Json(metadata))
        source, namespace, content, vec_str, meta_json = rows[0]
        assert source == "a.md"
        assert namespace == "ns"
        assert content == "内容甲"
        assert vec_str == _vec_to_str([0.1, 0.2, 0.3])
        assert isinstance(meta_json, psycopg2.extras.Json)

    def test_ingest_metadata_defaults_to_empty(self, db):
        chunks = [{"source": "s", "content": "c", "embedding": [0.1]}]
        with patch("psycopg2.extras.execute_values") as mock_ev:
            db.ingest_knowledge(chunks)
        rows = mock_ev.call_args[0][2]
        assert isinstance(rows[0][4], psycopg2.extras.Json)


class TestSearchKnowledge:
    def test_search_sql_and_params(self, db, mock_cursor):
        mock_cursor.fetchall.return_value = [
            {"id": 1, "source": "s", "content": "c", "similarity": 0.9}
        ]
        results = db.search_knowledge([0.1, 0.2], namespace="ns", top_k=5)

        sql, params = capture_sql(mock_cursor)
        assert "<=>" in sql  # 余弦距离算子
        assert "ORDER BY" in sql
        assert "LIMIT" in sql
        assert "1 - (embedding <=> %s::vector) AS similarity" in sql
        # params: (q_vec_str, namespace, q_vec_str, top_k)
        assert params[1] == "ns"
        assert params[3] == 5
        assert params[0] == _vec_to_str([0.1, 0.2])
        assert results == [{"id": 1, "source": "s", "content": "c", "similarity": 0.9}]


class TestDeleteKnowledge:
    def test_delete_sql_and_rowcount(self, db, mock_cursor):
        mock_cursor.rowcount = 7
        n = db.delete_knowledge("ns")
        sql, params = capture_sql(mock_cursor)
        assert "DELETE FROM knowledge_chunks" in sql
        assert "WHERE namespace = %s" in sql
        assert params == ("ns",)
        assert n == 7


class TestCountKnowledge:
    def test_count_with_namespace(self, db, mock_cursor):
        mock_cursor.fetchone.return_value = [42]
        n = db.count_knowledge("ns")
        sql, params = capture_sql(mock_cursor)
        assert "SELECT COUNT(*) FROM knowledge_chunks" in sql
        assert "WHERE namespace = %s" in sql
        assert params == ("ns",)
        assert n == 42

    def test_count_all_namespaces(self, db, mock_cursor):
        mock_cursor.fetchone.return_value = [100]
        n = db.count_knowledge("")
        sql, params = capture_sql(mock_cursor)
        assert "SELECT COUNT(*) FROM knowledge_chunks" in sql
        assert "WHERE" not in sql
        assert n == 100


class TestVecToStr:
    def test_format(self):
        assert _vec_to_str([0.1, 0.2, 0.3]) == "[0.1,0.2,0.3]"

    def test_handles_int_and_float(self):
        s = _vec_to_str([1, 2.5])
        assert s == "[1.0,2.5]"
