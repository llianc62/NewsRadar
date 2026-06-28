"""Tests for YicaiParser."""
import pytest
from news.parser.sites.yicai import YicaiParser


class TestYicaiParser:
    def test_parse_empty_returns_none(self):
        parser = YicaiParser()
        assert parser.parse("") is None

    def test_splits_chinese_semicolon_keywords(self):
        """meta keywords 用 ； 分隔时应被正确拆分为独立标签。"""
        html = """<html><head>
<meta name="keywords" content="全国统一大市场；反内卷；财政补贴；公平竞争；政府采购">
<title>建设全国统一大市场迎新部署</title>
</head><body>""" + "<p>test content. " * 30 + "</p></body></html>"
        parser = YicaiParser()
        result = parser.parse(html)
        assert result is not None
        assert result["tags"] == [
            "全国统一大市场",
            "反内卷",
            "财政补贴",
            "公平竞争",
            "政府采购",
        ]

    def test_tags_deduplicated_after_split(self):
        """拆分后重复标签应去重。"""
        html = """<html><head>
<meta name="keywords" content="AI；AI；半导体；AI">
<title>测试</title>
</head><body>""" + "<p>test content. " * 30 + "</p></body></html>"
        parser = YicaiParser()
        result = parser.parse(html)
        assert result is not None
        assert result["tags"] == ["AI", "半导体"]

    def test_semicolons_do_not_leave_empty_tags(self):
        """连续 ； 不产生空标签。"""
        html = """<html><head>
<meta name="keywords" content="AI；；半导体；">
<title>测试</title>
</head><body>""" + "<p>test content. " * 30 + "</p></body></html>"
        parser = YicaiParser()
        result = parser.parse(html)
        assert result is not None
        assert result["tags"] == ["AI", "半导体"]

    def test_no_keywords_meta_still_works(self):
        """没有 keywords meta 标签时也能正常解析。"""
        html = """<html><head>
<title>普通文章</title>
</head><body>""" + "<p>test content. " * 30 + "</p></body></html>"
        parser = YicaiParser()
        result = parser.parse(html)
        assert result is not None
        assert result["tags"] == []

    def test_parse_trivial_html(self):
        html = "<html><head><title>第一财经</title></head><body>" + "<p>test " * 30 + "</p></body></html>"
        parser = YicaiParser()
        result = parser.parse(html)
        assert result is not None
        assert len(result["markdown"]) > 50
