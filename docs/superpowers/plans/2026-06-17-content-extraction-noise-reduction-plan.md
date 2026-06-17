# 正文提取噪音削减 — 掐头去尾 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 trafilatura 提取前对 HTML 做“掐头去尾”预处理，剔除正文前后的 UI 噪音（页脚版权、分享按钮、面包屑残留等），保留中间正文区域。

**Architecture:** 在 `HtmlParser._extract_with_trafilatura` 中新增 `_trim_noise` 预处理步骤。用 lxml 解析 DOM，按文档序提取候选块级内容节点，通过文本长度和链接密度两个信号从前向后找正文起点、从后向前找正文终点，保留中间部分喂给 trafilatura。检测失败时退化回原始 HTML。

**Tech Stack:** Python 3.12, lxml, trafilatura, pytest

## Global Constraints

- 不引入新依赖（lxml 已被 trafilatura 依赖）
- 只在 trafilatura 路径生效，`_fallback` 和 `_extract_spa_data` 不受影响
- 检测失败时完全退化，不改变现有行为
- 保持文件 < 800 行（当前 parser.py 约 500 行，加约 70 行后仍在范围内）

---

## File Structure

```
news/parser.py          ← 修改：新增 Block dataclass + _trim_noise + 集成到 _extract_with_trafilatura
tests/test_parser.py    ← 新建：_trim_noise 单元测试
```

---

### Task 1: Write failing tests for `_trim_noise`

**Files:**
- Create: `tests/__init__.py`
- Create: `tests/test_parser.py`

**Interfaces:**
- Produces: 测试用例验证 `HtmlParser._trim_noise` 的边界检测行为
- 后续 Task 2/3 依赖此处定义的 `_trim_noise` 行为

- [ ] **Step 1: Create tests directory and test file skeleton**

```bash
mkdir -p tests
```

Create `tests/__init__.py` (empty):

```python
```

Create `tests/test_parser.py`:

```python
"""Tests for HtmlParser._trim_noise — head/tail noise trimming."""

import pytest
from news.parser import HtmlParser


def _make_html(body: str, head_noise: str = "", tail_noise: str = "") -> str:
    """Build a minimal HTML page with optional head/tail noise around body."""
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


class TestTrimNoise:
    """Tests for _trim_noise boundary detection."""

    def test_keeps_paragraph_body(self):
        """Body paragraphs should be fully retained."""
        body = "<p>" + "新闻正文内容。" * 20 + "</p>"
        html = _make_html(body)
        parser = HtmlParser()
        result = parser._trim_noise(html)
        assert result is not None
        assert "新闻正文内容" in result
        # Should not lose body content
        assert result.count("新闻正文内容") == 20

    def test_trims_footer_copyright(self):
        """Short link-heavy footer after body should be trimmed."""
        body = "<p>" + "新闻正文内容。" * 20 + "</p>"
        tail = """<footer>
<p>版权所有 © 2024 某某网</p>
<p><a href="/about">关于我们</a> | <a href="/contact">联系我们</a></p>
</footer>"""
        html = _make_html(body, tail_noise=tail)
        parser = HtmlParser()
        result = parser._trim_noise(html)
        assert result is not None
        assert "版权所有" not in result
        assert "新闻正文内容" in result

    def test_trims_head_navigation(self):
        """Short link-heavy nav before body should be trimmed."""
        head = """<nav>
<a href="/">首页</a> | <a href="/news">新闻</a> | <a href="/about">关于</a>
</nav>
<p>面包屑：首页 &gt; 新闻</p>"""
        body = "<p>" + "新闻正文内容。" * 20 + "</p>"
        html = _make_html(body, head_noise=head)
        parser = HtmlParser()
        result = parser._trim_noise(html)
        assert result is not None
        assert "面包屑" not in result
        assert "新闻正文内容" in result

    def test_trims_share_buttons_before_body(self):
        """Share button text before body should be trimmed."""
        head = '<p>分享到：<a href="#">微信</a> <a href="#">微博</a></p>'
        body = "<p>" + "新闻正文内容。" * 20 + "</p>"
        html = _make_html(body, head_noise=head)
        parser = HtmlParser()
        result = parser._trim_noise(html)
        assert result is not None
        assert "分享到" not in result

    def test_short_page_degrades_to_none(self):
        """Page with too few blocks should return None."""
        html = _make_html("<p>短。</p>")
        parser = HtmlParser()
        result = parser._trim_noise(html)
        assert result is None

    def test_malformed_html_degrades_to_none(self):
        """Malformed HTML should not crash, return None."""
        parser = HtmlParser()
        result = parser._trim_noise("not even html")
        assert result is None

    def test_body_with_heading_kept(self):
        """Body with an h2 heading should be kept — heading is a start signal."""
        head = "<p>短导航</p>"
        body = "<h2>重要标题</h2><p>" + "正文内容。" * 20 + "</p>"
        html = _make_html(body, head_noise=head)
        parser = HtmlParser()
        result = parser._trim_noise(html)
        assert result is not None
        assert "重要标题" in result

    def test_link_density_detects_noise(self):
        """A block with high link density should be treated as noise."""
        head = '<p><a href="/a">链接1</a> <a href="/b">链接2</a> <a href="/c">链接3</a></p>'
        body = "<p>" + "正文内容。" * 20 + "</p>"
        html = _make_html(body, head_noise=head)
        parser = HtmlParser()
        result = parser._trim_noise(html)
        assert result is not None
        assert "链接1" not in result
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/llianc62/ws/NewsRadar && python -m pytest tests/test_parser.py -v`
Expected: All tests FAIL with `AttributeError: 'HtmlParser' object has no attribute '_trim_noise'`

- [ ] **Step 3: Commit**

```bash
git add tests/__init__.py tests/test_parser.py
git commit -m "test: add failing tests for _trim_noise boundary detection"
```

---

### Task 2: Implement `_trim_noise` method

**Files:**
- Modify: `news/parser.py` — add `Block` dataclass at module level, add `_trim_noise` method to `HtmlParser`

**Interfaces:**
- Consumes: (none — new code)
- Produces: `Block` dataclass, `HtmlParser._trim_noise(html: str) -> Optional[str]`

- [ ] **Step 1: Add lxml import and Block dataclass**

In `news/parser.py`, after the existing imports (line 13), add:

```python
from dataclasses import dataclass

from lxml import html as lxml_html
from lxml.etree import ParseError
```

After the imports, before the `HtmlParser` class, add the `Block` dataclass:

```python
# ═══════════════════════════════════════════════════════════════════
# Block — extracted block-level content node
# ═══════════════════════════════════════════════════════════════════

BLOCK_TAGS = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "ul", "ol", "pre"}


@dataclass
class Block:
    """A block-level content node extracted from DOM for boundary detection."""

    tag: str
    text: str
    text_len: int
    link_density: float
    html: str  # original inner HTML, preserved for trafilatura
```

- [ ] **Step 2: Add `_extract_blocks` static method to HtmlParser**

Inside `HtmlParser`, before `_trim_noise`, add the block extraction method:

```python
    @staticmethod
    def _extract_blocks(tree) -> List["Block"]:
        """Extract block-level content nodes from lxml tree in document order.

        Only outermost block nodes are included — nested blocks (e.g.
        ``<blockquote><p>...</p></blockquote>``) yield only the parent,
        avoiding duplicate content.

        Each block's ``html`` is the full serialized element including its
        tag, so the reassembled fragment is valid HTML for trafilatura.
        """
        blocks: List[Block] = []
        for el in tree.iter():
            tag = el.tag if isinstance(el.tag, str) else ""
            if tag not in BLOCK_TAGS:
                continue

            # skip nested blocks — only keep the outermost block ancestor
            parent = el.getparent()
            if parent is not None:
                parent_tag = parent.tag if isinstance(parent.tag, str) else ""
                if parent_tag in BLOCK_TAGS:
                    continue

            text_content = el.text_content()
            text = " ".join(text_content.split())
            text_len = len(text)
            if text_len == 0:
                continue

            # calculate link density: ratio of link text to total text
            link_text = " ".join(
                a.text_content() for a in el.iter("a")
                if a.text_content()
            )
            link_text = " ".join(link_text.split())
            link_len = len(link_text)
            link_density = link_len / text_len if text_len > 0 else 0.0

            # serialize the full element (including its tag) for reassembly
            element_html = lxml_html.tostring(el, encoding="unicode")

            blocks.append(Block(
                tag=tag,
                text=text,
                text_len=text_len,
                link_density=link_density,
                html=element_html,
            ))
        return blocks
```

- [ ] **Step 3: Add `_trim_noise` method to HtmlParser**

```python
    @staticmethod
    def _trim_noise(html: str) -> Optional[str]:
        """Trim head/tail noise from HTML before feeding to trafilatura.

        Extracts block-level content nodes, finds the first "real content"
        block (the head boundary) and the last (the tail boundary), and
        returns only the HTML between them.

        Returns None when boundaries cannot be reliably detected — callers
        should fall back to the original HTML.
        """
        try:
            tree = lxml_html.fromstring(html)
        except ParseError:
            return None

        blocks = HtmlParser._extract_blocks(tree)

        # too few blocks — not worth trimming
        if len(blocks) < 3:
            return None

        # ── Find start (trim head) ──────────────────────────────────
        start = 0
        for i, b in enumerate(blocks):
            if b.text_len >= 80 and b.link_density < 0.3:
                start = i
                break
            if b.tag in ("h1", "h2", "h3") and b.text_len >= 10:
                start = i
                break

        # ── Find end (trim tail) ────────────────────────────────────
        end = len(blocks) - 1
        for i in range(len(blocks) - 1, -1, -1):
            b = blocks[i]
            if b.text_len >= 50 and b.link_density < 0.3:
                end = i
                break
            if b.tag in ("h1", "h2", "h3", "h4") and b.text_len >= 10:
                end = i
                break

        # No boundaries detected — degrade
        if start == 0 and end == len(blocks) - 1:
            return None

        # Overlap — degrade
        if start > end:
            return None

        # ── Reassemble ──────────────────────────────────────────────
        return "".join(b.html for b in blocks[start:end + 1])
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/llianc62/ws/NewsRadar && python -m pytest tests/test_parser.py -v`
Expected: All 8 tests PASS

- [ ] **Step 5: Commit**

```bash
git add news/parser.py
git commit -m "feat: add _trim_noise method for head/tail noise removal"
```

---

### Task 3: Integrate `_trim_noise` into `_extract_with_trafilatura`

**Files:**
- Modify: `news/parser.py:83-97` — `_extract_with_trafilatura` method

**Interfaces:**
- Consumes: `HtmlParser._trim_noise(html: str) -> Optional[str]`
- Produces: Modified `_extract_with_trafilatura` that preprocesses HTML before trafilatura

- [ ] **Step 1: Modify `_extract_with_trafilatura` to call `_trim_noise`**

In `news/parser.py`, replace `_extract_with_trafilatura` (lines 83-140) with:

```python
    def _extract_with_trafilatura(self, html: str, url: str) -> Optional[Dict[str, Any]]:
        """Use trafilatura for content + metadata extraction.

        HTML is first preprocessed by :meth:`_trim_noise` to remove
        head/tail UI noise (nav, footer, share buttons, etc.) before
        extraction.
        """

        title = self._extract_title_from_html(html)

        # ── Preprocess: trim head/tail noise ──────────────────────────
        clean_html = self._trim_noise(html)
        source_html = clean_html if clean_html is not None else html

        markdown = trafilatura.extract(
            source_html,
            url=url,
            output_format="markdown",
            include_tables=True,
            include_images=True,
            include_links=True,
            include_formatting=True,
            with_metadata=False,
        )

        # 如果正文小于50个字符，就默认是无效文档。
        if not markdown or len(markdown.strip()) <= 50:
            return None

        # 标题来源：正文 H1（干净无后缀） > HTML <title>/og:title
        title =  self._extract_markdown_heading(markdown) or title

        # 优化 markdown 文本
        markdown = self._beautify_markdown_formatting(markdown)

        # 元数据提取（轻量，只解析 head/meta/JSON-LD）
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
        if metadata and metadata.categories and len(metadata.categories) > 1:
            tags = list(metadata.categories[1:])
        if metadata and metadata.tags:
            tags = list(set(tags + metadata.tags))

        author = (metadata.author or "").strip() if metadata else ""
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
```

**What changed:** Only three lines added (after the `title = ...` line):
```python
        # ── Preprocess: trim head/tail noise ──────────────────────────
        clean_html = self._trim_noise(html)
        source_html = clean_html if clean_html is not None else html
```
And `source_html` replaces `html` as the first argument to `trafilatura.extract`.

- [ ] **Step 2: Run all parser tests**

Run: `cd /home/llianc62/ws/NewsRadar && python -m pytest tests/test_parser.py -v`
Expected: All 8 tests PASS

- [ ] **Step 3: Smoke test with real trafilatura**

Run: `cd /home/llianc62/ws/NewsRadar && python -c "
from news.parser import HtmlParser
parser = HtmlParser()

# Test with a realistic article HTML that has footer noise
html = '''<!DOCTYPE html>
<html><head><title>Test</title></head>
<body>
<header><nav><a href=\"/\">Home</a></nav></header>
<article>
<h1>Article Title</h1>
<p>''' + 'This is the article body content. ' * 30 + '''</p>
<p>Another meaningful paragraph with enough text to pass the threshold for body content detection in our trim noise algorithm.</p>
</article>
<footer>
<p>Copyright 2024 Example News. All rights reserved.</p>
<p><a href=\"/privacy\">Privacy Policy</a> | <a href=\"/terms\">Terms</a></p>
</footer>
</body></html>'''

result = parser.parse(html)
assert result is not None
md = result['markdown']
assert 'Article Title' in md
assert 'Copyright' not in md, f'Copyright should be trimmed but found in: {md[-200:]}'
print('OK: Footer noise trimmed successfully')
print('Markdown preview (last 300 chars):', md[-300:])
"
`
Expected: "OK: Footer noise trimmed successfully", no "Copyright" in markdown

- [ ] **Step 4: Commit**

```bash
git add news/parser.py
git commit -m "feat: integrate _trim_noise into trafilatura extraction pipeline"
```
