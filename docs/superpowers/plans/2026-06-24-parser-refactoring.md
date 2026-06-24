# Parser 拆分重构实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将单体的 `news/parser.py` (~800+ 行) 拆分为基类 + 11 个站点 Parser，每个站点 Parser 只负责自己站点的 HTML 解析逻辑。

**Architecture:** 模板方法模式 — `HtmlParser` 基类定义 `parse()` 流水线骨架和通用降级链（readability → fallback），子类只覆写 `_preprocess()` 或 `_extract()` 实现站点特定逻辑。`ParserRegistry` 维护 `source_id → Parser实例` 映射，Crawler 通过 registry 路由到正确的 Parser。

**Tech Stack:** Python 3.12+, readability-lxml, markdownify, trafilatura, lxml, requests

## Global Constraints

- Python ≥ 3.12（使用 match/case 和 PEP 604 unions）
- 遵循 `rules/zh/coding-style.md`：不可变数据、函数 <50 行、文件 <800 行
- 测试覆盖率 ≥ 80%：离线 fixture + 在线 grab-one 双重验证
- 提交消息格式：`<type>: <描述>`（feat, refactor, test, chore）
- 站点 Parser 只能覆写 `_preprocess()` 和 `_extract()`，不能覆写 `parse()`
- 站点 Parser 必须通过 `_build_result()` 返回结果
- 站点 Parser 不能发起 HTTP 请求、不能 import 其他站点 Parser
- 先确保每个站点 Parser 独立完整工作，最后再提取公共模式

---

### Task 1: 创建 `news/parser/` 包结构

**Files:**
- Create: `news/parser/__init__.py`
- Create: `news/parser/parser.py`
- Create: `news/parser/registry.py`
- Create: `news/parser/sites/__init__.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `HtmlParser` class with `parse()`, `_preprocess()`, `_extract()`, `_extract_with_readability()`, `_fallback()`, `_build_result()`, `_extract_title_from_html()`, `_extract_meta()`, `_extract_markdown_heading()`, `_beautify_markdown_formatting()`, `_handle_markdown_bold()`
  - `ParserRegistry` class with `register()`, `parse()`
  - 模块级 `parser_registry` 单例

- [ ] **Step 1: 创建 `news/parser/registry.py`**

```python
"""Parser registry — source_id → Parser 路由."""

from __future__ import annotations

from typing import Optional, Dict, Any


class ParserRegistry:
    """source_id → Parser 实例的路由表。

    未注册的 source_id 自动走 HtmlParser() 默认实例兜底。
    """

    def __init__(self):
        self._parsers: dict[str, object] = {}
        self._default: object | None = None  # 从 __init__.py 注入

    def set_default(self, parser: object) -> None:
        """注入默认 Parser（HtmlParser 实例），避免循环 import。

        在 __init__.py 中调用：registry.set_default(HtmlParser())
        """
        self._default = parser

    def register(self, source_id: str, parser: object) -> None:
        """注册 source_id → Parser 映射。同名 source_id 后来者覆盖前者。"""
        self._parsers[source_id] = parser

    def parse(
        self, source_id: str, html: str, url: str = ""
    ) -> Optional[Dict[str, Any]]:
        """根据 source_id 路由到对应 Parser 解析。

        Args:
            source_id: 新闻源标识（如 "thepaper"、"ifeng"），来自 item["source_id"]
            html: 原始 HTML 文本
            url: 来源 URL（传给 readability 用于元数据提取）

        Returns:
            Dict 含 markdown/title/author/published_at/summary/category/tags，
            或 None 如果所有提取路径均失败。
        """
        parser = self._parsers.get(source_id, self._default)
        if parser is None:
            return None
        return parser.parse(html, url)


# 全局单例 — 模块加载时创建，由 __init__.py 和 sites/__init__.py 填充
parser_registry = ParserRegistry()
```

- [ ] **Step 2: 创建 `news/parser/parser.py`（基类）**

```python
# coding=utf-8
"""HtmlParser 基类 — 通用 HTML → Markdown 解析能力。

子类只需覆写 _preprocess() 或 _extract() 实现站点特定逻辑。
"""

from __future__ import annotations

import re
import html as _html

from typing import Any, Dict, List, Optional

import trafilatura  # kept for metadata extraction
from readability import Document
from markdownify import markdownify as _md


def _split_keyword_tags(tags: List[str]) -> List[str]:
    """Normalise keyword tags: split comma/space-separated strings into
    individual tags and remove duplicates while preserving order."""
    result: List[str] = []
    for tag in tags:
        # Split on comma or whitespace, drop empties
        parts = [t.strip() for t in re.split(r'[,\s]+', tag) if t.strip()]
        for p in parts:
            if p not in result:
                result.append(p)
    return result


class HtmlParser:
    """HTML → Markdown 解析基类。

    模板方法：:meth:`parse` 定义流水线骨架，
    子类覆写 :meth:`_preprocess` 或 :meth:`_extract` 实现站点特定逻辑。

    Usage::

        parser = HtmlParser(config)
        result = parser.parse(html_text, url="https://example.com")
    """

    def __init__(self, config: dict | None = None):
        self._config = config or {}
        cfg = self._config
        crawler_cfg = cfg.get("crawler", {})
        self.max_content_length = crawler_cfg.get("max_content_length", 100000)

    # ── Public API ─────────────────────────────────────────────────

    def parse(self, html: str, url: str = "") -> Optional[Dict[str, Any]]:
        """Extract Markdown + metadata from HTML.

        流水线: _preprocess → _extract → _extract_with_readability → _fallback

        Does **not** download or process images — callers should use
        :class:`ImageProcessor` separately.

        Args:
            html: Raw HTML text.
            url: Source URL (passed to readability for metadata).

        Returns:
            Dict with keys ``markdown``, ``title``, ``author``,
            ``published_at``, ``summary``, ``category``, ``tags``,
            or None if extraction produced nothing useful.
        """
        if not html or not html.strip():
            return None

        # 1. 站点可覆写的预处理 Hook
        html = self._preprocess(html, url)

        # 2. 站点可覆写的解析 Hook
        result = self._extract(html, url)

        # 3-4. 通用降级链
        if result is None:
            result = self._extract_with_readability(html, url)
        if result is None:
            result = self._fallback(html, url)

        if result is not None:
            md = result.get("markdown", "")
            if md and len(md) > self.max_content_length:
                result["markdown"] = md[:self.max_content_length] + "\n\n... (truncated)"

        return result

    # ── Hooks (子类可覆写) ─────────────────────────────────────────

    def _preprocess(self, html: str, url: str) -> str:
        """预处理 HTML — 子类可覆写此方法进行 DOM 清理等操作。

        默认行为：不修改 HTML。
        """
        return html

    def _extract(self, html: str, url: str) -> Optional[Dict[str, Any]]:
        """站点特定的解析逻辑 — 子类必须覆写此方法。

        默认行为：返回 None，走降级链。
        """
        return None

    # ── readability path ───────────────────────────────────────────

    def _extract_with_readability(
        self, html: str, url: str, skip_trim: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Use readability-lxml + markdownify for content extraction.

        Set *skip_trim* to True when *html* is already clean article
        body content (e.g. from SPA JSON) that doesn't need noise trimming.
        """
        # readability-lxml: extract article content HTML
        try:
            doc = Document(html, url=url)
            content_html = doc.summary()
        except Exception:
            return None

        if not content_html or not content_html.strip():
            return None

        # markdownify: HTML → Markdown
        markdown = _md(
            content_html,
            heading_style="ATX",
            strip=["script", "style"],
            escape_asterisks=False,
            escape_underscores=False,
        )

        if not markdown or len(markdown.strip()) <= 50:
            return None

        title = self._extract_markdown_heading(markdown)
        if not title:
            title = self._extract_title_from_html(html)

        markdown = self._beautify_markdown_formatting(markdown)

        # Trim lines before H1 (page header noise)
        lines = markdown.split("\n")
        in_fence = False
        h1_line_idx: int | None = None
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("```"):
                in_fence = not in_fence
            elif not in_fence and re.match(r"^#\s+.+$", line):
                h1_line_idx = i
                break
        if h1_line_idx is not None:
            markdown = "\n".join(lines[h1_line_idx:])

        # Metadata extraction
        try:
            metadata = trafilatura.extract_metadata(html, default_url=url)
        except Exception:
            metadata = None

        if metadata is None:
            return self._build_result(
                markdown=markdown.strip(),
                title=title,
            )

        tags: List[str] = []
        if metadata.categories and len(metadata.categories) > 1:
            tags = list(metadata.categories[1:])
        if metadata.tags:
            tags = list(set(tags + metadata.tags))
        tags = _split_keyword_tags(tags)

        author = (metadata.author or "").strip()
        published_at = (metadata.date or "").strip()
        summary = (metadata.description or "").strip()
        category = metadata.categories[0] if metadata.categories else ""

        return self._build_result(
            markdown=markdown.strip(),
            title=title,
            author=author,
            published_at=published_at,
            summary=summary,
            category=category,
            tags=tags,
        )

    # ── Fallback: HTML strip ───────────────────────────────────────

    def _fallback(self, html_text: str, url: str = "") -> Optional[Dict[str, Any]]:
        """Strip HTML tags, collapse whitespace — used when readability fails."""
        title = self._extract_title_from_html(html_text)

        author = self._extract_meta(html_text, r'name=["\']author["\']')
        summary = (
            self._extract_meta(html_text, r'name=["\']description["\']')
            or self._extract_meta(html_text, r'property=["\']og:description["\']')
        )
        published_at = self._extract_meta(
            html_text, r'property=["\']article:published_time["\']'
        )

        text = re.sub(
            r'<(script|style|nav|header|footer|aside)[^>]*>.*?</\1>',
            '',
            html_text,
            flags=re.DOTALL | re.IGNORECASE,
        )

        text = re.sub(r'<[^>]+>', ' ', text)
        text = _html.unescape(text)
        text = re.sub(r'\s+', ' ', text).strip()

        paragraphs = [p.strip() for p in text.split('\n') if len(p.strip()) > 80]
        if paragraphs:
            text = '\n\n'.join(paragraphs)

        if len(text) > 100:
            return self._build_result(
                markdown=text,
                title=title,
                author=author,
                published_at=published_at,
                summary=summary,
            )
        return None

    # ── Metadata extraction utilities ──────────────────────────────

    @staticmethod
    def _extract_meta(html_text: str, attr_pattern: str) -> str:
        """Extract ``content`` attribute from a ``<meta>`` tag matching
        *attr_pattern*."""
        pattern = re.compile(
            r'<meta[^>]*' + attr_pattern + r'[^>]*content=["\']([^"\']+)["\']',
            re.IGNORECASE,
        )
        match = pattern.search(html_text)
        return match.group(1).strip() if match else ""

    @staticmethod
    def _extract_title_from_html(html_text: str) -> str:
        """Extract title from ``og:title`` meta or ``<title>`` tag.

        Prefers ``og:title`` (usually cleaner, without site-name suffix).
        """
        match = re.search(
            r'<meta[^>]*property=["\']og:title["\'][^>]*'
            r'content=["\']([^"\']+)["\']',
            html_text,
            re.IGNORECASE,
        )
        if match:
            return _html.unescape(match.group(1).strip())

        match = re.search(
            r"<title[^>]*>(.*?)</title>",
            html_text,
            re.DOTALL | re.IGNORECASE,
        )
        if match:
            return _html.unescape(match.group(1).strip())
        return ""

    @staticmethod
    def _extract_markdown_heading(markdown: str) -> str:
        """Extract article title from the first H1 heading in markdown.

        Skips ``#`` lines inside fenced code blocks.
        """
        in_fence = False
        for line in markdown.strip().split("\n"):
            stripped = line.strip()
            if stripped.startswith("```"):
                in_fence = not in_fence
            elif not in_fence:
                m = re.match(r"^#\s+(.+?)$", line)
                if m:
                    return m.group(1).strip()
        return ""

    # ── Markdown formatting ────────────────────────────────────────

    @staticmethod
    def _handle_markdown_bold(markdown: str) -> str:
        """Normalize ``**`` bold markers: strip internal spaces, add
        external spaces where markers abut text."""
        parts = markdown.split("**")
        if len(parts) < 2:
            return markdown

        for i in range(1, len(parts), 2):
            parts[i] = parts[i].strip()

        result = [parts[0]]
        for i in range(1, len(parts)):
            prev, cur = parts[i - 1], parts[i]
            if i % 2 == 1:          # entering bold
                if prev and not prev[-1].isspace():
                    result.append(" ")
                result.append(f"**{cur}")
            else:                   # leaving bold
                result.append("**")
                need_space = cur and not cur[0].isspace()
                if need_space or (not cur and i + 1 < len(parts)):
                    result.append(" ")
                result.append(cur)

        return "".join(result)

    @staticmethod
    def _beautify_markdown_formatting(markdown: str) -> str:
        """Post-process trafilatura output: normalize bold formatting and
        remove praise-button noise (``- +1``) from thepaper.cn widgets."""
        markdown = re.sub(r"^- \+1\n+(?=# )", "", markdown, count=1)
        return HtmlParser._handle_markdown_bold(markdown)

    # ── Unified result builder ──────────────────────────────────────

    @staticmethod
    def _build_result(
        markdown: str,
        title: str = "",
        author: str = "",
        published_at: str = "",
        summary: str = "",
        category: str = "",
        tags: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Build a unified result dict from extracted content and metadata."""
        if tags:
            tags = [t.lstrip("#") for t in tags if t]
            tags = [t for t in tags if t]
        return {
            "markdown": markdown,
            "title": title,
            "author": author,
            "published_at": published_at,
            "summary": summary,
            "category": category,
            "tags": tags or [],
        }
```

- [ ] **Step 3: 创建 `news/parser/__init__.py`**

```python
"""News parser framework — HtmlParser 基类 + ParserRegistry 路由."""

from news.parser.parser import HtmlParser
from news.parser.registry import parser_registry

__all__ = ["HtmlParser", "parser_registry"]
```

- [ ] **Step 4: 创建 `news/parser/sites/__init__.py`（占位）**

```python
"""Site-specific parsers — one module per news source."""
```

- [ ] **Step 5: 提交**

```bash
git add news/parser/__init__.py news/parser/parser.py news/parser/registry.py news/parser/sites/__init__.py
git commit -m "feat: create parser framework — HtmlParser base class + ParserRegistry"
```

---

### Task 2: 验证框架可 import + 基类默认行为

**Files:**
- Modify: `news/parser/sites/__init__.py`
- Test: `tests/parser_sites/test_framework.py`

**Interfaces:**
- Consumes: `HtmlParser` from `news/parser.parser`, `parser_registry` from `news/parser.registry`
- Produces: 无新接口

- [ ] **Step 1: 编写框架冒烟测试**

```python
"""Smoke tests for HtmlParser base class and ParserRegistry routing."""
import pytest
from news.parser.parser import HtmlParser, _split_keyword_tags
from news.parser.registry import parser_registry, ParserRegistry


class TestHtmlParserBase:
    """Base class default behavior — no site-specific hooks."""

    def test_parse_empty_html_returns_none(self):
        parser = HtmlParser()
        assert parser.parse("") is None
        assert parser.parse("   ") is None

    def test_parse_trivial_html_falls_back(self):
        html = "<html><head><title>Test</title></head><body>" + "<p>content " * 30 + "</p></body></html>"
        parser = HtmlParser()
        result = parser.parse(html, url="https://example.com")
        assert result is not None
        assert result["markdown"]
        assert "content" in result["markdown"]

    def test_extract_title_from_og_title(self):
        html = '<html><head><meta property="og:title" content="OG标题"></head><body></body></html>'
        title = HtmlParser._extract_title_from_html(html)
        assert title == "OG标题"

    def test_extract_title_from_title_tag(self):
        html = '<html><head><title>页面标题 - 网站名</title></head><body></body></html>'
        title = HtmlParser._extract_title_from_html(html)
        assert "页面标题" in title

    def test_extract_meta_author(self):
        html = '<html><head><meta name="author" content="张三"></head><body></body></html>'
        author = HtmlParser._extract_meta(html, r'name=["\']author["\']')
        assert author == "张三"

    def test_extract_markdown_heading(self):
        md = "# 文章标题\n\n正文内容"
        title = HtmlParser._extract_markdown_heading(md)
        assert title == "文章标题"

    def test_extract_markdown_heading_skips_code_fence(self):
        md = "```bash\n# 这是注释\n```\n# 真正的标题\n\n正文"
        title = HtmlParser._extract_markdown_heading(md)
        assert title == "真正的标题"

    def test_build_result(self):
        result = HtmlParser._build_result(
            markdown="测试正文",
            title="测试标题",
            author="作者",
            published_at="2026-06-24",
            summary="摘要",
            tags=["科技", "AI"],
        )
        assert result["markdown"] == "测试正文"
        assert result["title"] == "测试标题"
        assert result["author"] == "作者"
        assert result["tags"] == ["科技", "AI"]

    def test_build_result_strips_tag_hash_prefix(self):
        result = HtmlParser._build_result(
            markdown="x",
            tags=["#科技", "#经济", ""],
        )
        assert result["tags"] == ["科技", "经济"]

    def test_max_content_length_truncation(self):
        parser = HtmlParser({"crawler": {"max_content_length": 50}})
        result = parser.parse("x" * 100)
        # Empty HTML won't parse, so test via direct parse
        html = "<html><head><title>T</title></head><body>" + "<p>" + "word " * 200 + "</p></body></html>"
        result = parser.parse(html)
        if result:
            assert len(result["markdown"]) <= 50 + len("\n\n... (truncated)")


class TestSplitKeywordTags:
    def test_comma_separated(self):
        assert _split_keyword_tags(["科技,AI, 经济"]) == ["科技", "AI", "经济"]

    def test_space_separated(self):
        assert _split_keyword_tags(["科技 AI 经济"]) == ["科技", "AI", "经济"]

    def test_dedup_preserves_order(self):
        assert _split_keyword_tags(["科技,AI,科技"]) == ["科技", "AI"]


class TestParserRegistry:
    """Test routing behavior."""

    def test_registered_source_id_routes_to_correct_parser(self):
        reg = ParserRegistry()
        reg.set_default(HtmlParser())

        class DummyParser(HtmlParser):
            def _extract(self, html, url):
                return self._build_result(markdown="dummy result")

        reg.register("dummy", DummyParser())
        result = reg.parse("dummy", "<html></html>", "")
        assert result is not None
        assert result["markdown"] == "dummy result"

    def test_unregistered_source_id_falls_back_to_default(self):
        reg = ParserRegistry()
        reg.set_default(HtmlParser())
        html = "<html><head><title>Fallback</title></head><body>" + "<p>text " * 30 + "</p></body></html>"
        result = reg.parse("unknown-source", html, "")
        assert result is not None
        # Default HtmlParser uses readability → fallback
        assert len(result["markdown"]) > 50
```

- [ ] **Step 2: 运行测试验证通过**

```bash
pytest tests/parser_sites/test_framework.py -v
```

- [ ] **Step 3: 提交**

```bash
git add tests/parser_sites/__init__.py tests/parser_sites/test_framework.py
git commit -m "test: add HtmlParser base class and registry smoke tests"
```

---

### Task 3: Crawler 适配 — 从 `self.parser` 迁移到 `parser_registry`

**Files:**
- Modify: `news/crawler.py:28,71,81,318,512`

**Interfaces:**
- Consumes: `parser_registry` from `news.parser`
- Produces: 无

- [ ] **Step 1: 修改 Crawler import 和构造**

将 `news/crawler.py` 第 28 行的:
```python
from news.parser import HtmlParser
```
改为:
```python
from news.parser import parser_registry
```

将第 71 行:
```python
        parser: HtmlParser | None = None,
```
改为:
```python
        parser_registry_obj: object | None = None,
```

将第 81 行:
```python
        self.parser = parser or HtmlParser(config)
```
改为:
```python
        self.parser_registry = parser_registry_obj or parser_registry
```

- [ ] **Step 2: 修改 Crawler._download_and_parse**

将第 512 行:
```python
        parsed = self.parser.parse(resp.text, url)
```
改为:
```python
        parsed = self.parser_registry.parse(item["source_id"], resp.text, url)
```

- [ ] **Step 3: 修改 Crawler.fetch (single URL)**

将第 318 行:
```python
        parsed = self.parser.parse(resp.text, url)
```
改为:
```python
        parsed = self.parser_registry.parse(item["source_id"], resp.text, url)
```

- [ ] **Step 4: 验证 Crawler 仍然可 import**

```bash
python -c "from news.crawler import Crawler; print('OK')"
```

- [ ] **Step 5: 提交**

```bash
git add news/crawler.py
git commit -m "refactor: switch Crawler from HtmlParser to ParserRegistry"
```

---

### Task 4: 迁移 ThepaperParser（澎湃新闻）

**Files:**
- Create: `news/parser/sites/thepaper.py`
- Create: `tests/parser_sites/fixtures/thepaper.html`
- Create: `tests/parser_sites/test_thepaper.py`

**Interfaces:**
- Consumes: `HtmlParser` from `news.parser.parser`
- Produces: `ThepaperParser` class

- [ ] **Step 1: 从旧 parser 提取 thepaper 的解析逻辑，创建 `ThepaperParser`**

回顾旧 `_extract_spa_data()` 中对 thepaper 的处理：
1. `_find_json_candidates()` 找 `__NEXT_DATA__`
2. `_find_article_in_json()` 递归找 article
3. `_fix_lazy_images()` 处理 `data-src` 懒加载
4. `_extract_with_readability(skip_trim=True)` 转 markdown

把这些逻辑直接实现在 `ThepaperParser._extract()` 中：

```python
"""ThepaperParser — 澎湃新闻 (thepaper.cn) HTML → Markdown 解析."""

from __future__ import annotations

import json
import re
import html as _html

from typing import Any, Dict, Optional
from readability import Document
from markdownify import markdownify as _md

from news.parser.parser import HtmlParser


class ThepaperParser(HtmlParser):
    """澎湃新闻解析器 — 从 __NEXT_DATA__ JSON 提取正文 HTML。

    澎湃新闻的文章页使用 Next.js SSR，正文 HTML 嵌入在
    ``<script id="__NEXT_DATA__" type="application/json">`` 中。
    """

    def _extract(self, html: str, url: str = "") -> Optional[Dict[str, Any]]:
        # 1. Find __NEXT_DATA__ JSON candidates
        for candidate in self._find_next_data_candidates(html):
            article = self._find_article_in_json(candidate)
            if not article:
                continue
            if not self._is_valid_article(article):
                continue

            # 2. Fix lazy images (data-src → src) for thepaper
            content_html = self._fix_lazy_images(article["content"])

            # 3. Wrap bare <img> in <p> so readability preserves them
            content_html = re.sub(
                r'(?<!>)\s*<img\s',
                '<p><img ',
                content_html,
                count=1,
            )
            content_html = re.sub(
                r'<img([^>]*?)>\s*(?!<)',
                r'<img\1></p>',
                content_html,
            )

            # 4. Convert to markdown via readability
            try:
                doc = Document(content_html, url=url)
                article_html = doc.summary()
            except Exception:
                continue

            if not article_html or not article_html.strip():
                continue

            markdown = _md(
                article_html,
                heading_style="ATX",
                strip=["script", "style"],
                escape_asterisks=False,
                escape_underscores=False,
            )

            if not markdown or len(markdown.strip()) <= 50:
                # Image-heavy content fallback
                markdown = self._build_image_markdown(content_html)
                if not markdown or len(markdown.strip()) <= 50:
                    continue

            markdown = self._beautify_markdown_formatting(markdown)

            # 5. Metadata: JSON title has priority
            title = article.get("title", "")
            if not title:
                title = self._extract_title_from_html(html)

            # Summary: JSON description → og:description
            summary = article.get("description", "")
            if not summary:
                summary = self._extract_meta(
                    html, r'name=["\']description["\']'
                ) or self._extract_meta(
                    html, r'property=["\']og:description["\']'
                )

            published_at = article.get("datePublished", "")
            tags = article.get("keywords", [])
            if isinstance(tags, str):
                tags = [t.strip() for t in tags.split(",") if t.strip()]

            return self._build_result(
                markdown=markdown.strip(),
                title=title,
                published_at=published_at,
                summary=summary,
                tags=tags,
            )

        return None

    # ── Internal helpers ────────────────────────────────────────────

    @staticmethod
    def _find_next_data_candidates(html_text: str):
        """Yield JSON objects from __NEXT_DATA__ script tags/assignments."""
        # Script tag form: <script id="__NEXT_DATA__" type="application/json">
        pattern = re.compile(
            r'<script[^>]*id=["\']__NEXT_DATA__["\'][^>]*'
            r'type=["\']application/json["\'][^>]*>(.*?)</script>',
            re.DOTALL,
        )
        for match in pattern.finditer(html_text):
            try:
                yield json.loads(match.group(1).strip())
            except (json.JSONDecodeError, ValueError):
                continue

        # JS assignment form: __NEXT_DATA__ = {...}
        pattern = re.compile(
            r'__NEXT_DATA__\s*=\s*(\{.*?\});\s*$',
            re.MULTILINE | re.DOTALL,
        )
        for match in pattern.finditer(html_text):
            # Extract from the outermost brace
            text = match.group(1)
            obj = ThepaperParser._extract_bracketed_json(text, r'^(\{)')
            for o in obj:
                yield o

    @staticmethod
    def _find_article_in_json(data):
        """Recursively find article object in JSON tree."""
        if isinstance(data, dict):
            if "title" in data and ("content" in data or "articleBody" in data):
                content = data.get("content") or data.get("articleBody")
                if isinstance(content, str) and len(content) >= 50:
                    return data
                if data.get("articleBody") and isinstance(data["articleBody"], str) and len(data["articleBody"]) >= 50:
                    return data
            # Special case for json-ld type
            if data.get("@type") == "Article" and "headline" in data and "articleBody" in data:
                if len(data["articleBody"]) >= 50:
                    return data
            # Recurse
            best = None
            best_len = 0
            for v in data.values():
                found = ThepaperParser._find_article_in_json(v)
                if found:
                    content = found.get("content") or found.get("articleBody", "")
                    if len(content) > best_len:
                        best = found
                        best_len = len(content)
            return best
        elif isinstance(data, list):
            for item in data:
                found = ThepaperParser._find_article_in_json(item)
                if found:
                    return found
        return None

    @staticmethod
    def _is_valid_article(article: dict) -> bool:
        """Check if article dict has enough content."""
        content = article.get("content") or article.get("articleBody", "")
        return isinstance(content, str) and len(content) > 100

    @staticmethod
    def _fix_lazy_images(html_text: str) -> str:
        """Convert lazy-loaded ``data-src`` to ``src`` for thepaper.cn."""
        for data_attr in ("data-src", "data-original"):
            # data-attr appears before src
            html_text = re.sub(
                rf'<img([^>]*)\s+{data_attr}="([^"]+)"([^>]*)\s+src="[^"]*"',
                rf'<img\1 src="\2"\3',
                html_text,
            )
            # data-attr appears after src
            html_text = re.sub(
                rf'<img([^>]*)\s+src="[^"]*"([^>]*)\s+{data_attr}="([^"]+)"',
                rf'<img\1 src="\3"\2',
                html_text,
            )
        return html_text

    @staticmethod
    def _extract_bracketed_json(text: str, start_pattern: str):
        """Extract JSON objects from text starting with *start_pattern*."""
        match = re.search(start_pattern, text)
        if not match:
            return []
        start = match.start(1)
        depth = 0
        end = start
        for i, ch in enumerate(text[start:], start):
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end <= start:
            return []
        try:
            return [json.loads(text[start:end])]
        except (json.JSONDecodeError, ValueError):
            return []

    @staticmethod
    def _build_image_markdown(html_text: str) -> str:
        """Build markdown from image-heavy HTML when readability fails."""
        imgs = re.findall(
            r'<img[^>]+src=["\']([^"\']+)["\'][^>]*>',
            html_text, re.IGNORECASE,
        )
        text = re.sub(r'<[^>]+>', '', html_text)
        text = _html.unescape(text)
        text = re.sub(r'\s+', ' ', text).strip()

        parts = [f'![]({url})' for url in imgs]
        if text:
            parts.append(text)
        return '\n\n'.join(parts)
```

- [ ] **Step 2: 保存真实 HTML fixture**

```bash
python -c "
import requests
resp = requests.get('https://www.thepaper.cn/newsDetail_forward_30896343', headers={'User-Agent': 'Mozilla/5.0'}, timeout=30)
with open('tests/parser_sites/fixtures/thepaper.html', 'w') as f:
    f.write(resp.text)
print(f'Saved {len(resp.text)} bytes')
"
```

- [ ] **Step 3: 编写 ThepaperParser 测试**

```python
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
```

- [ ] **Step 4: 注册 ThepaperParser**

在 `news/parser/sites/__init__.py` 添加：

```python
from news.parser.sites.thepaper import ThepaperParser
from news.parser.registry import parser_registry

parser_registry.register("thepaper", ThepaperParser())
```

- [ ] **Step 5: 运行测试**

```bash
pytest tests/parser_sites/test_thepaper.py -v
```

- [ ] **Step 6: 在线验证**

```bash
python -m cli grab-one "https://www.thepaper.cn/newsDetail_forward_30896343" --output-style markdown
```

- [ ] **Step 7: 提交**

```bash
git add news/parser/sites/thepaper.py news/parser/sites/__init__.py tests/parser_sites/fixtures/thepaper.html tests/parser_sites/test_thepaper.py
git commit -m "feat: add ThepaperParser — __NEXT_DATA__ JSON extraction"
```

---

### Task 5: 迁移 IfengParser（凤凰网）

**Files:**
- Create: `news/parser/sites/ifeng.py`
- Create: `tests/parser_sites/fixtures/ifeng.html`
- Create: `tests/parser_sites/test_ifeng.py`
- Modify: `news/parser/sites/__init__.py`

**Interfaces:**
- Consumes: `HtmlParser` from `news.parser.parser`
- Produces: `IfengParser` class — 只覆写 `_preprocess()`

- [ ] **Step 1: 创建 IfengParser**

从旧 `_handle_ifeng()` 提取 DOM 清理逻辑：

```python
"""IfengParser — 凤凰网 (ifeng.com) HTML → Markdown 解析."""

from __future__ import annotations

from typing import Any, Dict, Optional

from lxml import html as lxml_html

from news.parser.parser import HtmlParser


class IfengParser(HtmlParser):
    """凤凰网解析器 — DOM 预处理后走 readability 降级链。

    凤凰网文章页在正文前后插入了大量 UI 噪声：
    - #lowBrowerBoxFixed（浏览器升级提示）
    - .index_info_*（头像、来源名、日期、分享按钮）
    - .index_devide_*（分隔线）
    - .index_copyRight_*（版权信息/页脚）

    这些 DOM 元素必须在 readability 提取之前删除。
    """

    def _preprocess(self, html: str, url: str) -> str:
        """Remove ifeng-specific template noise from HTML before extraction."""
        try:
            tree = lxml_html.fromstring(html)
        except Exception:
            return html

        removed = False

        # Browser upgrade prompt at page bottom
        for el in tree.xpath("//*[@id='lowBrowerBoxFixed']"):
            parent = el.getparent()
            if parent is not None:
                parent.remove(el)
                removed = True

        # Meta info bar: avatar, source name, "独家抢先看", date, share btns
        for el in tree.xpath("//div[contains(@class, 'index_info_')]"):
            parent = el.getparent()
            if parent is not None:
                parent.remove(el)
                removed = True

        # Divider between meta bar and article body
        for el in tree.xpath("//div[contains(@class, 'index_devide_')]"):
            parent = el.getparent()
            if parent is not None:
                parent.remove(el)
                removed = True

        # Copyright / footer at bottom of article
        for el in tree.xpath("//div[contains(@class, 'index_copyRight_')]"):
            parent = el.getparent()
            if parent is not None:
                parent.remove(el)
                removed = True

        if removed:
            return lxml_html.tostring(tree, encoding="unicode")
        return html
```

- [ ] **Step 2: 注册 IfengParser**

```python
# 在 sites/__init__.py 添加
from news.parser.sites.ifeng import IfengParser
parser_registry.register("ifeng", IfengParser())
```

- [ ] **Step 3: 编写测试 + 提交**

（结构同 Task 4，此处省略重复模板——测试 fixture 需要真实 ifeng 文章 HTML）

- [ ] **Step 4: 提交**

```bash
git add news/parser/sites/ifeng.py news/parser/sites/__init__.py tests/parser_sites/test_ifeng.py tests/parser_sites/fixtures/ifeng.html
git commit -m "feat: add IfengParser — DOM noise removal preprocessing"
```

---

### Task 6: 迁移 CkxxappParser（参考新闻/新华社）

**Files:**
- Create: `news/parser/sites/cankaoxiaoxi.py`
- Create: `tests/parser_sites/fixtures/cankaoxiaoxi.html`
- Create: `tests/parser_sites/test_cankaoxiaoxi.py`
- Modify: `news/parser/sites/__init__.py`

**Interfaces:**
- Consumes: `HtmlParser` from `news.parser.parser`
- Produces: `CkxxappParser` class

- [ ] **Step 1: 创建 CkxxappParser**

```python
"""CkxxappParser — 参考新闻 (ckxxapp.ckxx.net) / 新华社客户端 HTML → Markdown 解析."""

from __future__ import annotations

import re
import html as _html

from typing import Any, Dict, Optional
from readability import Document
from markdownify import markdownify as _md

from news.parser.parser import HtmlParser


class CkxxappParser(HtmlParser):
    """参考新闻 / 新华社客户端解析器。

    使用 xinhuamm.net CMS 模板——文章正文 HTML 嵌入在
    ``<script>`` 标签内的 ``var contentTxt = "...";`` 变量中。
    双引号被 JS-escape 为 ``\"``，``</`` 被写为 ``<\/`` 以避免
    提前关闭 script 标签。
    """

    def _extract(self, html: str, url: str = "") -> Optional[Dict[str, Any]]:
        content_html = self._extract_js_content_vars(html)
        if not content_html:
            return None

        # Convert to markdown via readability (skip_trim — already clean)
        try:
            doc = Document(content_html, url=url)
            article_html = doc.summary()
        except Exception:
            return None

        if not article_html or not article_html.strip():
            return None

        markdown = _md(
            article_html,
            heading_style="ATX",
            strip=["script", "style"],
            escape_asterisks=False,
            escape_underscores=False,
        )

        if not markdown or len(markdown.strip()) <= 50:
            return None

        markdown = self._beautify_markdown_formatting(markdown)

        # Metadata from page HTML
        title = self._extract_title_from_html(html)
        summary = (
            self._extract_meta(html, r'name=["\']description["\']')
            or self._extract_meta(html, r'property=["\']og:description["\']')
        )
        published_at = self._extract_meta(
            html, r'property=["\']article:published_time["\']'
        )
        author = self._extract_meta(html, r'name=["\']author["\']')

        return self._build_result(
            markdown=markdown.strip(),
            title=title,
            author=author,
            published_at=published_at,
            summary=summary,
        )

    @staticmethod
    def _extract_js_content_vars(html_text: str) -> Optional[str]:
        """Extract article content HTML from JS string variables in inline scripts."""
        for var_name in ("contentTxt",):
            pattern = re.compile(
                rf'var\s+{var_name}\s*=\s*"((?:[^"\\]|\\.)*)"',
                re.DOTALL,
            )
            match = pattern.search(html_text)
            if not match:
                continue
            content = match.group(1)
            content = content.replace(r'\"', '"')
            content = content.replace(r'\/', '/')
            content = _html.unescape(content)
            if content and len(content) > 50:
                return content
        return None
```

- [ ] **Step 2: 注册 + 测试 + 提交**

（测试已有 `tests/test_parser_spa.py::TestExtractJsContentVars` 格式可参考，在线验证 URL 已确认可用）

```bash
git add news/parser/sites/cankaoxiaoxi.py news/parser/sites/__init__.py tests/parser_sites/test_cankaoxiaoxi.py tests/parser_sites/fixtures/cankaoxiaoxi.html
git commit -m "feat: add CkxxappParser — JS variable content extraction"
```

---

### Task 7-14: 其余 8 个站点 Parser

**通用流程（每个站点重复以下步骤）：**

1. 用 `grab-one` 抓取真实文章 URL，保存 HTML fixture
2. 分析 HTML 结构：正文在哪？是否需要特殊处理？
3. 如果 readability 能直接覆盖 → 空子类，文件存在即可
4. 如果需要覆写 → 实现 `_extract()` 或 `_preprocess()`
5. 编写离线测试
6. 在线验证 markdown 质量
7. 注册到 `sites/__init__.py`

**站点清单：**

| # | source_id | 类名 | 文件 | 可能需要的处理 |
|---|-----------|------|------|---------------|
| 7 | `wallstreetcn-hot`, `wallstreetcn-news` | `WallstreetcnParser` | `wallstreetcn.py` | `__SSR__` JSON、blockquote-wrapped images |
| 8 | `cls-hot`, `cls-depth` | `ClsParser` | `cls.py` | 待验证（grab-one 分析） |
| 9 | `zaobao` | `ZaobaoParser` | `zaobao.py` | 待验证 |
| 10 | `kaopu` | `KaopuParser` | `kaopu.py` | 待验证 |
| 11 | `fastbull-news` | `FastbullParser` | `fastbull.py` | 待验证 |
| 12 | `ithome` | `IthomeParser` | `ithome.py` | 待验证（可能有懒加载图片） |
| 13 | `sspai` | `SspaiParser` | `sspai.py` | 待验证 |
| 14 | `juejin` | `JuejinParser` | `juejin.py` | 待验证（可能是 SPA） |

每个站点 Parser 的最小骨架：

```python
"""XxxParser — XX网站 HTML → Markdown 解析."""

from __future__ import annotations

from typing import Any, Dict, Optional

from news.parser.parser import HtmlParser


class XxxParser(HtmlParser):
    """XX网站解析器。

    （描述该站点的 HTML 结构特点和提取策略）
    """

    def _extract(self, html: str, url: str = "") -> Optional[Dict[str, Any]]:
        # TODO: 分析站点 HTML 后实现
        return None
```

---

### Task 15: 清理 — 删除旧 `news/parser.py`

**Files:**
- Delete: `news/parser.py`（旧的单体文件）

- [ ] **Step 1: 迁移仍在使用旧 parser 的测试**

旧测试文件依赖 `from news.parser import HtmlParser`。因为 `news/parser/__init__.py` 导出同名的 `HtmlParser`，import 路径不变，所有旧测试应继续工作。

验证：

```bash
pytest tests/test_parser_*.py -v --tb=short
```

需要调整的测试：
- `tests/test_parser_spa.py` — 其中的 `_extract_spa_data` 调用需要迁移到站点 Parser 测试中
- `tests/test_parser_trim_noise.py` — `_trim_noise` / `Block` 相关测试可能需要迁移或删除
- `tests/test_parser_lazy_images.py` — `_fix_lazy_images` 测试迁移到站点 Parser 测试中

- [ ] **Step 2: 删除旧文件**

```bash
git rm news/parser.py
```

- [ ] **Step 3: 运行全量测试**

```bash
pytest --cov=news/parser --cov-report=term-missing
```

确保覆盖率 ≥ 80%。

- [ ] **Step 4: 提交**

```bash
git commit -m "refactor: remove old monolithic parser.py"
```

---

## 全局提交序列

```
feat: create parser framework — HtmlParser base class + ParserRegistry
test: add HtmlParser base class and registry smoke tests
refactor: switch Crawler from HtmlParser to ParserRegistry
feat: add ThepaperParser — __NEXT_DATA__ JSON extraction
feat: add IfengParser — DOM noise removal preprocessing
feat: add CkxxappParser — JS variable content extraction
feat: add WallstreetcnParser — __SSR__ JSON extraction
feat: add ClsParser
feat: add ZaobaoParser
feat: add KaopuParser
feat: add FastbullParser
feat: add IthomeParser
feat: add SspaiParser
feat: add JuejinParser
refactor: remove old monolithic parser.py
```
