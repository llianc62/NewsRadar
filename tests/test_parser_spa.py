"""Tests for SPA data extraction: _find_json_candidates, _find_article_in_json,
and _extract_spa_data — the full SPA JSON → Markdown pipeline."""

import json
import re

from news.parser import HtmlParser


# ── Shared test HTML fixtures ──────────────────────────────────────

# Minimal Next.js script tag HTML for testing candidate discovery
_script_content = "<p>正文。" + "测试内容。" * 20 + "</p>"
NEXT_DATA_SCRIPT_HTML = """<!DOCTYPE html>
<html><head><title>Test</title></head><body>
<script id="__NEXT_DATA__" type="application/json">""" + json.dumps({
    "props": {
        "pageProps": {
            "article": {
                "title": "测试",
                "content": _script_content,
            }
        }
    }
}, ensure_ascii=False) + """</script>
</body></html>"""

# Minimal Next.js JS assignment HTML
_js_content = "<p>正文。" + "测试内容。" * 20 + "</p>"
NEXT_DATA_JS_HTML = """<!DOCTYPE html>
<html><head><title>Test</title></head><body>
<script>__NEXT_DATA__ = """ + json.dumps({
    "props": {
        "pageProps": {
            "article": {
                "title": "测试",
                "content": _js_content,
            }
        }
    }
}, ensure_ascii=False) + """</script>
</body></html>"""

# WallStreetCN style __SSR__
_ssr_content = "<p>" + "正文内容。" * 20 + "</p>"
SSR_HTML = """<!DOCTYPE html>
<html><head><title>Test</title></head><body>
<script>__SSR__ = """ + json.dumps({
    "article": {
        "title": "SSR文章",
        "content": _ssr_content,
    }
}, ensure_ascii=False) + """</script>
</body></html>"""

# JSON-LD Article HTML
_jsonld_body = "<p>" + "正文内容。" * 20 + "</p>"
JSON_LD_HTML = """<!DOCTYPE html>
<html><head><title>Test</title></head><body>
<script type="application/ld+json">""" + json.dumps({
    "@type": "Article",
    "headline": "JSON-LD标题",
    "articleBody": _jsonld_body,
    "keywords": ["科技", "AI"],
    "datePublished": "2026-06-01",
}, ensure_ascii=False) + """</script>
</body></html>"""

# ThePaper.cn full integration HTML (Next.js script tag format)
# Build thepaper.cn fixture — use single quotes for HTML attributes
# inside the content string so json.dumps handles all escaping correctly.
_thepaper_content = (
    "<p>正文段落。</p>"
    "<img src='https://x.com/photo.jpg' alt='配图'>"
    "<p>更多内容。" + "正文。" * 15 + "</p>"
)
THEPAPER_HTML = """<!DOCTYPE html>
<html>
<head>
<title>测试标题 - 澎浃新闻</title>
<meta name="description" content="澎浃新闻测试摘要">
</head>
<body>
<script id="__NEXT_DATA__" type="application/json">""" + json.dumps({
    "props": {
        "pageProps": {
            "article": {
                "title": "测试文章标题",
                "content": _thepaper_content,
                "keywords": ["时政", "经济"],
                "datePublished": "2026-06-15T10:00:00+08:00",
                "description": "",
            }
        }
    }
}, ensure_ascii=False) + """</script>
</body>
</html>"""


# ── B1: JSON candidate discovery ────────────────────────────────────

class TestFindJsonCandidates:
    """_find_json_candidates discovers SPA JSON from known patterns."""

    def test_finds_next_data_script_tag(self):
        parser = HtmlParser()
        candidates = list(parser._find_json_candidates(NEXT_DATA_SCRIPT_HTML))
        assert len(candidates) > 0

    def test_finds_next_data_js_assignment(self):
        parser = HtmlParser()
        candidates = list(parser._find_json_candidates(NEXT_DATA_JS_HTML))
        assert len(candidates) > 0

    def test_finds_ssr_assignment(self):
        parser = HtmlParser()
        candidates = list(parser._find_json_candidates(SSR_HTML))
        assert len(candidates) > 0

    def test_finds_nuxt_assignment(self):
        html = '<script>__NUXT__ = {"state":{"article":{"title":"t","content":"<p>c' + "text" * 20 + '</p>"}}}</script>'
        parser = HtmlParser()
        candidates = list(parser._find_json_candidates(html))
        assert len(candidates) > 0

    def test_finds_json_ld_article(self):
        parser = HtmlParser()
        candidates = list(parser._find_json_candidates(JSON_LD_HTML))
        assert len(candidates) > 0

    def test_handles_malformed_json_gracefully(self):
        html = '<script>__NEXT_DATA__ = {broken json{{{</script>'
        parser = HtmlParser()
        # Should not raise — just yield nothing for unparseable JSON
        candidates = list(parser._find_json_candidates(html))
        # With malformed input, we get no candidates — that's fine
        assert isinstance(candidates, list)


# ── B2: Article search in JSON tree ─────────────────────────────────

class TestFindArticleInJson:
    """_find_article_in_json recursively finds article objects in JSON."""

    def test_finds_article_by_title_and_content(self):
        data = {"a": {"b": {"title": "文章标题", "content": "正文内容。" * 20}}}
        result = HtmlParser._find_article_in_json(data)
        assert result is not None
        assert result["title"] == "文章标题"
        assert "正文内容" in result["content"]

    def test_finds_jsonld_by_headline_and_article_body(self):
        data = {
            "@type": "Article",
            "headline": "JSON-LD 标题",
            "articleBody": "正文内容。" * 20,
        }
        result = HtmlParser._find_article_in_json(data)
        assert result is not None
        assert result["title"] == "JSON-LD 标题"
        assert "正文内容" in result["content"]

    def test_picks_longest_content_when_multiple(self):
        short = {"title": "短文章", "content": "短。"}
        long = {
            "title": "长文章",
            "content": "这是很长的正文内容。" * 20,
        }
        data = {"articles": [short, long]}
        result = HtmlParser._find_article_in_json(data)
        assert result is not None
        assert result["title"] == "长文章"

    def test_extracts_jsonld_keywords_list(self):
        data = {
            "@type": "Article",
            "headline": "标题",
            "articleBody": "正文。" * 20,
            "keywords": ["科技", "AI"],
        }
        result = HtmlParser._find_article_in_json(data)
        assert result is not None
        assert result["keywords"] == ["科技", "AI"]

    def test_extracts_jsonld_keywords_comma_string(self):
        data = {
            "@type": "Article",
            "headline": "标题",
            "articleBody": "正文。" * 20,
            "keywords": "科技,AI,政策",
        }
        result = HtmlParser._find_article_in_json(data)
        assert result is not None
        # Keywords as string is handled in _extract_spa_data, not here
        # _find_article_in_json just passes it through
        assert result["keywords"] == "科技,AI,政策"


# ── B3: Full SPA → Markdown pipeline ────────────────────────────────

class TestExtractSpaData:
    """_extract_spa_data — the main SPA JSON → Markdown pipeline."""

    def test_strips_blockquote_before_trafilatura(self):
        """<blockquote>-wrapped images (wallstreetcn) must survive."""
        html = """<script id="__NEXT_DATA__" type="application/json">"""
        data = {
            "props": {
                "pageProps": {
                    "article": {
                        "title": "测试",
                        "content": "<p>文字</p><blockquote><img src='https://x.com/img.jpg'></blockquote><p>更多" + "内容。" * 20 + "</p>",
                    }
                }
            }
        }
        html += json.dumps(data, ensure_ascii=False) + "</script>"
        parser = HtmlParser()
        result = parser._extract_spa_data(html)
        assert result is not None
        assert "img.jpg" in result["markdown"] or "<img" in result["markdown"]

    def test_preserves_bare_img_in_html_fragment(self):
        """Bare <img> between <p> tags (thepaper.cn) must survive.
        The fix wraps <img> in <p> before trafilatura processing.
        Note: Short text content ensures trafilatura returns None,
        triggering _build_image_markdown which preserves the image."""
        html = """<script id="__NEXT_DATA__" type="application/json">"""
        data = {
            "props": {
                "pageProps": {
                    "article": {
                        "title": "澎浃文章",
                        "content": "<p>段落一。</p><img src='https://x.com/photo.jpg' alt='配图'><p>段落二。正文正文正文正文正文正文正文正文正文正文正文</p>",
                    }
                }
            }
        }
        html += json.dumps(data, ensure_ascii=False) + "</script>"
        parser = HtmlParser()
        result = parser._extract_spa_data(html)
        assert result is not None
        # The bare img should be preserved (via _build_image_markdown fallback)
        assert "photo.jpg" in result["markdown"]

    def test_falls_back_to_image_markdown_for_image_heavy(self):
        """Image-only content with <50 chars of text triggers
        _build_image_markdown fallback.
        Multiple images ensure the generated markdown exceeds 50 chars
        so _extract_spa_data accepts the result."""
        html = """<script id="__NEXT_DATA__" type="application/json">"""
        data = {
            "props": {
                "pageProps": {
                    "article": {
                        "title": "一图看懂",
                        "content": '<img src="https://x.com/infographic.jpg" alt="信息图"><img src="https://x.com/chart.png" alt="图表">',
                    }
                }
            }
        }
        html += json.dumps(data, ensure_ascii=False) + "</script>"
        parser = HtmlParser()
        result = parser._extract_spa_data(html)
        assert result is not None
        assert "infographic.jpg" in result["markdown"]

    def test_extracts_summary_from_og_description(self):
        """When JSON has no description, fall back to og:description meta."""
        html = """<!DOCTYPE html><html><head>
<meta name="description" content="OG摘要内容">
</head><body>
<script id="__NEXT_DATA__" type="application/json">"""
        data = {
            "props": {
                "pageProps": {
                    "article": {
                        "title": "测试",
                        "content": "<p>" + "正文内容。" * 20 + "</p>",
                        "description": "",
                    }
                }
            }
        }
        html += json.dumps(data, ensure_ascii=False) + "</script></body></html>"
        parser = HtmlParser()
        result = parser._extract_spa_data(html)
        assert result is not None
        assert "OG摘要内容" in result["summary"]

    def test_skips_candidate_with_short_content(self):
        """Candidates with content < 50 chars are skipped."""
        html = """<script id="__NEXT_DATA__" type="application/json">"""
        data = {
            "props": {
                "pageProps": {
                    "article": {
                        "title": "短文章",
                        "content": "太短了",
                    }
                }
            }
        }
        html += json.dumps(data, ensure_ascii=False) + "</script>"
        parser = HtmlParser()
        result = parser._extract_spa_data(html)
        assert result is None

    def test_integration_thepaper_next_data(self):
        """Full thepaper.cn HTML → parse → markdown with title + images + body."""
        parser = HtmlParser()
        result = parser._extract_spa_data(THEPAPER_HTML)
        assert result is not None
        assert result["title"] == "测试文章标题"
        assert "photo.jpg" in result["markdown"]
        assert len(result["markdown"].strip()) > 50
        assert result["tags"] == ["时政", "经济"]
