"""Tests for ReActExecutor._normalize_tool_result — 工具结果归一化。

聚焦 get_hot_topics 分页返回（{items, total, next_offset}）与既有 list/dict
返回类型的兼容性：dict 里嵌套的 list（items）必须逐条展平，不能被 [:200] 硬截断。
"""

from __future__ import annotations

import json

from agent.executor import ReActExecutor


def _hot_topics_raw(n: int = 8) -> str:
    """构造 get_hot_topics 返回的分页 dict（items 足够长以触发截断，但不超 4096）。"""
    items = [
        {
            "id": i,
            "title": f"热点新闻第{i}条标题",
            "summary": f"第{i}条摘要内容",
            "source": f"来源{i}",
            "tier": 1,
            "heat_score": 100 - i,
            "url": f"https://example.com/{i}",
            "published_at": "2026-11-08 08:00:00",
        }
        for i in range(1, n + 1)
    ]
    return json.dumps(
        {"items": items, "total": n, "next_offset": 30},
        ensure_ascii=False,
    )


class TestNormalizeDictWithItems:
    def test_items_list_is_flattened_not_truncated(self):
        """dict 里的 list(items) 应逐条展平，而非 json[:200] 截断。"""
        result = ReActExecutor._normalize_tool_result(_hot_topics_raw(8))
        for i in (1, 2, 5, 8):
            assert f"热点新闻第{i}条标题" in result, f"第 {i} 条标题被截断丢失"
        assert "来源8" in result

    def test_pagination_metadata_preserved(self):
        """total / next_offset 标量应完整保留在归一化结果中。"""
        result = ReActExecutor._normalize_tool_result(_hot_topics_raw(8))
        assert "total: 8" in result
        assert "next_offset: 30" in result


class TestNormalizeLongResult:
    """raw 超过 MAX_LEN 时，应先解析+展平再截断（而非先截断原始 JSON 导致非法解析）。"""

    def _long_raw(self, n: int = 30) -> str:
        # summary 凑体积，确保原始 JSON 远超 4096 触发曾经的"先截断"路径
        long_summary = "很长的摘要内容" * 40
        items = [
            {"title": f"热点{i}", "source": f"来源{i}",
             "summary": long_summary, "heat_score": 95}
            for i in range(1, n + 1)
        ]
        # total/next_offset 前置（与 get_hot_topics 新返回顺序一致）
        return json.dumps(
            {"total": 106, "next_offset": 30, "items": items},
            ensure_ascii=False,
        )

    def test_metadata_survives_when_items_large(self):
        """items 超大时 total/next_offset 仍保留，items 正常展平而非 JSON 截裂。"""
        result = ReActExecutor._normalize_tool_result(self._long_raw(30))
        assert "total: 106" in result
        assert "next_offset: 30" in result
        assert "共 30 条结果" in result
        assert "热点1" in result


class TestNormalizeBackwardCompat:
    def test_top_level_list_flattened(self):
        """顶层 list 返回值仍逐条展平（regression guard）。"""
        raw = json.dumps(
            [
                {"title": "标题A", "source": "来源A", "summary": "摘要A"},
                {"title": "标题B", "source": "来源B", "summary": "摘要B"},
            ],
            ensure_ascii=False,
        )
        result = ReActExecutor._normalize_tool_result(raw)
        assert "标题A" in result
        assert "标题B" in result
        assert "共 2 条结果" in result

    def test_scalar_dict_unchanged(self):
        """纯标量 dict（如 analyze_sentiment）仍 key:value 展平。"""
        raw = json.dumps(
            {"score": 80, "label": "正面", "detail": "jieba 词典"},
            ensure_ascii=False,
        )
        result = ReActExecutor._normalize_tool_result(raw)
        assert "score: 80" in result
        assert "label: 正面" in result
        assert "detail: jieba 词典" in result