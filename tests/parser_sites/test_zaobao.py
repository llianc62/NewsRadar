"""Tests for ZaobaoParser — 联合早报 (zaobao.com.sg) JSON-LD + articleBody extraction."""

import json
from pathlib import Path

from news.parser.sites.zaobao import ZaobaoParser

FIXTURES = Path(__file__).parent / "fixtures"


class TestZaobaoParser:
    """Test suite for ZaobaoParser."""

    def test_extracts_content_from_real_fixture(self):
        """Real zaobao.com.sg article fixture — verifies full extraction pipeline."""
        html = (FIXTURES / "zaobao.html").read_text(encoding="utf-8")
        parser = ZaobaoParser()
        content_html, meta = parser._extract(html)
        assert content_html
        assert len(content_html) > 200
        # Author from JSON-LD
        assert meta["author"] == "李庚洧"
        # Published_at from JSON-LD
        assert meta["published_at"] == "2026-01-16T06:05:34.000Z"
        # Title from JSON-LD headline
        assert "亚细安推区域反诈指南" in meta["title"]

    def test_returns_none_no_article_body(self):
        """No div.articleBody present — _extract returns original HTML with empty meta."""
        parser = ZaobaoParser()
        html = "<html><body><p>no article here</p></body></html>"
        content_html, meta = parser._extract(html)
        assert content_html == html
        assert meta == {}

    def test_returns_none_short_content(self):
        """articleBody present but content too short — _extract returns original HTML."""
        html = '<html><body><div class="articleBody"><p>短</p></div></body></html>'
        parser = ZaobaoParser()
        content_html, meta = parser._extract(html)
        assert content_html == html
        assert meta == {}

    def test_find_article_body_found(self):
        """_find_article_body — finds div.articleBody and returns its HTML."""
        html = '<html><body><div class="articleBody"><p>测试正文内容段落。</p></div></body></html>'
        result = ZaobaoParser._find_article_body(html)
        assert result is not None
        assert "articleBody" in result
        assert "测试正文内容段落" in result

    def test_find_article_body_not_found(self):
        """_find_article_body — returns empty string when not found."""
        result = ZaobaoParser._find_article_body("<html><body></body></html>")
        assert result == ""

    def test_find_jsonld_meta_extracts_all_fields(self):
        """_find_jsonld_meta — extracts title, author, published_at from JSON-LD."""
        ld = {
            "@context": "https://schema.org",
            "@graph": [
                {
                    "@type": "NewsArticle",
                    "headline": "测试文章标题",
                    "name": "测试文章标题",
                    "datePublished": "2026-01-16T06:05:34.000Z",
                    "author": [
                        {"@type": "Person", "name": "张三"}
                    ],
                    "description": "这是一篇测试文章",
                }
            ]
        }
        html = (
            '<html><head>'
            '<script type="application/ld+json">'
            + json.dumps(ld, ensure_ascii=False)
            + '</script>'
            '</head><body></body></html>'
        )
        meta = ZaobaoParser._find_jsonld_meta(html)
        assert meta["title"] == "测试文章标题"
        assert meta["author"] == "张三"
        assert meta["published_at"] == "2026-01-16T06:05:34.000Z"
        assert meta["summary"] == "这是一篇测试文章"

    def test_find_jsonld_meta_returns_empty_on_no_jsonld(self):
        """_find_jsonld_meta — returns empty dict when no JSON-LD."""
        meta = ZaobaoParser._find_jsonld_meta("<html><body></body></html>")
        assert meta["title"] == ""
        assert meta["author"] == ""
        assert meta["published_at"] == ""

    def test_find_jsonld_meta_handles_single_object(self):
        """_find_jsonld_meta — handles JSON-LD without @graph wrapper."""
        ld = {
            "@context": "https://schema.org",
            "@type": "NewsArticle",
            "headline": "单对象标题",
            "datePublished": "2026-03-01T08:00:00.000Z",
            "author": {"@type": "Person", "name": "李四"},
        }
        html = (
            '<html><head>'
            '<script type="application/ld+json">'
            + json.dumps(ld, ensure_ascii=False)
            + '</script>'
            '</head><body></body></html>'
        )
        meta = ZaobaoParser._find_jsonld_meta(html)
        assert meta["title"] == "单对象标题"
        assert meta["author"] == "李四"
        assert meta["published_at"] == "2026-03-01T08:00:00.000Z"

    def test_full_parse_pipeline(self):
        """Full parse() pipeline — _extract succeeds, no readability fallback needed."""
        html = (FIXTURES / "zaobao.html").read_text(encoding="utf-8")
        parser = ZaobaoParser()
        result = parser.parse(html, url="https://www.zaobao.com.sg/realtime/singapore/story20260116-8110467")
        assert result is not None
        assert len(result["markdown"]) > 200
        assert result["author"] == "李庚洧"
