"""Tests for ThepaperParser — __NEXT_DATA__ extraction."""
import pytest
from pathlib import Path
from news.parser.sites.thepaper import ThepaperParser


FIXTURES = Path(__file__).parent / "fixtures"


class TestThepaperParser:
    def test_extracts_content_from_fixture(self):
        html = (FIXTURES / "thepaper.html").read_text(encoding="utf-8")
        parser = ThepaperParser()
        result = parser._extract(html, url="https://www.thepaper.cn/")
        assert result is not None
        assert len(result["markdown"]) > 200
        assert result["title"]

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
        result = parser._extract(html)
        assert result is not None
        assert result["title"] == "测试标题"
        assert "测试正文内容" in result["markdown"]
        assert result["tags"] == ["时政", "经济"]

    def test_returns_none_for_unrelated_html(self):
        parser = ThepaperParser()
        result = parser._extract("<html><body><p>no next data here</p></body></html>")
        assert result is None
