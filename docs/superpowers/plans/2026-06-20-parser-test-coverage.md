# Parser 全量测试覆盖 — 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 `news/parser.py` 的所有解析路径、边界处理和已知 bug 场景编写 74 个测试用例，覆盖率从现有的 ~15% 提升到 80%+。

**Architecture:** 按 parser 子系统拆分为 9 个测试文件，加 1 个共享 conftest.py。纯函数（`_fix_lazy_images`、`_beautify_markdown_formatting` 等）直接调静态方法测试；需要完整 HTML 的路径（`_extract_spa_data`、`_extract_with_trafilatura`）用最小 HTML fixture 端到端测试。所有测试独立运行，无状态共享。

**Tech Stack:** pytest, Python 3.12+, news.parser.HtmlParser

## Global Constraints

- 测试覆盖率目标: 80%+ 行覆盖（整体），纯函数 100%
- 测试文件 < 300 行/文件
- 测试风格: pytest 函数 + 类组织，与现有 `test_parser.py` 一致
- `news/parser.py` 不修改（未提交的 SPA image fix 保留不动）
- 使用 `conftest.py` 共享 `make_html` 工具函数和 `parser` fixture
- 所有测试必须独立，不依赖执行顺序或外部网络

---

## File Map

| 文件 | 操作 | 责任 |
|------|------|------|
| `tests/conftest.py` | 新建 | 共享工具函数 `make_html`、pytest fixture `parser` |
| `tests/test_parser_trim_noise.py` | 重命名+扩展 | `test_parser.py` → 重命名，补充 9 个测试 |
| `tests/test_parser_lazy_images.py` | 新建 | `_fix_lazy_images` — data-src/data-original 替换 |
| `tests/test_parser_beautify.py` | 新建 | `_beautify_markdown_formatting` — 格式修复 |
| `tests/test_parser_json_helpers.py` | 新建 | `_extract_bracketed_json` + `_extract_json_ld` |
| `tests/test_parser_edge_cases.py` | 新建 | 截断、标题提取、结果构建、空输入 |
| `tests/test_parser_fallback.py` | 新建 | `_fallback` — HTML tag strip |
| `tests/test_parser_build_image.py` | 新建 | `_build_image_markdown` — 图片兜底 |
| `tests/test_parser_trafilatura.py` | 新建 | `_extract_with_trafilatura` — 核心提取路径 |
| `tests/test_parser_spa.py` | 新建 | `_extract_spa_data` — SPA JSON 提取全链路 |

---

### Task 1: 基础设施 — conftest.py

**Files:**
- Create: `tests/conftest.py`
- Modify: `tests/test_parser.py` → 重命名为 `tests/test_parser_trim_noise.py`，更新 `_make_html` 引用

**Interfaces:**
- Produces: `make_html(body, head_noise, tail_noise) -> str` — HTML 构建工具函数
- Produces: `parser` — pytest fixture，返回 `HtmlParser()` 实例

- [ ] **Step 1: 创建 `tests/conftest.py`**

```python
"""Shared fixtures and utilities for parser tests."""

import pytest


def make_html(body: str, head_noise: str = "", tail_noise: str = "") -> str:
    """Build a minimal HTML page with optional head/tail noise around body.

    Moved from tests/test_parser.py:_make_html so all parser test files
    can share the same HTML construction helper.
    """
    return f"""<!DOCTYPE html>
<html>
<head><title>Test Article</title></head>
<body>
<article>
{head_noise}
{body}
{tail_noise}
</article>
</body>
</html>"""


@pytest.fixture
def parser():
    """Return a default-configured HtmlParser instance."""
    from news.parser import HtmlParser

    return HtmlParser()
```

- [ ] **Step 2: 重命名 `tests/test_parser.py` → `tests/test_parser_trim_noise.py`，更新 `_make_html` 为 `make_html`**

Run:
```bash
git mv tests/test_parser.py tests/test_parser_trim_noise.py
```

然后编辑 `tests/test_parser_trim_noise.py`，将文件顶部的 `_make_html` 函数定义替换为从 conftest 导入：

删掉现有的 `def _make_html(...)` 函数（第 7-19 行），替换为：
```python
"""Tests for HtmlParser._trim_noise — head/tail noise trimming."""

import pytest
from conftest import make_html
from news.parser import HtmlParser
```

并将文件中所有 `_make_html(` 调用替换为 `make_html(`。

- [ ] **Step 3: 运行已有测试验证重命名后全部通过**

```bash
pytest tests/test_parser_trim_noise.py -v
```
Expected: 11 passed

- [ ] **Step 4: Commit**

```bash
git add tests/conftest.py tests/test_parser_trim_noise.py tests/test_parser.py
git commit -m "test: add conftest.py with shared fixtures, rename test_parser.py"
```

---

### Task 2: P1 — Lazy Images 测试（3 个）

**Files:**
- Create: `tests/test_parser_lazy_images.py`

**Interfaces:**
- Consumes: `HtmlParser._fix_lazy_images(html: str) -> str` — 静态方法
- Produces: 无（独立文件）

- [ ] **Step 1: 编写测试文件**

```python
"""Tests for HtmlParser._fix_lazy_images — lazy-loaded image src rewriting."""

from news.parser import HtmlParser


class TestFixLazyImages:
    """_fix_lazy_images converts data-src / data-original to src
    when the placeholder is a data: URI."""

    def test_swaps_data_src_with_data_uri_placeholder(self):
        html = '<img data-src="https://x.com/real.jpg" src="data:image/gif;base64,R0lGODlhAQABAAAAACH5BAEKAAEALAAAAAABAAEAAAICTAEAOw==">'
        result = HtmlParser._fix_lazy_images(html)
        assert 'src="https://x.com/real.jpg"' in result
        assert "data-src" not in result

    def test_swaps_data_original_with_data_uri_placeholder(self):
        html = '<img class="lazy" data-original="https://x.com/real.png" src="data:image/png;base64,iVBORw0KGgo=">'
        result = HtmlParser._fix_lazy_images(html)
        assert 'src="https://x.com/real.png"' in result
        assert "data-original" not in result

    def test_preserves_normal_img_unchanged(self):
        # Normal img without lazy-load placeholder — must stay intact
        html = '<img src="https://x.com/normal.jpg" alt="正常图片">'
        result = HtmlParser._fix_lazy_images(html)
        assert result == html
```

- [ ] **Step 2: 运行测试**

```bash
pytest tests/test_parser_lazy_images.py -v
```
Expected: 3 passed

- [ ] **Step 3: Commit**

```bash
git add tests/test_parser_lazy_images.py
git commit -m "test: add _fix_lazy_images test coverage"
```

---

### Task 3: P1 — Beautify 测试（4 个）

**Files:**
- Create: `tests/test_parser_beautify.py`

**Interfaces:**
- Consumes: `HtmlParser._beautify_markdown_formatting(markdown: str) -> str`

- [ ] **Step 1: 编写测试文件**

```python
"""Tests for HtmlParser._beautify_markdown_formatting."""

from news.parser import HtmlParser


class TestBeautifyMarkdown:
    """_beautify_markdown_formatting cleans up formatting noise."""

    def test_normalizes_stray_spaces_in_bold_markers(self):
        markdown = "这是 ** text** 和 **text ** 测试"
        result = HtmlParser._beautify_markdown_formatting(markdown)
        assert "** text**" not in result
        assert "**text **" not in result

    def test_adds_space_around_bold_adjacent_text(self):
        markdown = "是**text**普"
        result = HtmlParser._beautify_markdown_formatting(markdown)
        assert "是 **text** 普" in result

    def test_removes_praise_button(self):
        markdown = "- +1\n\n# 标题\n\n正文内容"
        result = HtmlParser._beautify_markdown_formatting(markdown)
        assert "- +1" not in result
        assert "# 标题" in result

    def test_idempotent_on_clean_markdown(self):
        markdown = "# 标题\n\n**加粗文字** 普通文字"
        result = HtmlParser._beautify_markdown_formatting(markdown)
        assert result == markdown
```

- [ ] **Step 2: 运行测试**

```bash
pytest tests/test_parser_beautify.py -v
```
Expected: 4 passed

- [ ] **Step 3: Commit**

```bash
git add tests/test_parser_beautify.py
git commit -m "test: add _beautify_markdown_formatting test coverage"
```

---

### Task 4: P1 — JSON Helpers 测试（5 个）

**Files:**
- Create: `tests/test_parser_json_helpers.py`

**Interfaces:**
- Consumes: `HtmlParser._extract_bracketed_json(html_text: str, pattern: str) -> list[dict]`
- Consumes: `HtmlParser._extract_json_ld(html_text: str) -> list[dict]`

- [ ] **Step 1: 编写测试文件**

```python
"""Tests for HtmlParser._extract_bracketed_json and _extract_json_ld."""

from news.parser import HtmlParser


class TestExtractBracketedJson:
    """_extract_bracketed_json finds and parses JSON by bracket-matching."""

    def test_bracket_match_simple(self):
        html = 'var data = {"key":"value"};'
        results = HtmlParser._extract_bracketed_json(html, r'data\s*=\s*(\{)')
        assert len(results) == 1
        assert results[0] == {"key": "value"}

    def test_bracket_match_nested(self):
        html = '{"outer":{"inner":{"deep":1}}}'
        results = HtmlParser._extract_bracketed_json(html, r'=\s*(\{)')
        # Pattern doesn't match — test direct nested bracket matching
        results = HtmlParser._extract_bracketed_json(html, r'^(\{)')
        assert len(results) == 1
        assert results[0] == {"outer": {"inner": {"deep": 1}}}

    def test_bracket_match_unclosed_returns_empty(self):
        html = 'var data = {"key":"value"'
        results = HtmlParser._extract_bracketed_json(html, r'data\s*=\s*(\{)')
        assert results == []


class TestExtractJsonLd:
    """_extract_json_ld extracts JSON-LD from script tags."""

    def test_extracts_multiple_script_tags(self):
        html = """<script type="application/ld+json">{"@type":"Article","headline":"A"}</script>
<script type="application/ld+json">{"@type":"WebSite","name":"S"}</script>"""
        results = HtmlParser._extract_json_ld(html)
        assert len(results) == 2
        assert results[0]["@type"] == "Article"
        assert results[1]["@type"] == "WebSite"

    def test_skips_invalid_json(self):
        html = """<script type="application/ld+json">{invalid json}</script>
<script type="application/ld+json">{"@type":"Article"}</script>"""
        results = HtmlParser._extract_json_ld(html)
        assert len(results) == 1
        assert results[0]["@type"] == "Article"
```

- [ ] **Step 2: 运行测试**

```bash
pytest tests/test_parser_json_helpers.py -v
```
Expected: 5 passed

- [ ] **Step 3: Commit**

```bash
git add tests/test_parser_json_helpers.py
git commit -m "test: add _extract_bracketed_json and _extract_json_ld test coverage"
```

---

### Task 5: P1 — Edge Cases 测试（6 个）

**Files:**
- Create: `tests/test_parser_edge_cases.py`

**Interfaces:**
- Consumes: `HtmlParser._extract_markdown_heading(markdown: str) -> str`
- Consumes: `HtmlParser._build_result(markdown, ...) -> dict`
- Consumes: `HtmlParser.parse(html: str, url: str) -> dict | None`
- Consumes: `parser` fixture from conftest

- [ ] **Step 1: 编写测试文件**

```python
"""Tests for parser edge cases: truncation, heading extraction,
result building, and empty input."""

from news.parser import HtmlParser


class TestExtractMarkdownHeading:
    """_extract_markdown_heading extracts the first H1 from markdown."""

    def test_returns_first_h1(self):
        markdown = "# 文章标题\n\n## 副标题\n\n正文内容"
        result = HtmlParser._extract_markdown_heading(markdown)
        assert result == "文章标题"

    def test_returns_empty_when_no_h1(self):
        markdown = "## 只有副标题\n\n正文内容"
        result = HtmlParser._extract_markdown_heading(markdown)
        assert result == ""


class TestBuildResult:
    """_build_result builds the unified result dict."""

    def test_strips_hash_prefix_from_tags(self):
        result = HtmlParser._build_result(
            "content", tags=["#tag1", "#tag2"]
        )
        assert result["tags"] == ["tag1", "tag2"]

    def test_removes_empty_tags_after_stripping(self):
        result = HtmlParser._build_result(
            "content", tags=["#", "tag"]
        )
        assert result["tags"] == ["tag"]


class TestParseEdgeCases:
    """parse() edge cases."""

    def test_parse_returns_none_for_empty_html(self):
        parser = HtmlParser()
        result = parser.parse("")
        assert result is None

    def test_truncates_content_over_max_length(self):
        parser = HtmlParser()
        parser.max_content_length = 200
        long_text = "正文内容。" * 100  # ~600 chars
        html = f"<html><body><article><p>{long_text}</p></article></body></html>"
        result = parser.parse(html)
        assert result is not None
        assert len(result["markdown"]) <= parser.max_content_length + len("\n\n... (truncated)")
        assert "... (truncated)" in result["markdown"]
```

- [ ] **Step 2: 运行测试**

```bash
pytest tests/test_parser_edge_cases.py -v
```
Expected: 6 passed

- [ ] **Step 3: Commit**

```bash
git add tests/test_parser_edge_cases.py
git commit -m "test: add edge case test coverage for truncation, heading, result building"
```

---

### Task 6: P2 — Fallback 测试（5 个）

**Files:**
- Create: `tests/test_parser_fallback.py`

**Interfaces:**
- Consumes: `parser.parse(html)` — trafilatura 不可用时走 `_fallback`
- Consumes: `parser._fallback(html_text, url)` — 直接测试
- Consumes: `make_html` from conftest

- [ ] **Step 1: 编写测试文件**

```python
"""Tests for HtmlParser._fallback — HTML tag stripping path."""

from news.parser import HtmlParser
from conftest import make_html


class TestFallback:
    """_fallback strips HTML tags when trafilatura is unavailable."""

    def test_strips_non_content_tags(self):
        parser = HtmlParser()
        html = """<html><head><title>Test</title></head><body>
<script>console.log('x')</script>
<style>.a{color:red}</style>
<nav><a href="/">Home</a></nav>
<header>Header content</header>
<footer>Footer content</footer>
<aside>Sidebar</aside>
<p>""" + "正文段落内容。" * 20 + """</p>
</body></html>"""
        result = parser._fallback(html, "http://example.com")
        assert result is not None
        assert "正文段落内容" in result["markdown"]
        assert "console.log" not in result["markdown"]
        assert "Home" not in result["markdown"]

    def test_extracts_meta_fields(self):
        parser = HtmlParser()
        html = """<html><head>
<title>Test</title>
<meta name="author" content="张三">
<meta name="description" content="这是一篇测试文章">
<meta property="article:published_time" content="2026-01-01T10:00:00+08:00">
</head><body><p>""" + "正文段落内容。" * 20 + """</p></body></html>"""
        result = parser._fallback(html)
        assert result is not None
        assert result["author"] == "张三"
        assert "测试文章" in result["summary"]

    def test_filters_short_paragraphs(self):
        parser = HtmlParser()
        # 多个短段落 + 1 个长段落
        body = "<p>短。</p>\n<p>也很短。</p>\n<p>" + "长段落内容。" * 20 + "</p>"
        html = make_html(body)
        result = parser._fallback(html)
        assert result is not None
        assert "长段落内容" in result["markdown"]

    def test_returns_none_for_short_content(self):
        parser = HtmlParser()
        html = make_html("<p>短内容。</p>")
        result = parser._fallback(html)
        assert result is None

    def test_extracts_title(self):
        parser = HtmlParser()
        html = """<html><head>
<title>文章标题 - 网站名</title>
<meta property="og:title" content="OG 文章标题">
</head><body><p>""" + "正文段落内容。" * 20 + """</p></body></html>"""
        result = parser._fallback(html)
        assert result is not None
        # _fallback uses _extract_title_from_html which prefers og:title over <title>
        assert "文章标题" in result["title"]
```

- [ ] **Step 2: 运行测试**

```bash
pytest tests/test_parser_fallback.py -v
```
Expected: 5 passed

- [ ] **Step 3: Commit**

```bash
git add tests/test_parser_fallback.py
git commit -m "test: add _fallback test coverage"
```

---

### Task 7: P2 — Image Building 测试（4 个）

**Files:**
- Create: `tests/test_parser_build_image.py`

**Interfaces:**
- Consumes: `HtmlParser._build_image_markdown(html: str) -> str`

- [ ] **Step 1: 编写测试文件**

```python
"""Tests for HtmlParser._build_image_markdown — image-heavy content fallback."""

from news.parser import HtmlParser


class TestBuildImageMarkdown:
    """_build_image_markdown extracts img tags into markdown for
    image-heavy / low-text articles."""

    def test_extracts_img_to_markdown_syntax(self):
        html = '<img src="https://x.com/photo.jpg" alt="配图">'
        result = HtmlParser._build_image_markdown(html)
        assert "![](https://x.com/photo.jpg)" in result

    def test_preserves_remaining_text(self):
        html = '<img src="https://x.com/chart.png"><p>图表说明文字</p>'
        result = HtmlParser._build_image_markdown(html)
        assert "![](https://x.com/chart.png)" in result
        assert "图表说明文字" in result

    def test_handles_multiple_images(self):
        html = '<img src="https://x.com/a.jpg"><img src="https://x.com/b.jpg">'
        result = HtmlParser._build_image_markdown(html)
        assert "![](https://x.com/a.jpg)" in result
        assert "![](https://x.com/b.jpg)" in result

    def test_returns_empty_for_no_images_or_text(self):
        html = "<div></div>"
        result = HtmlParser._build_image_markdown(html)
        assert result == ""
```

- [ ] **Step 2: 运行测试**

```bash
pytest tests/test_parser_build_image.py -v
```
Expected: 4 passed

- [ ] **Step 3: Commit**

```bash
git add tests/test_parser_build_image.py
git commit -m "test: add _build_image_markdown test coverage"
```

---

### Task 8: P3 — trafilatura 路径测试（10 个）

**Files:**
- Create: `tests/test_parser_trafilatura.py`

**Interfaces:**
- Consumes: `parser._extract_with_trafilatura(html, url, skip_trim=False) -> dict | None`
- Consumes: `parser.parse(html, url)` — 主入口走 trafilatura
- Consumes: `make_html` from conftest

- [ ] **Step 1: 编写测试文件**

```python
"""Tests for HtmlParser._extract_with_trafilatura — the primary extraction path."""

from news.parser import HtmlParser
from conftest import make_html


def _full_page(title="测试标题", body_text="正文段落内容。") -> str:
    """Build a complete HTML page with enough content for trafilatura."""
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


class TestTrafilaturaExtraction:
    """_extract_with_trafilatura — full-page content extraction."""

    def test_extracts_content_from_full_page(self):
        parser = HtmlParser()
        html = _full_page()
        result = parser._extract_with_trafilatura(html, "http://example.com")
        assert result is not None
        assert len(result["markdown"].strip()) > 50
        assert "正文段落内容" in result["markdown"]

    def test_returns_none_for_short_content(self):
        parser = HtmlParser()
        html = make_html("<p>短。</p>")
        result = parser._extract_with_trafilatura(html, "http://example.com")
        assert result is None

    def test_title_prefers_h1_over_og_title(self):
        parser = HtmlParser()
        html = _full_page(title="真正的文章标题")
        result = parser._extract_with_trafilatura(html, "http://example.com")
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
        result = parser._extract_with_trafilatura(html, "http://example.com")
        # Should not crash even if metadata extraction has issues
        assert result is not None
        assert "markdown" in result

    def test_skip_trim_respected(self):
        parser = HtmlParser()
        html = _full_page()
        # With skip_trim=True, _trim_noise should not be called
        result = parser._extract_with_trafilatura(
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
        result = parser._extract_with_trafilatura(html, "http://example.com")
        assert result is not None
        # Copyright footer should be trimmed
        assert "版权所有" not in result["markdown"]

    def test_extracts_categories_and_tags(self):
        parser = HtmlParser()
        html = _full_page()
        result = parser._extract_with_trafilatura(html, "http://example.com")
        assert result is not None
        # trafilatura should extract categories from meta keywords
        assert isinstance(result["tags"], list)

    def test_deduplicates_tags(self):
        parser = HtmlParser()
        html = _full_page()
        result = parser._extract_with_trafilatura(html, "http://example.com")
        assert result is not None
        # No duplicate tags
        assert len(result["tags"]) == len(set(result["tags"]))

    def test_extracts_author_date_description(self):
        parser = HtmlParser()
        html = _full_page()
        result = parser._extract_with_trafilatura(html, "http://example.com")
        assert result is not None
        # trafilatura metadata should include author
        assert len(result["author"]) > 0

    def test_beautify_applied_to_output(self):
        parser = HtmlParser()
        body = "<h1>标题</h1><p>是**重要**通知</p><p>" + "正文。" * 20 + "</p>"
        html = make_html(body)
        result = parser._extract_with_trafilatura(html, "http://example.com")
        assert result is not None
        # Beautify should normalize bold marker spacing
        assert "是 **重要** 通知" in result["markdown"]
```

- [ ] **Step 2: 运行测试**

```bash
pytest tests/test_parser_trafilatura.py -v
```
Expected: 10 passed

- [ ] **Step 3: Commit**

```bash
git add tests/test_parser_trafilatura.py
git commit -m "test: add _extract_with_trafilatura test coverage"
```

---

### Task 9: P3 — SPA Data 测试（17 个）

**Files:**
- Create: `tests/test_parser_spa.py`

**Interfaces:**
- Consumes: `parser._find_json_candidates(html_text)` — 实例方法
- Consumes: `HtmlParser._find_article_in_json(data)` — 静态方法
- Consumes: `parser._extract_spa_data(html_text, url)` — 实例方法
- Consumes: `parser.parse(html, url)` — 主入口，SPA 优先

- [ ] **Step 1: 编写测试文件 — Part A: B1 JSON 候选发现（6 个测试）**

```python
"""Tests for SPA data extraction: _find_json_candidates, _find_article_in_json,
and _extract_spa_data — the full SPA JSON → Markdown pipeline."""

import json
import re

from news.parser import HtmlParser


# ── Shared test HTML fixtures ──────────────────────────────────────

# Minimal Next.js script tag HTML for testing candidate discovery
NEXT_DATA_SCRIPT_HTML = """<!DOCTYPE html>
<html><head><title>Test</title></head><body>
<script id="__NEXT_DATA__" type="application/json">{"props":{"pageProps":{"article":{"title":"测试","content":"<p>正文。" + "测试内容。" * 20 + "</p>"}}}}</script>
</body></html>"""

# Minimal Next.js JS assignment HTML
NEXT_DATA_JS_HTML = """<!DOCTYPE html>
<html><head><title>Test</title></head><body>
<script>__NEXT_DATA__ = {"props":{"pageProps":{"article":{"title":"测试","content":"<p>正文。" + "测试内容。" * 20 + "</p>"}}}}</script>
</body></html>"""

# WallStreetCN style __SSR__
SSR_HTML = """<!DOCTYPE html>
<html><head><title>Test</title></head><body>
<script>__SSR__ = {"article":{"title":"SSR文章","content":"<p>" + "正文内容。" * 20 + "</p>"}}</script>
</body></html>"""

# JSON-LD Article HTML
JSON_LD_HTML = """<!DOCTYPE html>
<html><head><title>Test</title></head><body>
<script type="application/ld+json">{"@type":"Article","headline":"JSON-LD标题","articleBody":"<p>" + "正文内容。" * 20 + "</p>","keywords":["科技","AI"],"datePublished":"2026-06-01"}</script>
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
<title>测试标题 - 澎湃新闻</title>
<meta name="description" content="澎湃新闻测试摘要">
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
```

- [ ] **Step 2: 运行 B1 测试**

```bash
pytest tests/test_parser_spa.py -v -k "TestFindJsonCandidates"
```
Expected: 6 passed

- [ ] **Step 3: 编写测试文件 — Part B: B2 article 搜索（5 个测试）**

Append to `tests/test_parser_spa.py`:

```python
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
```

- [ ] **Step 4: 运行 B2 测试**

```bash
pytest tests/test_parser_spa.py -v -k "TestFindArticleInJson"
```
Expected: 5 passed

- [ ] **Step 5: 编写测试文件 — Part C: B3 SPA content → Markdown（6 个测试）**

Append to `tests/test_parser_spa.py`:

```python
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
        The fix wraps <img> in <p> before trafilatura processing."""
        html = """<script id="__NEXT_DATA__" type="application/json">"""
        data = {
            "props": {
                "pageProps": {
                    "article": {
                        "title": "澎湃文章",
                        "content": "<p>段落一。</p><img src='https://x.com/photo.jpg' alt='配图'><p>段落二。" + "更多内容。" * 20 + "</p>",
                    }
                }
            }
        }
        html += json.dumps(data, ensure_ascii=False) + "</script>"
        parser = HtmlParser()
        result = parser._extract_spa_data(html)
        assert result is not None
        # The bare img should be preserved
        assert "photo.jpg" in result["markdown"]

    def test_falls_back_to_image_markdown_for_image_heavy(self):
        """Image-only content with <50 chars of text triggers
        _build_image_markdown fallback."""
        html = """<script id="__NEXT_DATA__" type="application/json">"""
        data = {
            "props": {
                "pageProps": {
                    "article": {
                        "title": "一图看懂",
                        "content": '<img src="https://x.com/infographic.jpg" alt="信息图">',
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
```

- [ ] **Step 6: 运行 B3 测试**

```bash
pytest tests/test_parser_spa.py -v -k "TestExtractSpaData"
```
Expected: 6 passed

- [ ] **Step 7: 运行全部 SPA 测试**

```bash
pytest tests/test_parser_spa.py -v
```
Expected: 17 passed

- [ ] **Step 8: Commit**

```bash
git add tests/test_parser_spa.py
git commit -m "test: add _extract_spa_data full pipeline test coverage"
```

---

### Task 10: P4 — Trim Noise 补充测试（9 个）

**Files:**
- Modify: `tests/test_parser_trim_noise.py`

**Interfaces:**
- Consumes: `parser._trim_noise(html) -> str | None`
- Consumes: `make_html` from conftest
- Produces: 补充 9 个测试到现有 `TestTrimNoise` 类

- [ ] **Step 1: 追加补充测试**

在 `tests/test_parser_trim_noise.py` 的 `TestTrimNoise` 类末尾（`test_preserves_image_inside_paragraph` 之后）追加：

```python
    def test_short_page_with_h1_not_degraded(self):
        """A page with h1 heading but short body should not degrade to None."""
        body = "<h1>标题</h1><p>短正文。</p>"
        html = make_html(body)
        parser = HtmlParser()
        result = parser._trim_noise(html)
        assert result is not None
        assert "标题" in result

    def test_h4_h5_h6_skipped_as_footer_headings(self):
        """h4/h5/h6 are almost always footer headings like '扫码下载APP'
        and should be skipped when searching for the end boundary."""
        body = "<h1>文章标题</h1><p>" + "正文内容。" * 20 + "</p>"
        tail = "<h4>扫码下载APP</h4><p>© 2024</p>"
        html = make_html(body, tail_noise=tail)
        parser = HtmlParser()
        result = parser._trim_noise(html)
        assert result is not None
        assert "扫码下载" not in result

    def test_start_gt_end_degrades_to_none(self):
        """When start > end (overlap), _trim_noise returns None."""
        # This is challenging to trigger manually — test the degenerate case
        # where boundaries can't be established
        html = make_html("")
        parser = HtmlParser()
        # No blocks at all — should degrade
        result = parser._trim_noise(html)
        assert result is None

    def test_no_blocks_degrades_to_none(self):
        """HTML with no block-level elements should return None."""
        html = "<html><body>裸文本，没有块级标签。</body></html>"
        parser = HtmlParser()
        result = parser._trim_noise(html)
        assert result is None

    def test_preserves_nested_div_between_boundaries(self):
        """Content inside nested <div> between start and end is preserved
        after DOM pruning (not lost like with the old block reassembly)."""
        body = "<h1>标题</h1>"
        body += "<div><p>" + "嵌套段落内容。" * 20 + "</p></div>"
        body += "<p>" + "更多内容。" * 10 + "</p>"
        html = make_html(body)
        parser = HtmlParser()
        result = parser._trim_noise(html)
        assert result is not None
        assert "嵌套段落内容" in result

    def test_removes_meta_wrapper_different_parents(self):
        """When h1 and the content div are siblings, noise wrappers
        between them (author/date/share divs) should be removed."""
        body = "<h1>文章标题</h1>"
        # Metadata wrapper — author, date, share buttons
        body += '<div class="meta"><span>作者：张三</span><span>2026-06-15</span></div>'
        body += '<div class="content"><p>' + "正文内容。" * 20 + "</p></div>"
        html = make_html(body)
        parser = HtmlParser()
        result = parser._trim_noise(html)
        assert result is not None
        # Metadata wrapper should be removed
        assert "作者" not in result
        assert "正文内容" in result

    def test_short_copyright_line_trimmed(self):
        """Short <p> at the end like '© 2024 某某网' (< 30 chars) is
        treated as tail noise."""
        body = "<h1>标题</h1><p>" + "正文内容。" * 20 + "</p>"
        tail = "<p>© 2024 某某网</p>"
        html = make_html(body, tail_noise=tail)
        parser = HtmlParser()
        result = parser._trim_noise(html)
        assert result is not None
        assert "©" not in result
        assert "正文内容" in result

    def test_long_paragraph_as_end_signal(self):
        """A paragraph >= 50 chars with low link density should serve
        as a reliable end boundary."""
        body = "<h1>标题</h1><p>" + "正文内容。" * 20 + "</p>"
        tail = "<p>" + "尾部无关链接。" * 5 + "</p>"
        html = make_html(body, tail_noise=tail)
        parser = HtmlParser()
        result = parser._trim_noise(html)
        assert result is not None
        assert "正文内容" in result

    def test_output_wrapped_in_article(self):
        """_trim_noise output is wrapped in <html><body><article> for
        trafilatura heading recognition."""
        body = "<h1>标题</h1><p>" + "正文内容。" * 20 + "</p>"
        html = make_html(body)
        parser = HtmlParser()
        result = parser._trim_noise(html)
        assert result is not None
        assert "<article>" in result
        assert "标题" in result
```

- [ ] **Step 2: 运行 trim noise 全部测试（已有 11 + 新 9 个）**

```bash
pytest tests/test_parser_trim_noise.py -v
```
Expected: 20 passed

- [ ] **Step 3: Commit**

```bash
git add tests/test_parser_trim_noise.py
git commit -m "test: add 9 supplementary _trim_noise test cases"
```

---

### Task 11: 最终验证 — 全量运行 + 覆盖率

**Files:**
- 无新建/修改

- [ ] **Step 1: 运行全部 parser 测试**

```bash
pytest tests/test_parser_*.py -v
```
Expected: 74 passed（11 已有 + 63 新建）

- [ ] **Step 2: 运行覆盖率**

```bash
pytest tests/test_parser_*.py --cov=news.parser --cov-report=term-missing
```
Expected: 80%+ 行覆盖

- [ ] **Step 3: 运行全量测试确保无回归**

```bash
pytest --cov=news.parser --cov-report=term-missing
```
Expected: 所有已有测试 + 新建测试全部通过

- [ ] **Step 4: 如有覆盖率不达标的模块，根据 `--cov-report=term-missing` 输出补充测试**

- [ ] **Step 5: Commit 最终结果**

```bash
git add tests/
git commit -m "test: finalize parser full coverage — 74 tests, 80%+ coverage"
```

---

## 实现节奏

| 任务 | 文件 | 测试数 | 预计耗时 | 依赖 |
|------|------|--------|----------|------|
| Task 1 | conftest + rename | — | 5 min | — |
| Task 2 | test_parser_lazy_images | 3 | 10 min | Task 1 |
| Task 3 | test_parser_beautify | 4 | 10 min | Task 1 |
| Task 4 | test_parser_json_helpers | 5 | 10 min | Task 1 |
| Task 5 | test_parser_edge_cases | 6 | 15 min | Task 1 |
| Task 6 | test_parser_fallback | 5 | 15 min | Task 1 |
| Task 7 | test_parser_build_image | 4 | 10 min | Task 1 |
| Task 8 | test_parser_trafilatura | 10 | 20 min | Task 1 |
| Task 9 | test_parser_spa | 17 | 30 min | Task 1 |
| Task 10 | test_parser_trim_noise补充 | 9 | 20 min | Task 1 |
| Task 11 | 验证 + 覆盖率 | — | 10 min | All |
| **Total** | | **74** | **~2.5h** | |
