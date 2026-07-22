"""单元测试 - 文本切片器 ``agent/knowledge/chunker.py``。"""

from __future__ import annotations

import pytest

from agent.knowledge.chunker import chunk_text


class TestChunkTextBasic:
    def test_empty_returns_empty(self):
        assert chunk_text("") == []
        assert chunk_text("   ") == []
        assert chunk_text("\n\n  \n") == []

    def test_single_short_paragraph(self):
        text = "这是一段简短的文字。"
        assert chunk_text(text) == [text]

    def test_multiple_paragraphs_packed_into_one_chunk(self):
        """两个短段落总长 < max_chars，应合并为一个片段。"""
        paras = ["第一段内容。", "第二段内容。"]
        text = "\n\n".join(paras)
        result = chunk_text(text, max_chars=200, overlap=50)
        assert len(result) == 1
        assert "第一段内容" in result[0]
        assert "第二段内容" in result[0]

    def test_paragraphs_split_when_exceeding_max(self):
        """总长超过 max_chars 时应切成多个片段。"""
        p1 = "甲" * 300
        p2 = "乙" * 300
        text = f"{p1}\n\n{p2}"
        result = chunk_text(text, max_chars=500, overlap=0)
        assert len(result) >= 2


class TestChunkTextHardCut:
    def test_oversized_paragraph_is_hard_cut(self):
        """单段超过 max_chars 时按 max_chars 硬切。"""
        para = "字" * 1000
        result = chunk_text(para, max_chars=200, overlap=0)
        assert len(result) == 5  # 1000 / 200
        # 每片恰好 max_chars
        for piece in result:
            assert len(piece) == 200

    def test_oversized_paragraph_with_overlap(self):
        """硬切带 overlap：步长 = max_chars - overlap。"""
        para = "字" * 1000
        result = chunk_text(para, max_chars=200, overlap=50)
        # 步长 150：ceil(1000/150) = 7
        assert len(result) == 7
        # 相邻片段应有重叠
        assert result[0][-50:] == result[1][:50]


class TestChunkTextOverlap:
    def test_overlap_carries_tail_into_next_chunk(self):
        """软切时，下一片段应以上一片段的 overlap 字符尾部为前缀。"""
        p1 = "甲" * 300
        p2 = "乙" * 300
        text = f"{p1}\n\n{p2}"
        result = chunk_text(text, max_chars=500, overlap=100)
        assert len(result) == 2
        # 第二片段前 100 字符 = 第一片段末 100 个"甲"
        assert result[1][:100] == "甲" * 100
        assert "乙" in result[1]

    def test_no_overlap_when_zero(self):
        p1 = "甲" * 300
        p2 = "乙" * 300
        text = f"{p1}\n\n{p2}"
        result = chunk_text(text, max_chars=500, overlap=0)
        assert len(result) == 2
        assert result[1].startswith("乙")


class TestChunkTextValidation:
    def test_invalid_max_chars_raises(self):
        with pytest.raises(ValueError):
            chunk_text("text", max_chars=0)
        with pytest.raises(ValueError):
            chunk_text("text", max_chars=-1)

    def test_invalid_overlap_raises(self):
        with pytest.raises(ValueError):
            chunk_text("text", max_chars=100, overlap=-1)
        with pytest.raises(ValueError):
            chunk_text("text", max_chars=100, overlap=100)  # overlap >= max_chars

    def test_overlap_equal_to_max_minus_one_is_ok(self):
        """overlap = max_chars - 1 是边界合法值。"""
        result = chunk_text("甲" * 300, max_chars=100, overlap=99)
        assert len(result) > 0
