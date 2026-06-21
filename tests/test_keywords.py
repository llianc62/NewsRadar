"""Tests for jieba TF-IDF / TextRank keyword extraction fallback."""

import os
import tempfile
import pytest


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


# ── _clean_markdown 单元测试 ───────────────────────────────────────

def test_clean_markdown_strips_images():
    from news.crawler import clean_markdown
    text = clean_markdown("特朗普 ![图片](https://example.com/img.png) 访问北京")
    assert "example.com" not in text
    assert "img.png" not in text


def test_clean_markdown_strips_links():
    from news.crawler import clean_markdown
    text = clean_markdown("[特朗普](https://example.com/trump) 访问北京")
    assert "example.com" not in text
    assert "特朗普" in text


def test_clean_markdown_strips_formatting():
    from news.crawler import clean_markdown
    text = clean_markdown("**特朗普** 访问 `北京`")
    assert "*" not in text
    assert "`" not in text


# ── _extract_keywords_textrank 单元测试 ────────────────────────────

def test_strips_markdown_images():
    from news.crawler import extract_keywords_textrank
    content = "特朗普 ![图片](https://example.com/img.png) 访问北京"
    tags = extract_keywords_textrank(content)
    assert not any("example" in t or "http" in t or "img" in t for t in tags)


def test_strips_markdown_links():
    from news.crawler import extract_keywords_textrank
    content = "[特朗普](https://example.com/trump) 访问北京"
    tags = extract_keywords_textrank(content)
    assert not any("example" in t or "http" in t for t in tags)


def test_strips_markdown_formatting():
    from news.crawler import extract_keywords_textrank
    content = "**特朗普** 访问 `北京`"
    tags = extract_keywords_textrank(content)
    assert not any("*" in t or "`" in t for t in tags)


def test_empty_content_returns_empty():
    from news.crawler import extract_keywords_textrank
    assert extract_keywords_textrank("") == []


def test_short_content_returns_empty():
    from news.crawler import extract_keywords_textrank
    assert extract_keywords_textrank("短。") == []
    assert extract_keywords_textrank("今天天气不错。明天可能下雨。") == []


def test_whitespace_only_returns_empty():
    from news.crawler import extract_keywords_textrank
    assert extract_keywords_textrank("   \n  \t  ") == []


def test_extracts_from_chinese_news():
    from news.crawler import extract_keywords_textrank
    content = (
        "美国前总统特朗普在G7峰会期间与日本首相高市早苗会谈，"
        "双方讨论了贸易和安全议题。特朗普表示将继续推动双边合作，"
        "高市早苗则强调日本在亚太地区的战略地位。"
        "此次会谈持续了约两小时，会后双方发表了联合声明。"
    )
    tags = extract_keywords_textrank(content)
    assert 1 <= len(tags) <= 5
    assert all(isinstance(t, str) and len(t) > 0 for t in tags)


def test_topk_respected():
    from news.crawler import extract_keywords_textrank
    content = (
        "美国前总统特朗普在G7峰会期间与日本首相高市早苗会谈，"
        "双方讨论了贸易和安全议题。会谈持续约两小时。"
    )
    tags = extract_keywords_textrank(content, topk=3)
    assert len(tags) <= 3


def test_no_duplicate_tags():
    from news.crawler import extract_keywords_textrank
    content = (
        "美国前总统特朗普在G7峰会期间与日本首相高市早苗会谈，"
        "双方讨论了贸易和安全议题。" * 3
    )
    tags = extract_keywords_textrank(content)
    assert len(tags) == len(set(tags))


# ── Crawler._extract_keywords (TF-IDF 优先, TextRank 兜底) ────────

def test_crawler_extract_keywords_no_db_falls_back_to_textrank(monkeypatch):
    """无数据库时 _extract_keywords 回退到 TextRank"""
    import storage.s3
    monkeypatch.setattr(storage.s3.S3Client, "_ensure_bucket", lambda self: None)
    from news.crawler import Crawler

    crawler = Crawler(_test_config())
    content = (
        "美国前总统特朗普在G7峰会期间与日本首相高市早苗会谈，"
        "双方讨论了贸易和安全议题。特朗普表示将继续推动双边合作。"
        "会谈持续约两小时，会后双方发表了联合声明。"
    )
    tags = crawler._extract_keywords(content)
    assert 1 <= len(tags) <= 5
    assert all(isinstance(t, str) and len(t) > 0 for t in tags)


def test_crawler_extract_keywords_with_idf_file(monkeypatch, tmp_path):
    """有 IDF 文件时优先使用 TF-IDF"""
    import storage.s3
    monkeypatch.setattr(storage.s3.S3Client, "_ensure_bucket", lambda self: None)
    from news.crawler import Crawler, _IDF_PATH

    # 构造一个 IDF 文件，使 "公司" 的 IDF 极低（通用词）
    idf_content = """特朗普 5.0
会谈 4.5
G7 5.5
公司 0.01
企业 0.01
项目 0.01
"""
    idf_file = tmp_path / "jieba_idf.txt"
    idf_file.write_text(idf_content, encoding="utf-8")

    # 临时替换 _IDF_PATH
    monkeypatch.setattr(
        "news.crawler._IDF_PATH", str(idf_file),
    )

    crawler = Crawler(_test_config())
    content = (
        "美国前总统特朗普在G7峰会期间与日本首相高市早苗会谈，"
        "这家公司的主要项目涉及多个企业的合作。"
        "双方讨论了贸易和安全议题。会谈持续约两小时。"
    )
    tags = crawler._extract_keywords(content)
    assert len(tags) >= 1
    # "公司""企业""项目" 的 IDF 极低，不应被选为关键词
    generic = {"公司", "企业", "项目"}
    assert not any(t in generic for t in tags), (
        f"Generic words should be filtered by IDF, got: {tags}"
    )


def test_crawler_extract_keywords_short_content_returns_empty(monkeypatch):
    """正文 < 50 字符返回空列表"""
    import storage.s3
    monkeypatch.setattr(storage.s3.S3Client, "_ensure_bucket", lambda self: None)
    from news.crawler import Crawler

    crawler = Crawler(_test_config())
    assert crawler._extract_keywords("短。") == []


# ── 集成：_download_and_parse tags fallback ──────────────────────

def test_download_and_parse_falls_back_to_textrank_when_no_meta_tags(monkeypatch):
    """当 trafilatura 无 tags 时，用 jieba TextRank 填补"""
    import storage.s3
    monkeypatch.setattr(storage.s3.S3Client, "_ensure_bucket", lambda self: None)
    from news.crawler import Crawler

    class FakeParser:
        def parse(self, html, url=""):
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
                "tags": [],  # 无 tags → 触发 fallback
            }

    crawler = Crawler(_test_config(), parser=FakeParser())

    class FakeResp:
        text = "<html></html>"
        def raise_for_status(self):
            pass

    monkeypatch.setattr(crawler.session(), "get", lambda url, timeout: FakeResp())

    item = {"url": "https://example.com/news/1"}
    ok = crawler._download_and_parse(item)
    assert ok is True
    assert len(item["tags"]) >= 1
    assert len(item["tags"]) <= 5
    assert all(isinstance(t, str) for t in item["tags"])


def test_download_and_parse_preserves_meta_tags_when_present(monkeypatch):
    """当 trafilatura 有 tags 时，保留原始 tags，不覆盖"""
    import storage.s3
    monkeypatch.setattr(storage.s3.S3Client, "_ensure_bucket", lambda self: None)
    from news.crawler import Crawler

    class FakeParser:
        def parse(self, html, url=""):
            return {
                "markdown": "正文内容。",
                "title": "测试",
                "author": "",
                "published_at": "",
                "summary": "",
                "category": "",
                "tags": ["编辑标注", "原创"],
            }

    crawler = Crawler(_test_config(), parser=FakeParser())

    class FakeResp:
        text = "<html></html>"
        def raise_for_status(self):
            pass

    monkeypatch.setattr(crawler.session(), "get", lambda url, timeout: FakeResp())

    item = {"url": "https://example.com/news/2"}
    ok = crawler._download_and_parse(item)
    assert ok is True
    assert item["tags"] == ["编辑标注", "原创"]


def test_download_and_parse_short_content_no_fallback(monkeypatch):
    """正文 < 50 字符时，无 meta tags 也不会生成 NLP tags"""
    import storage.s3
    monkeypatch.setattr(storage.s3.S3Client, "_ensure_bucket", lambda self: None)
    from news.crawler import Crawler

    class FakeParser:
        def parse(self, html, url=""):
            return {
                "markdown": "短。",
                "title": "测试",
                "author": "",
                "published_at": "",
                "summary": "",
                "category": "",
                "tags": [],
            }

    crawler = Crawler(_test_config(), parser=FakeParser())

    class FakeResp:
        text = "<html></html>"
        def raise_for_status(self):
            pass

    monkeypatch.setattr(crawler.session(), "get", lambda url, timeout: FakeResp())

    item = {"url": "https://example.com/news/3"}
    ok = crawler._download_and_parse(item)
    assert ok is True
    assert item["tags"] == []
