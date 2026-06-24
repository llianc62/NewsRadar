"""Tests for WallstreetcnParser — __SSR__ JSON extraction."""

import json
from pathlib import Path

from news.parser.sites.wallstreetcn import WallstreetcnParser

FIXTURES = Path(__file__).parent / "fixtures"


class TestWallstreetcnParser:
    """Test suite for WallstreetcnParser."""

    def test_extracts_content_from_real_fixture(self):
        """Real wallstreetcn article fixture — verifies full extraction pipeline."""
        html = (FIXTURES / "wallstreetcn.html").read_text(encoding="utf-8")
        parser = WallstreetcnParser()
        result = parser._extract(html)
        assert result is not None
        assert len(result["markdown"]) > 200
        assert result["title"] == "赣锋锂业签署固态电池研发及产业化基地合作协议"
        assert result["author"] == "袁佳颖"
        assert len(result["markdown"]) > 300
        # Check that categories are used as tags
        assert len(result["tags"]) >= 2
        assert "A股公告" in result["tags"]

    def test_extracts_content_from_minimal_fixture(self):
        """Minimal __SSR__ fixture — verifies core extraction without network."""
        content_html = "<p>" + "测试正文内容。" * 30 + "</p>"
        article_data = {
            "title": "测试标题",
            "content": content_html,
            "author": {"display_name": "测试作者"},
            "display_time": 1745572478,
            "categories": [{"key": "finance", "name": "财经"}],
            "tags": ["A股", "公告"],
            "source_name": "测试来源",
            "words_count": 100,
        }
        ssr_data = {
            "state": {
                "default": {
                    "children": {
                        "default": {
                            "data": {
                                "id": 12345,
                                "article": article_data,
                            }
                        }
                    }
                }
            }
        }
        # Build HTML with __SSR__ JS variable assignment
        html = (
            "<html><head><title>测试标题</title></head><body>"
            "<div id='app'></div>"
            "<script>__SSR__ = "
            + json.dumps(ssr_data, ensure_ascii=False)
            + ";</script>"
            "</body></html>"
        )
        parser = WallstreetcnParser()
        result = parser._extract(html)
        assert result is not None
        assert result["title"] == "测试标题"
        assert result["author"] == "测试作者"
        assert "测试正文内容" in result["markdown"]
        assert len(result["tags"]) >= 2
        assert "财经" in result["tags"]
        assert result["published_at"] == "2025-04-25T09:14:38+00:00"

    def test_returns_none_for_unrelated_html(self):
        """Negative test — no __SSR__ → returns None."""
        parser = WallstreetcnParser()
        result = parser._extract("<html><body><p>no SSR data here</p></body></html>")
        assert result is None

    def test_returns_none_for_empty_content(self):
        """Negative test — __SSR__ present but content too short."""
        ssr_data = {
            "state": {
                "default": {
                    "children": {
                        "default": {
                            "data": {
                                "article": {
                                    "title": "短标题",
                                    "content": "<p>短</p>",
                                }
                            }
                        }
                    }
                }
            }
        }
        html = (
            "<html><body><script>__SSR__ = "
            + json.dumps(ssr_data, ensure_ascii=False)
            + ";</script></body></html>"
        )
        parser = WallstreetcnParser()
        result = parser._extract(html)
        assert result is None

    def test_unwrap_blockquote_images(self):
        """_unwrap_blockquote_images — unwraps <blockquote> around <img>."""
        html_input = (
            "<blockquote><img src='https://example.com/img.jpg' alt='test'></blockquote>"
        )
        result = WallstreetcnParser._unwrap_blockquote_images(html_input)
        assert "<blockquote>" not in result
        assert "<img" in result
        assert "src='https://example.com/img.jpg'" in result

    def test_fix_lazy_images(self):
        """_fix_lazy_images — converts data-src to src."""
        html_input = "<img data-src='https://example.com/img.jpg' src='placeholder.jpg'>"
        result = WallstreetcnParser._fix_lazy_images(html_input)
        assert "data-src" not in result
        assert "https://example.com/img.jpg" in result

    def test_wrap_bare_images(self):
        """_wrap_bare_images — wraps bare <img> in <p>."""
        html_input = "<img src='https://example.com/img.jpg'>"
        result = WallstreetcnParser._wrap_bare_images(html_input)
        assert "<p><img" in result

    def test_find_article_uses_fallback_recursive(self):
        """Fallback recursive search — different JSON structure."""
        data = {
            "pageData": {
                "article": {
                    "title": "Recursive标题",
                    "content": "<p>" + "递归正文。" * 30 + "</p>",
                }
            }
        }
        article = WallstreetcnParser._find_article(data)
        assert article is not None
        assert article["title"] == "Recursive标题"

    def test_display_time_zero(self):
        """display_time=0 should not crash and published_at should be empty."""
        ssr_data = {
            "state": {
                "default": {
                    "children": {
                        "default": {
                            "data": {
                                "article": {
                                    "title": "零时间",
                                    "content": "<p>" + "内容内容。" * 30 + "</p>",
                                    "display_time": 0,
                                    "author": {"display_name": "作者"},
                                    "categories": [],
                                    "tags": [],
                                }
                            }
                        }
                    }
                }
            }
        }
        html = (
            "<html><body><script>__SSR__ = "
            + json.dumps(ssr_data, ensure_ascii=False)
            + ";</script></body></html>"
        )
        parser = WallstreetcnParser()
        result = parser._extract(html)
        assert result is not None
        assert result["published_at"] == ""
