"""Tests for ClsParser — 财联社 (cls.cn) Next.js __NEXT_DATA__ extraction."""

import json
from pathlib import Path

from news.parser.sites.cls import ClsParser

FIXTURES = Path(__file__).parent / "fixtures"


class TestClsParser:
    """Test suite for ClsParser."""

    def test_extracts_content_from_real_fixture(self):
        """Real cls.cn article fixture — verifies full extraction pipeline."""
        html = (FIXTURES / "cls.html").read_text(encoding="utf-8")
        parser = ClsParser()
        result = parser._extract(html)
        assert result is not None
        assert len(result["markdown"]) > 200
        assert result["title"] == "工信部总工程师：加强新一代通信网和算力网规划建设"
        # Author extracted from Next.js data
        assert result["author"] == "澎湃新闻"
        # Verify content is meaningful (check for key content snippets)
        assert "新一代信息通信技术" in result["markdown"]
        assert "6G" in result["markdown"]
        # Published_at from ctime (Unix timestamp)
        assert result["published_at"] == "2026-06-24T07:17:17+00:00"
        # Tags from subject array
        assert "算力" in result["tags"]
        # Summary from brief
        assert result["summary"]

    def test_returns_none_no_next_data(self):
        """Negative test — no __NEXT_DATA__ present."""
        parser = ClsParser()
        result = parser._extract("<html><body><p>no data here</p></body></html>")
        assert result is None

    def test_returns_none_short_content(self):
        """Negative test — __NEXT_DATA__ present but content too short."""
        next_data = {
            "props": {
                "pageProps": {
                    "articleDetail": {
                        "title": "短标题",
                        "content": "<p>短</p>",
                    }
                }
            }
        }
        html = (
            '<html><head></head><body>'
            '<script id="__NEXT_DATA__" type="application/json">'
            + json.dumps(next_data, ensure_ascii=False)
            + '</script>'
            '</body></html>'
        )
        parser = ClsParser()
        result = parser._extract(html)
        assert result is None

    def test_find_next_data_extracts_json(self):
        """_find_next_data — extracts JSON from __NEXT_DATA__ script tag."""
        test_data = {"props": {"pageProps": {"test": "value"}}}
        html = (
            '<html><script id="__NEXT_DATA__" type="application/json">'
            + json.dumps(test_data)
            + '</script></html>'
        )
        result = ClsParser._find_next_data(html)
        assert result is not None
        assert result["props"]["pageProps"]["test"] == "value"

    def test_find_next_data_returns_none_when_missing(self):
        """_find_next_data — returns None when no __NEXT_DATA__ script tag."""
        result = ClsParser._find_next_data("<html><body></body></html>")
        assert result is None

    def test_find_article_detail_extracts_article(self):
        """_find_article_detail — extracts articleDetail from Next.js data."""
        data = {
            "props": {
                "pageProps": {
                    "articleDetail": {
                        "title": "测试文章",
                        "content": "<p>测试内容</p>",
                    }
                }
            }
        }
        article = ClsParser._find_article_detail(data)
        assert article is not None
        assert article["title"] == "测试文章"
        assert article["content"] == "<p>测试内容</p>"

    def test_find_article_detail_returns_none_when_missing(self):
        """_find_article_detail — returns None when structure is wrong."""
        result = ClsParser._find_article_detail({"other": "data"})
        assert result is None

    def test_extracts_content_via_full_parse_pipeline(self):
        """Full parse() pipeline — _extract succeeds, no readability fallback needed."""
        html = (FIXTURES / "cls.html").read_text(encoding="utf-8")
        parser = ClsParser()
        result = parser.parse(html, url="https://www.cls.cn/detail/2407859")
        assert result is not None
        assert len(result["markdown"]) > 200
        assert result["title"] == "工信部总工程师：加强新一代通信网和算力网规划建设"
        assert result["author"] == "澎湃新闻"
