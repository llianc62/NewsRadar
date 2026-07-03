"""Tests for jieba TF-IDF / TextRank keyword extraction."""

import os
import tempfile
import pytest

from news.analyzer.jieba import JiebaAnalyzer


def _make_analyzer(db=None):
    """Create a JiebaAnalyzer with minimal config."""
    return JiebaAnalyzer({"analyzer": {"enabled": True, "backend": "jieba"}}, db=db)


def _test_config():
    """Minimal config the Crawler constructor accepts."""
    return {
        "app": {"timezone": "Asia/Shanghai"},
        "storage": {
            "resource": {
                "endpoint_url": "http://localhost:9000",
                "bucket_name": "test-bucket",
                "access_key_id": "test-key",
                "secret_access_key": "test-secret",
            },
        },
    }


# ── _clean_markdown_syntax 单元测试 ─────────────────────────────────

def test_clean_markdown_strips_images():
    analyzer = _make_analyzer()
    text = analyzer._clean_markdown_syntax("特朗普 ![图片](https://example.com/img.png) 访问北京")
    assert "example.com" not in text
    assert "img.png" not in text


def test_clean_markdown_strips_links():
    analyzer = _make_analyzer()
    text = analyzer._clean_markdown_syntax("[特朗普](https://example.com/trump) 访问北京")
    assert "example.com" not in text
    assert "特朗普" in text


def test_clean_markdown_strips_formatting():
    analyzer = _make_analyzer()
    text = analyzer._clean_markdown_syntax("**特朗普** 访问 `北京`")
    assert "*" not in text
    assert "`" not in text


# ── _analyze_keywords_textrank 单元测试 ────────────────────────────

def test_strips_markdown_images():
    analyzer = _make_analyzer()
    content = "特朗普 ![图片](https://example.com/img.png) 访问北京"
    tags = analyzer._analyze_keywords_textrank(content)
    assert not any("example" in t or "http" in t or "img" in t for t in tags)


def test_strips_markdown_links():
    analyzer = _make_analyzer()
    content = "[特朗普](https://example.com/trump) 访问北京"
    tags = analyzer._analyze_keywords_textrank(content)
    assert not any("example" in t or "http" in t for t in tags)


def test_strips_markdown_formatting():
    analyzer = _make_analyzer()
    content = "**特朗普** 访问 `北京`"
    tags = analyzer._analyze_keywords_textrank(content)
    assert not any("*" in t or "`" in t for t in tags)


def test_empty_content_returns_empty():
    analyzer = _make_analyzer()
    assert analyzer._analyze_keywords_textrank("") == []


def test_short_content_returns_empty():
    analyzer = _make_analyzer()
    assert analyzer._analyze_keywords_textrank("短。") == []
    assert analyzer._analyze_keywords_textrank("今天天气不错。明天可能下雨。") == []


def test_whitespace_only_returns_empty():
    analyzer = _make_analyzer()
    assert analyzer._analyze_keywords_textrank("   \n  \t  ") == []


def test_extracts_from_chinese_news():
    analyzer = _make_analyzer()
    content = (
        "美国前总统特朗普在G7峰会期间与日本首相高市早苗会谈，"
        "双方讨论了贸易和安全议题。特朗普表示将继续推动双边合作，"
        "高市早苗则强调日本在亚太地区的战略地位。"
        "此次会谈持续了约两小时，会后双方发表了联合声明。"
    )
    tags = analyzer._analyze_keywords_textrank(content)
    assert 1 <= len(tags) <= 5
    assert all(isinstance(t, str) and len(t) > 0 for t in tags)


def test_topk_respected():
    analyzer = _make_analyzer()
    content = (
        "美国前总统特朗普在G7峰会期间与日本首相高市早苗会谈，"
        "双方讨论了贸易和安全议题。会谈持续约两小时。"
    )
    tags = analyzer._analyze_keywords_textrank(content, topk=3)
    assert len(tags) <= 3


def test_no_duplicate_tags():
    analyzer = _make_analyzer()
    content = (
        "美国前总统特朗普在G7峰会期间与日本首相高市早苗会谈，"
        "双方讨论了贸易和安全议题。" * 3
    )
    tags = analyzer._analyze_keywords_textrank(content)
    assert len(tags) == len(set(tags))


# ── _extract_keywords (TF-IDF 优先, TextRank 兜底) ────────────────

def test_extract_keywords_no_db_falls_back_to_textrank(monkeypatch):
    """无数据库时 _extract_keywords 回退到 TextRank"""
    analyzer = _make_analyzer(db=None)
    content = (
        "美国前总统特朗普在G7峰会期间与日本首相高市早苗会谈，"
        "双方讨论了贸易和安全议题。特朗普表示将继续推动双边合作。"
        "会谈持续约两小时，会后双方发表了联合声明。"
    )
    tags = analyzer._extract_keywords(content)
    assert 1 <= len(tags) <= 5
    assert all(isinstance(t, str) and len(t) > 0 for t in tags)


def test_extract_keywords_filters_generic_words():
    """默认 jieba IDF 应自动压低通用词权重，关键词不包含"公司"等泛化词"""
    analyzer = _make_analyzer(db=None)
    content = (
        "美国前总统特朗普在G7峰会期间与日本首相高市早苗会谈，"
        "这家公司的主要项目涉及多个企业的合作。"
        "双方讨论了贸易和安全议题。会谈持续约两小时。"
    )
    tags = analyzer._extract_keywords(content)
    assert len(tags) >= 1
    # "公司""企业""项目" 在几乎所有文档中出现，默认 IDF 应压低
    generic = {"公司", "企业", "项目"}
    assert not any(t in generic for t in tags), (
        f"Generic words should be filtered by IDF, got: {tags}"
    )


def test_extract_keywords_short_content_returns_empty():
    """正文 < 50 字符返回空列表"""
    analyzer = _make_analyzer()
    assert analyzer._extract_keywords("短。") == []


# ── analyze_keywords(items) 批量接口 ────────────────────────────────

def test_analyze_keywords_batch_sets_tags_on_items():
    """analyze_keywords(items) 原地设置每个 item 的 tags"""
    analyzer = _make_analyzer()
    items = [
        {
            "content": (
                "美国前总统特朗普在G7峰会期间与日本首相高市早苗会谈，"
                "双方讨论了贸易和安全议题。会谈持续约两小时，会后双方发表了联合声明。"
            ),
            "tags": [],
        },
        {"content": "短。", "tags": []},
    ]
    analyzer.analyze_keywords(items)
    assert len(items[0]["tags"]) >= 1
    assert items[1]["tags"] == []


# ── 集成：_download_and_parse preserves parser tags ─────────────────

def test_download_and_parse_preserves_meta_tags_when_present(monkeypatch):
    """当 trafilatura 有 tags 时，保留原始 tags"""
    import storage.s3
    monkeypatch.setattr(storage.s3.S3Client, "_ensure_bucket", lambda self: None)
    from news.crawler import Crawler
    import news.crawler as crawler_mod

    class FakeParserRegistry:
        def parse(self, source_id, html, url=""):
            return {
                "markdown": "正文内容。",
                "title": "测试",
                "author": "",
                "published_at": "",
                "summary": "",
                "category": "",
                "tags": ["编辑标注", "原创"],
            }

    monkeypatch.setattr(crawler_mod, "parser", FakeParserRegistry())
    crawler = Crawler(_test_config())

    class FakeResp:
        text = "<html></html>"
        def raise_for_status(self):
            pass

    monkeypatch.setattr(crawler.session(), "get", lambda url, timeout, headers=None: FakeResp())

    item = {"url": "https://example.com/news/2", "source_id": "test"}
    ok = crawler._download_and_parse(item)
    assert ok is True
    assert item["tags"] == ["编辑标注", "原创"]


def test_download_and_parse_empty_tags_stays_empty(monkeypatch):
    """parser 返回空 tags 时保持空列表，keywords 由上层 fetch_all 统一处理"""
    import storage.s3
    monkeypatch.setattr(storage.s3.S3Client, "_ensure_bucket", lambda self: None)
    from news.crawler import Crawler
    import news.crawler as crawler_mod

    class FakeParserRegistry:
        def parse(self, source_id, html, url=""):
            return {
                "markdown": (
                    "美国前总统特朗普在G7峰会期间与日本首相高市早苗会谈，"
                    "双方讨论了贸易和安全议题。特朗普表示将继续推动双边合作。"
                    "会谈持续约两小时，会后双方发表了联合声明。"
                ),
                "title": "测试标题",
                "author": "",
                "published_at": "",
                "summary": "",
                "category": "",
                "tags": [],  # 无 tags → 不再在 _download_and_parse 中 fallback
            }

    monkeypatch.setattr(crawler_mod, "parser", FakeParserRegistry())
    crawler = Crawler(_test_config())

    class FakeResp:
        text = "<html></html>"
        def raise_for_status(self):
            pass

    monkeypatch.setattr(crawler.session(), "get", lambda url, timeout, headers=None: FakeResp())

    item = {"url": "https://example.com/news/1", "source_id": "test"}
    ok = crawler._download_and_parse(item)
    assert ok is True
    assert item["tags"] == []  # keywords 由 fetch_all 的 analyze_keywords(items) 统一处理


def test_download_and_parse_short_content_preserves_tags(monkeypatch):
    """正文短时 tags 保持 parser 返回的原值"""
    import storage.s3
    monkeypatch.setattr(storage.s3.S3Client, "_ensure_bucket", lambda self: None)
    from news.crawler import Crawler
    import news.crawler as crawler_mod

    class FakeParserRegistry:
        def parse(self, source_id, html, url=""):
            return {
                "markdown": "短。",
                "title": "测试",
                "author": "",
                "published_at": "",
                "summary": "",
                "category": "",
                "tags": [],
            }

    monkeypatch.setattr(crawler_mod, "parser", FakeParserRegistry())
    crawler = Crawler(_test_config())

    class FakeResp:
        text = "<html></html>"
        def raise_for_status(self):
            pass

    monkeypatch.setattr(crawler.session(), "get", lambda url, timeout, headers=None: FakeResp())

    item = {"url": "https://example.com/news/3", "source_id": "test"}
    ok = crawler._download_and_parse(item)
    assert ok is True
    assert item["tags"] == []
