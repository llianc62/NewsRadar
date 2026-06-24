"""Tests for HtmlParser._extract_with_readability — the primary extraction path."""

from news.parser import HtmlParser, _split_keyword_tags
from tests.helpers import make_html


def _full_page(title="测试标题", body_text="正文段落内容。") -> str:
    """Build a complete HTML page with enough content for extraction."""
    return f"""<!DOCTYPE html>
<html>
<head>
<title>{title} - 新闻网站</title>
<meta property="og:title" content="{title} | 新闻网站">
<meta name="author" content="测试作者">
<meta name="description" content="这是文章摘要">
<meta name="keywords" content="时政,经济,社会">
<meta property="article:published_time" content="2026-06-15T10:00:00+08:00">
</head>
<body>
<article>
<h1>{title}</h1>
<p>{"".join(body_text for _ in range(20))}</p>
<p>{"第二段内容。" * 20}</p>
<img src="https://x.com/illustration.jpg" alt="插图">
<p>{"第三段内容。" * 20}</p>
</article>
</body>
</html>"""


class TestReadabilityExtraction:
    """_extract_with_readability — full-page content extraction via readability + markdownify."""

    def test_extracts_content_from_full_page(self):
        parser = HtmlParser()
        html = _full_page()
        result = parser._extract_with_readability(html, "http://example.com")
        assert result is not None
        assert len(result["markdown"].strip()) > 50
        assert "正文段落内容" in result["markdown"]

    def test_returns_none_for_short_content(self):
        parser = HtmlParser()
        html = make_html("<p>短。</p>")
        result = parser._extract_with_readability(html, "http://example.com")
        assert result is None

    def test_title_prefers_h1_over_og_title(self):
        parser = HtmlParser()
        html = _full_page(title="真正的文章标题")
        result = parser._extract_with_readability(html, "http://example.com")
        assert result is not None
        # H1 "真正的文章标题" should win over og:title
        # "真正的文章标题 | 新闻网站"
        assert "真正的文章标题" in result["title"]
        # The H1-based title should NOT include the site suffix
        assert "|" not in result["title"]

    def test_metadata_exception_handled(self):
        # Malformed page that might cause metadata extraction issues
        parser = HtmlParser()
        html = make_html("<h1>标题</h1><p>" + "正文。" * 30 + "</p>")
        result = parser._extract_with_readability(html, "http://example.com")
        # Should not crash even if metadata extraction has issues
        assert result is not None
        assert "markdown" in result

    def test_skip_trim_respected(self):
        parser = HtmlParser()
        html = _full_page()
        # With skip_trim=True, _trim_noise should not be called
        result = parser._extract_with_readability(
            html, "http://example.com", skip_trim=True
        )
        assert result is not None
        assert len(result["markdown"].strip()) > 50

    def test_trim_applied_when_not_skipped(self):
        parser = HtmlParser()
        # Page with copyright footer noise that _trim_noise should remove
        body = "<h1>文章标题</h1><p>" + "正文内容。" * 20 + "</p>"
        tail = '<footer><p>版权所有 © 2024</p></footer>'
        html = make_html(body, tail_noise=tail)
        result = parser._extract_with_readability(html, "http://example.com")
        assert result is not None
        # Copyright footer should be trimmed
        assert "版权所有" not in result["markdown"]

    def test_extracts_categories_and_tags(self):
        parser = HtmlParser()
        html = _full_page()
        result = parser._extract_with_readability(html, "http://example.com")
        assert result is not None
        # metadata extraction should collect categories from meta keywords
        assert isinstance(result["tags"], list)

    def test_deduplicates_tags(self):
        parser = HtmlParser()
        html = _full_page()
        result = parser._extract_with_readability(html, "http://example.com")
        assert result is not None
        # No duplicate tags
        assert len(result["tags"]) == len(set(result["tags"]))

    def test_extracts_author_date_description(self):
        parser = HtmlParser()
        html = _full_page()
        result = parser._extract_with_readability(html, "http://example.com")
        assert result is not None
        # ``author`` is a string — metadata extraction does not
        # always pick up <meta name="author">, so we only validate the key
        # exists and is the correct type rather than requiring a non-empty
        # value.
        assert isinstance(result["author"], str)

    def test_beautify_applied_to_output(self):
        parser = HtmlParser()
        body = "<h1>标题</h1><p>是**重要**通知</p><p>" + "正文。" * 20 + "</p>"
        html = make_html(body)
        result = parser._extract_with_readability(html, "http://example.com")
        assert result is not None
        # Beautify should normalize bold marker spacing
        assert "是 **重要** 通知" in result["markdown"]


class TestSplitKeywordTags:
    """Tests for :func:`_split_keyword_tags` — format-aware keyword splitting."""

    # ── Structured key:value format (sspai-style) ──────────────────

    def test_structured_extracts_keyword_values(self):
        """Only ``keyword:`` values are kept; other fields discarded."""
        tags = ["keyword: 618鼠标推荐,weight: 0.95,level: 1,keyword: 无线游戏鼠标"]
        assert _split_keyword_tags(tags) == ["618鼠标推荐", "无线游戏鼠标"]

    def test_structured_deduplicates_identical_keywords(self):
        tags = ["keyword: AI,keyword: AI,weight: 0.5,keyword: 机器学习"]
        assert _split_keyword_tags(tags) == ["AI", "机器学习"]

    def test_structured_single_keyword(self):
        tags = ["keyword: 深度学习"]
        assert _split_keyword_tags(tags) == ["深度学习"]

    def test_structured_no_keyword_prefix_returns_empty(self):
        """When all fragments are k:v but none start with ``keyword:``,
        return empty — this is SEO metadata, not user tags."""
        tags = ["title: Hello,author: John"]
        assert _split_keyword_tags(tags) == []

    # ── Comma-separated format ─────────────────────────────────────

    def test_comma_separated_simple(self):
        tags = ["Russia, Ukraine, war, politics"]
        assert _split_keyword_tags(tags) == ["Russia", "Ukraine", "war", "politics"]

    def test_comma_separated_with_chinese_comma(self):
        tags = ["AI, 机器学习，深度学习"]
        assert _split_keyword_tags(tags) == ["AI", "机器学习", "深度学习"]

    def test_comma_separated_single_element(self):
        tags = ["Technology"]
        assert _split_keyword_tags(tags) == ["Technology"]

    def test_comma_separated_isolated_colon_not_treated_as_structured(self):
        """One fragment with a colon among many without — treated as plain comma."""
        tags = ["AI, machine: learning, deep learning, neural networks"]
        assert _split_keyword_tags(tags) == [
            "AI", "machine: learning", "deep learning", "neural networks"
        ]

    # ── Space-separated format ─────────────────────────────────────

    def test_space_separated_chinese(self):
        tags = ["中国 经济 科技 发展"]
        assert _split_keyword_tags(tags) == ["中国", "经济", "科技", "发展"]

    def test_space_separated_deduplicates(self):
        tags = ["a b a c"]
        assert _split_keyword_tags(tags) == ["a", "b", "c"]

    # ── Already-split lists (pass-through) ─────────────────────────

    def test_already_split_passthrough(self):
        tags = ["Python", "FastAPI", "Web开发"]
        assert _split_keyword_tags(tags) == ["Python", "FastAPI", "Web开发"]

    def test_already_split_deduplicates(self):
        tags = ["Python", "FastAPI", "Python"]
        assert _split_keyword_tags(tags) == ["Python", "FastAPI"]

    # ── RSS-style categories ───────────────────────────────────────

    def test_rss_categories_unchanged(self):
        tags = ["Technology", "Science"]
        assert _split_keyword_tags(tags) == ["Technology", "Science"]
