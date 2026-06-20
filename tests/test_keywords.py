"""Tests for jieba TextRank keyword extraction fallback."""

import pytest


def test_strips_markdown_images():
    from news.crawler import _extract_keywords_textrank
    content = "特朗普 ![图片](https://example.com/img.png) 访问北京"
    tags = _extract_keywords_textrank(content)
    assert not any("example" in t or "http" in t or "img" in t for t in tags)


def test_strips_markdown_links():
    from news.crawler import _extract_keywords_textrank
    content = "[特朗普](https://example.com/trump) 访问北京"
    tags = _extract_keywords_textrank(content)
    assert not any("example" in t or "http" in t for t in tags)


def test_strips_markdown_formatting():
    from news.crawler import _extract_keywords_textrank
    content = "**特朗普** 访问 `北京`"
    tags = _extract_keywords_textrank(content)
    assert not any("*" in t or "`" in t for t in tags)


def test_empty_content_returns_empty():
    from news.crawler import _extract_keywords_textrank
    assert _extract_keywords_textrank("") == []


def test_short_content_returns_empty():
    from news.crawler import _extract_keywords_textrank
    assert _extract_keywords_textrank("短。") == []
    assert _extract_keywords_textrank("今天天气不错。明天可能下雨。") == []


def test_whitespace_only_returns_empty():
    from news.crawler import _extract_keywords_textrank
    assert _extract_keywords_textrank("   \n  \t  ") == []


def test_extracts_from_chinese_news():
    from news.crawler import _extract_keywords_textrank
    content = (
        "美国前总统特朗普在G7峰会期间与日本首相高市早苗会谈，"
        "双方讨论了贸易和安全议题。特朗普表示将继续推动双边合作，"
        "高市早苗则强调日本在亚太地区的战略地位。"
        "此次会谈持续了约两小时，会后双方发表了联合声明。"
    )
    tags = _extract_keywords_textrank(content)
    assert 1 <= len(tags) <= 5
    assert all(isinstance(t, str) and len(t) > 0 for t in tags)


def test_topk_respected():
    from news.crawler import _extract_keywords_textrank
    content = (
        "美国前总统特朗普在G7峰会期间与日本首相高市早苗会谈，"
        "双方讨论了贸易和安全议题。会谈持续约两小时。"
    )
    tags = _extract_keywords_textrank(content, topk=3)
    assert len(tags) <= 3


def test_no_duplicate_tags():
    from news.crawler import _extract_keywords_textrank
    content = (
        "美国前总统特朗普在G7峰会期间与日本首相高市早苗会谈，"
        "双方讨论了贸易和安全议题。" * 3
    )
    tags = _extract_keywords_textrank(content)
    assert len(tags) == len(set(tags))
