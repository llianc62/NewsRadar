"""Tests for ThepaperParser — __NEXT_DATA__ extraction."""
import pytest
from pathlib import Path
from news.parser.sites.thepaper import ThepaperParser


FIXTURES = Path(__file__).parent / "fixtures"


class TestThepaperParser:
    def test_extracts_content_from_fixture(self):
        html = (FIXTURES / "thepaper.html").read_text(encoding="utf-8")
        parser = ThepaperParser()
        content_html, meta = parser._extract(html, url="https://www.thepaper.cn/")
        assert content_html
        assert len(content_html) > 200
        assert meta["title"]

    def test_extracts_content_from_minimal_fixture(self):
        """Minimal fixture — verifies core extraction works without network."""
        import json
        content = "<p>" + "测试正文内容。" * 30 + "</p>"
        data = {
            "props": {
                "pageProps": {
                    "article": {
                        "title": "测试标题",
                        "content": content,
                        "keywords": ["时政", "经济"],
                        "datePublished": "2026-06-24",
                    }
                }
            }
        }
        html = '<script id="__NEXT_DATA__" type="application/json">' + json.dumps(data, ensure_ascii=False) + '</script>'
        parser = ThepaperParser()
        content_html, meta = parser._extract(html)
        assert content_html
        assert meta["title"] == "测试标题"
        assert "测试正文内容" in content_html
        assert meta["tags"] == ["时政", "经济"]

    def test_returns_none_for_unrelated_html(self):
        parser = ThepaperParser()
        html = "<html><body><p>no next data here</p></body></html>"
        content_html, meta = parser._extract(html)
        assert content_html == html
        assert meta == {}
