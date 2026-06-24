# coding=utf-8
"""HTML content parser — HTML → Markdown conversion (readability + fallback).

Reference: https://github.com/microsoft/markitdown
"""

import re
import json

import trafilatura  # kept for metadata extraction (extract_metadata)
from readability import Document
from markdownify import markdownify as _md
import html as _html

from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field

from lxml import html as lxml_html
from lxml.etree import ParseError

# ═══════════════════════════════════════════════════════════════════
# Block — extracted block-level content node
# ═══════════════════════════════════════════════════════════════════

BLOCK_TAGS = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "ul", "ol", "pre", "figure"}


@dataclass
class Block:
    """A block-level content node extracted from DOM for boundary detection.

    ``element`` holds the live lxml element reference so the pruning
    phase can locate this block in the parsed tree.
    """

    tag: str
    text: str
    text_len: int
    link_density: float
    html: str  # serialized HTML of this element
    element: Any = field(default=None, repr=False, compare=False)  # lxml element


# ═══════════════════════════════════════════════════════════════════
# Keyword tag normalisation
# ═══════════════════════════════════════════════════════════════════


def _split_keyword_tags(tags: List[str]) -> List[str]:
    """Split composite keyword strings into individual tags.

    trafilatura returns ``<meta name="keywords">`` content verbatim.
    This function detects the delimiter / structure convention of each
    tag string and applies the matching strategy:

    1. **Structured** — ``key: value`` pairs separated by commas
       (e.g. sspai).  Only ``keyword:`` values are kept as real tags;
       SEO metadata fields (``weight:``, ``level:``, ``intent:``, …)
       are discarded.

    2. **Comma-separated** — plain comma-delimited keywords
       (e.g. sputniknews).  Split by comma, trim each fragment.

    3. **Space-separated** — whitespace-delimited keywords
       (e.g. ifeng).  Split by whitespace, deduplicate.

    Already-split lists (each element already a single tag) pass
    through unchanged (deduplication only).
    """
    result: List[str] = []
    for t in tags:
        # ── Detect format ──────────────────────────────────────────
        # Normalise Chinese comma (U+FF0C) to ASCII comma for detection.
        normalised = t.replace("，", ",")

        # Structured "key: value" pairs: comma-separated fragments
        # where most pieces carry a colon.
        if "," in normalised:
            fragments = [f.strip() for f in normalised.split(",")]
            fragments = [f for f in fragments if f]
            colon_count = sum(1 for f in fragments if ":" in f)
            if colon_count >= len(fragments) * 0.5:
                for f in fragments:
                    if f.startswith("keyword:"):
                        kw = f.split(":", 1)[1].strip()
                        if kw and kw not in result:
                            result.append(kw)
                continue
            # Plain comma-separated — treat each fragment as a tag.
            for f in fragments:
                if f not in result:
                    result.append(f)
            continue

        # No commas — detect space-separated "key: value" pairs
        # (e.g. "keyword: 深度学习 weight: 0.95").  When even-index
        # words all end with ":" it's structured; otherwise plain
        # space-delimited keywords.
        words = normalised.split()
        if words:
            key_count = sum(1 for w in words[::2] if w.endswith(":"))
            if key_count == len(words[::2]) and any(
                w == "keyword:" for w in words[::2]
            ):
                # Structured — extract keyword: values only.
                for i in range(0, len(words) - 1, 2):
                    if words[i] == "keyword:":
                        if words[i + 1] not in result:
                            result.append(words[i + 1])
                continue

        for word in words:
            if word not in result:
                result.append(word)

    return result


# ═══════════════════════════════════════════════════════════════════
# HtmlParser
# ═══════════════════════════════════════════════════════════════════


class HtmlParser:
    """Convert HTML to Markdown via readability + markdownify with a regex fallback.

    Pure parser — no database dependency, no network I/O (image
    processing is delegated to :class:`ImageProcessor`).

    Usage::

        parser = HtmlParser(config)
        markdown = parser.parse(html_text, url="https://example.com")
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self._config = config or {}
        cfg = self._config
        crawler_cfg = cfg.get("crawler", {})
        self.max_content_length = crawler_cfg.get("max_content_length", 100000)

    # ── Public API ─────────────────────────────────────────────────

    def parse(self, html: str, url: str = "") -> Optional[Dict[str, Any]]:
        """Extract Markdown + metadata from HTML.

        Priority: SPA embedded data (clean structured JSON) →
        trafilatura (full-page extraction) → HTML-stripping fallback.

        Does **not** download or process images — callers should use
        :class:`ImageProcessor` separately when image handling is needed.

        Args:
            html: Raw HTML text.
            url: Source URL (passed to trafilatura for metadata).

        Returns:
            Dict with keys ``markdown``, ``title``, ``author``,
            ``published_at``, ``summary``, ``category``, ``tags``,
            or None if extraction produced nothing useful.
        """
        result = None

        if not html or not html.strip():
            return None

        # Custom website handle
        html = self._handle_ifeng(html, url)

        # 1. SPA embedded data first — __NEXT_DATA__ / __SSR__ / JSON-LD
        #    These carry clean article HTML without nav/footer/sidebar noise.
        result = self._extract_spa_data(html, url)

        # 2. readability — full-page extraction with noise trimming
        if result is None:
            result = self._extract_with_readability(html, url)

        # 3. HTML-stripping fallback
        if result is None:
            result = self._fallback(html, url)

        if result is not None:
            md = result.get("markdown", "")
            if md and len(md) > self.max_content_length:
                result["markdown"] = md[:self.max_content_length] + "\n\n... (truncated)"

        return result

    # ── Image-heavy content builder ──────────────────────────────────

    @staticmethod
    def _build_image_markdown(html: str) -> str:
        """Build markdown from image-heavy HTML when trafilatura fails.

        Extracts ``<img src>`` URLs and remaining text, producing
        markdown that preserves images even when there's very little
        body text (e.g. infographic / 一图看懂 articles).
        """
        # Extract image URLs from <img> tags
        imgs = re.findall(
            r'<img[^>]+src=["\']([^"\']+)["\'][^>]*>',
            html, re.IGNORECASE,
        )
        # Strip tags for remaining text
        text = re.sub(r'<[^>]+>', '', html)
        text = _html.unescape(text)
        text = re.sub(r'\s+', ' ', text).strip()

        parts = [f'![]({url})' for url in imgs]
        if text:
            parts.append(text)
        return '\n\n'.join(parts)

    # ── Lazy-image fix ─────────────────────────────────────────────

    @staticmethod
    def _fix_lazy_images(html: str) -> str:
        """Convert lazy-loaded ``data-src`` / ``data-original`` to ``src``.

        Many sites (thepaper.cn, WeChat, ithome, etc.) set ``src`` to a
        placeholder (1×1 pixel, data URI, or a generic placeholder PNG)
        and put the real URL in ``data-src`` or ``data-original``.
        trafilatura sees only ``src`` and captures the placeholder.
        This rewrites ``<img>`` tags so the real image is visible to
        downstream extraction.

        Two passes per attribute to handle both orderings
        (``src`` before or after the data attribute).
        """
        for data_attr in ("data-src", "data-original"):
            # data-attr appears before src
            html = re.sub(
                rf'<img([^>]*)\s+{data_attr}="([^"]+)"([^>]*)\s+src="[^"]*"',
                rf'<img\1 src="\2"\3',
                html,
            )
            # data-attr appears after src
            html = re.sub(
                rf'<img([^>]*)\s+src="[^"]*"([^>]*)\s+{data_attr}="([^"]+)"',
                rf'<img\1 src="\3"\2',
                html,
            )
        return html

    # ── Site-specific preprocessing ────────────────────────────────

    @staticmethod
    def _handle_ifeng(html: str, url: str) -> str:
        """Remove ifeng-specific template noise from HTML before extraction.

        Removes:
        - Browser-upgrade prompt (``#lowBrowerBoxFixed``)
        - Meta info bar between title and body (avatar, source name,
          "独家抢先看" label, date, share buttons)
        - Divider between meta bar and article body
        """
        if "ifeng.com" not in url:
            return html

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
            html = lxml_html.tostring(tree, encoding="unicode")
        return html

    # ── readability path ───────────────────────────────────────────

    def _extract_with_readability(
        self, html: str, url: str, skip_trim: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Use readability-lxml + markdownify for content extraction.

        HTML is first preprocessed by :meth:`_trim_noise` to remove
        head/tail UI noise (nav, footer, share buttons, etc.) before
        extraction.

        Set *skip_trim* to True when *html* is already clean article
        body content (e.g. from SPA JSON) that doesn't need head/tail
        noise trimming.
        """

        # ── Preprocess: fix lazy images, then trim head/tail noise ────
        html = self._fix_lazy_images(html)
        if skip_trim:
            source_html = html
        else:
            clean_html = self._trim_noise(html)
            source_html = clean_html if clean_html is not None else html

        # ── readability-lxml: extract article content HTML ────────────
        try:
            doc = Document(source_html, url=url)
            content_html = doc.summary()
        except Exception:
            return None

        if not content_html or not content_html.strip():
            return None

        # ── markdownify: HTML → Markdown ──────────────────────────────
        markdown = _md(
            content_html,
            heading_style="ATX",
            strip=["script", "style"],
            escape_asterisks=False,
            escape_underscores=False,
        )

        # 如果正文小于50个字符，就默认是无效文档。
        if not markdown or len(markdown.strip()) <= 50:
            return None

        # 标题来源：正文 H1（干净无后缀） > HTML <title>/og:title
        title = self._extract_markdown_heading(markdown)

        if not title:
            title = self._extract_title_from_html(html)

        # 优化 markdown 文本
        markdown = self._beautify_markdown_formatting(markdown)

        # 当 _trim_noise 退化为全页 HTML 时，readability 输出
        # 会包含页头噪声（URL、来源名、日期、标签等），正文
        # 以 H1 标题开头。裁掉 H1 之前的所有行。
        #
        # 注意：不能简单地用正则 ^#\s+ 匹配 H1——代码块内的
        # shell 注释也以 # 开头（例如 sspai.com 的终端命令代码块）。
        # 必须跟踪 ``` 围栏状态，跳过代码块内部的 # 行。
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

        # Normalise: trafilatura returns <meta name="keywords"> as-is,
        # which for Chinese sites is often a single space-separated
        # string (e.g. ifeng) or a comma-separated string with
        # duplicates (e.g. sputniknews).  Split into individual tags.
        tags = _split_keyword_tags(tags)

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

    # ── Fallback: HTML strip ───────────────────────────────────────

    def _fallback(self, html_text: str, url: str = "") -> Optional[Dict[str, Any]]:
        """Strip HTML tags, collapse whitespace — used when trafilatura
        is unavailable or fails to extract meaningful content.

        Also extracts title from ``<title>`` tag and metadata from
        ``<meta>`` tags (author, description, published_time).
        """
        title = self._extract_title_from_html(html_text)

        # Extract metadata from <meta> tags
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
    def _handle_markdown_bold(markdown: str) -> str:
        """Normalize ``**`` bold markers: strip internal spaces, add
        external spaces where markers abut text.

        Splits on ``**`` so bold spans are tracked explicitly — plain
        regex cannot distinguish opening-``**`` from closing-``**``,
        causing adjacent bold spans (``**a**文字**b**``) to be mangled.
        """
        parts = markdown.split("**")
        if len(parts) < 2:
            return markdown

        # Odd indices are bold content — strip stray leading/trailing spaces.
        for i in range(1, len(parts), 2):
            parts[i] = parts[i].strip()

        # Rebuild, inserting a space between ** and abutting text.
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
        remove praise-button noise (``- +1``) from thepaper.cn widgets.
        """
        markdown = re.sub(r"^- \+1\n+(?=# )", "", markdown, count=1)
        return HtmlParser._handle_markdown_bold(markdown)

    @staticmethod
    def _extract_markdown_heading(markdown: str) -> str:
        """Extract article title from the first H1 heading in markdown.

        trafilatura converts body ``<h1>`` to ``# heading`` — this is the
        clean article title without site-name suffixes that pollute
        ``<title>`` and ``og:title``.

        Skips ``#`` lines inside fenced code blocks — shell comments
        (e.g. ``# display ...``) are not article headings.
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
        """Build a unified result dict from extracted content and metadata.

        All callers are responsible for extracting their own metadata
        and passing it as explicit parameters — there is no implicit
        override logic inside this method.
        """
        if tags:
            tags = [t.lstrip("#") for t in tags if t]
            tags = [t for t in tags if t]  # remove empty strings after stripping
        return {
            "markdown": markdown,
            "title": title,
            "author": author,
            "published_at": published_at,
            "summary": summary,
            "category": category,
            "tags": tags or [],
        }

    # ── Block extraction & noise trimming ──────────────────────────

    @staticmethod
    def _extract_blocks(tree) -> List["Block"]:
        """Extract block-level content nodes from lxml tree in document order.

        Only outermost block nodes are included — nested blocks (e.g.
        ``<blockquote><p>...</p></blockquote>``) yield only the parent,
        avoiding duplicate content.

        Each block's ``html`` is the full serialized element including its
        tag, so the reassembled fragment is valid HTML for trafilatura.
        """
        # scope to <body> to avoid stray elements in <head>
        body = tree.find(".//body")
        root = body if body is not None else tree

        blocks: List[Block] = []
        for el in root.iter():
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

            # Keep blocks that contain images even if they have no text
            # (e.g. <figure><img src="..."></figure> or <p><img src="..."></p>)
            has_image = el.find(".//img") is not None
            if text_len == 0 and not has_image:
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
                element=el,
            ))
        return blocks

    @staticmethod
    def _trim_noise(html: str) -> Optional[str]:
        """Trim head/tail noise from HTML before feeding to trafilatura.

        Uses block-level content nodes to detect the article body
        boundaries, then prunes the original DOM tree — removing elements
        before the start boundary and after the end boundary.  This
        preserves ALL original content between the boundaries (images,
        tables, formatting, etc.), not just the block-level elements.

        Returns None when boundaries cannot be reliably detected — callers
        should fall back to the original HTML.
        """
        try:
            tree = lxml_html.fromstring(html)
        except ParseError:
            return None

        blocks = HtmlParser._extract_blocks(tree)

        # no blocks — nothing to work with
        if not blocks:
            return None

        # When the page relies on <div> for body text (e.g. Sputnik
        # uses div.article__block), blocks see very little of the
        # total visible text — boundary detection would be unreliable.
        # Fall back to trafilatura on the full HTML.
        body = tree.find(".//body")
        root = body if body is not None else tree
        page_text = " ".join(root.itertext()).strip()
        if page_text:
            blocks_total = sum(b.text_len for b in blocks)
            if blocks_total / len(page_text) < 0.03:
                return None

        # ── Find start (trim head) ──────────────────────────────────
        # Priority: h1 (article title) > long paragraph > h2/h3
        # h1 is the most reliable content-start signal — lower-level
        # headings (h2/h3) are often UI widgets (e.g. "大家都在搜")
        start = 0
        start_found = False

        # Pass 1: h1 — the article's main title
        for i, b in enumerate(blocks):
            if b.tag == "h1" and b.text_len >= 4:
                start = i
                start_found = True
                break

        # Pass 2: first substantial paragraph
        if not start_found:
            for i, b in enumerate(blocks):
                if b.text_len >= 80 and b.link_density < 0.3:
                    start = i
                    start_found = True
                    break

        # Pass 3: fall back to h2/h3 (only when no h1 or long paragraph found)
        if not start_found:
            for i, b in enumerate(blocks):
                if b.tag in ("h2", "h3") and b.text_len >= 4:
                    start = i
                    start_found = True
                    break

        # ── Find end (trim tail) ────────────────────────────────────
        end = len(blocks) - 1
        end_found = False
        for i in range(len(blocks) - 1, -1, -1):
            b = blocks[i]
            # Substantial content paragraph — reliable end signal.
            if b.text_len >= 50 and b.link_density < 0.3:
                end = i
                end_found = True
                break
            # h4/h5/h6 are almost always footer headings
            # ("扫码下载", "关于我们"), not article content — skip them.
            if b.tag in ("h4", "h5", "h6"):
                continue
            # Article section heading — likely still content.
            # Skip when this is the same block as the start boundary
            # (e.g. only h1 on the page).  h1 is the article title and
            # almost never the end of content; treating it as both start
            # and end would prune the entire article body.
            if b.tag in ("h1", "h2", "h3") and b.text_len >= 4 and i != start:
                end = i
                end_found = True
                break
            # Short body paragraph (but not a copyright line like
            # "© 2024 ..." which is typically < 30 chars).
            if b.tag == "p" and b.text_len >= 30 and b.link_density < 0.3:
                end = i
                end_found = True
                break

        # No boundaries detected — degrade
        if not start_found and not end_found:
            return None

        # Overlap — degrade
        if start > end:
            return None

        # ── Prune the original DOM tree ─────────────────────────────
        # Locate the boundary elements in the lxml tree and remove
        # everything outside the [start_el, end_el] range.
        start_el = blocks[start].element
        end_el = blocks[end].element

        body = tree.find(".//body")
        container = body if body is not None else tree

        HtmlParser._remove_before(container, start_el)
        HtmlParser._remove_after(container, end_el)

        # ── Clean metadata wrappers between heading and body ─────────
        # div/span wrappers for author, date, share buttons sitting
        # between h1 and the first body paragraph survive the range
        # prune above — remove them while keeping visual content
        # (figures, images, videos).
        body_anchor = start
        for i in range(start + 1, len(blocks)):
            if blocks[i].tag not in ("h1", "h2", "h3", "h4", "h5", "h6"):
                body_anchor = i
                break
        if body_anchor != start:
            HtmlParser._remove_meta_between(start_el, blocks[body_anchor].element)

        # readability needs an <article> wrapper to recognise headings
        # (bare <h1> inside <body> is treated as plain text).  Wrap the
        # remaining children so the extractor can see the structure.
        body_html = (container.text or "") + "".join(
            lxml_html.tostring(child, encoding="unicode")
            for child in container
        )
        return f"<html><body><article>{body_html}</article></body></html>"

    # ── Tree-pruning helpers ────────────────────────────────────────

    @staticmethod
    def _contains_or_is(ancestor, descendant):
        """Return True if *ancestor* is *descendant* or contains it."""
        if ancestor is descendant:
            return True
        parent = descendant.getparent()
        while parent is not None:
            if parent is ancestor:
                return True
            parent = parent.getparent()
        return False

    @staticmethod
    def _remove_before(parent, target):
        """Remove children of *parent* that come before the child
        containing *target* in document order.  Recurse into nested
        containers to prune predecessors at every depth."""
        for child in list(parent):
            if HtmlParser._contains_or_is(child, target):
                # *target* lives inside this child — recurse if nested
                if child is not target:
                    HtmlParser._remove_before(child, target)
                return  # everything after this child is >= target
            parent.remove(child)

    @staticmethod
    def _remove_after(parent, target):
        """Remove children of *parent* that come after the child
        containing *target* in document order.  Iterates in reverse so
        removals don't shift earlier positions."""
        for child in reversed(list(parent)):
            if HtmlParser._contains_or_is(child, target):
                # *target* lives inside this child — recurse if nested
                if child is not target:
                    HtmlParser._remove_after(child, target)
                return  # everything before this child is <= target
            parent.remove(child)

    @staticmethod
    def _remove_meta_between(before_el, after_el):
        """Remove non-content wrapper elements between *before_el* and
        *after_el* in the DOM tree.

        Handles two cases:

        1. Both elements share the same parent — cleans siblings
           directly between them.
        2. *after_el* is nested inside a later sibling of *before_el*
           (e.g. content div wrapping body paragraphs) — walks siblings
           after *before_el*, removing noise wrappers until the
           container that holds *after_el* is reached.
        """
        parent = before_el.getparent()
        if parent is None:
            return

        # Case 1: same parent — clean siblings between them
        if parent is after_el.getparent():
            between = False
            for child in list(parent):
                if child is before_el:
                    between = True
                    continue
                if child is after_el:
                    break
                if between and HtmlParser._is_noise_wrapper(child):
                    parent.remove(child)
            return

        # Case 2: different parents — walk siblings after before_el,
        # removing noise wrappers until we hit the container that
        # holds after_el
        after = False
        for child in list(parent):
            if child is before_el:
                after = True
                continue
            if not after:
                continue
            if HtmlParser._contains_or_is(child, after_el):
                break  # content container — stop
            if HtmlParser._is_noise_wrapper(child):
                parent.remove(child)

    @staticmethod
    def _is_noise_wrapper(el):
        """Return True if *el* is a metadata/noise wrapper, not content."""
        tag = el.tag if isinstance(el.tag, str) else ""
        if tag in BLOCK_TAGS:
            return False
        if el.find(".//img") is not None:
            return False
        if el.find(".//video") is not None:
            return False
        if el.find(".//iframe") is not None:
            return False
        # Contains nested block-level content → content container
        for t in BLOCK_TAGS:
            if el.find(f".//{t}") is not None:
                return False
        return True

    # ── SPA data extraction ────────────────────────────────────────

    @staticmethod
    def _extract_js_content_vars(html_text: str) -> Optional[str]:
        """Extract article content HTML from JS string variables in inline scripts.

        Some CMS platforms (xinhuamm.net / ckxxapp — 新华社/参考消息 client
        app) embed the full article body as an HTML string assigned to a
        JavaScript variable.  The content is never written into the DOM,
        so readability and fallback extraction both miss it.

        Returns clean HTML string or None.
        """
        # Target known variable names used by these CMS platforms.
        for var_name in ("contentTxt",):
            pattern = re.compile(
                rf'var\s+{var_name}\s*=\s*"((?:[^"\\]|\\.)*)"',
                re.DOTALL,
            )
            match = pattern.search(html_text)
            if not match:
                continue

            content = match.group(1)
            # Unescape JS string escapes
            content = content.replace(r'\"', '"')
            content = content.replace(r'\/', '/')
            # Unescape HTML entities (e.g. &amp;, &quot;)
            content = _html.unescape(content)

            if content and len(content) > 50:
                return content

        return None

    def _extract_spa_data(self, html_text: str, url: str = "") -> Optional[Dict[str, Any]]:
        """Extract content from SPA embedded JSON when DOM-based
        extraction fails (e.g. wallstreetcn.com, Next.js sites).

        Tries known SPA data patterns in order.  When JSON is found, the
        data tree is searched recursively for an object with both
        ``title`` and ``content`` keys — the content is then converted to
        Markdown via trafilatura (or HTML-stripped as fallback).

        Also tries extracting article HTML from JS string variables
        (xinhuamm.net CMS pattern: ``var contentTxt = "<p>...</p>"``).

        Returns result dict, or None if no SPA data was found.
        """
        candidates = self._find_json_candidates(html_text)
        for data in candidates:
            article = self._find_article_in_json(data)
            if article is None:
                continue
            content = article.get("content", "")
            if not content or not isinstance(content, str) or len(content) < 50:
                continue
            # 剥离 <blockquote> 标签以免 trafilatura 丢弃其中的 <img>
            # （华尔街见闻等站点用 <blockquote> 包裹后半段数据罗列内容）
            content = re.sub(r'</?blockquote[^>]*>', '', content)
            # SPA JSON 中的 HTML 片段缺少 <html>/<body> 上下文，trafilatura
            # 在此类片段中只能识别包裹在 <p> 或 <article> 内的 <img>，裸
            # <img> 或包裹在 <figure>/<div>/<blockquote>/<section> 中的
            # 均会被丢弃（澎湃新闻等 Next.js 站点的 content 中 <img> 直接
            # 夹在 <p> 之间）。统一包裹一层 <p> 解决此问题；对已在 <p>
            # 内的 <img> 无副作用（双层 <p> 输出一致）。
            content = re.sub(r'(<img[^>]*>)', r'<p>\1</p>', content)
            # content is HTML — convert to Markdown
            markdown = None
            extracted = self._extract_with_readability(content, url, skip_trim=True)
            if extracted is not None:
                markdown = extracted["markdown"]
            if markdown is None:
                fallback_result = self._fallback(content, url)
                if fallback_result is not None:
                    markdown = fallback_result["markdown"]

            # Image-heavy content: trafilatura + fallback both fail
            # because there's too little text.  Build markdown from
            # <img> tags + any remaining text so the images are preserved.
            if not markdown or len(markdown.strip()) <= 50:
                markdown = self._build_image_markdown(content)

            if markdown and len(markdown.strip()) > 50:
                # 从 SPA JSON / JSON-LD 中提取元数据
                title = article.get("title") or article.get("headline", "")
                author_val = ""
                pub_at = article.get("datePublished") or article.get("date", "")
                summary_val = article.get("description") or article.get("abstract", "")
                # JSON 里没有 description 时，回退到原始完整 HTML 的 <meta> 标签
                if not summary_val:
                    summary_val = (
                        self._extract_meta(html_text, r'name=["\']description["\']')
                        or self._extract_meta(html_text, r'property=["\']og:description["\']')
                    )
                category_val = ""
                tags_val: List[str] = []
                # JSON-LD keywords
                keywords = article.get("keywords")
                if isinstance(keywords, str):
                    tags_val = [k.strip() for k in keywords.split(",") if k.strip()]
                elif isinstance(keywords, list):
                    tags_val = [str(k) for k in keywords if k]
                # JSON-LD articleSection
                section = article.get("articleSection")
                if isinstance(section, str) and section:
                    category_val = section

                return self._build_result(
                    markdown=markdown.strip(),
                    title=title,
                    author=author_val,
                    published_at=pub_at,
                    summary=summary_val,
                    category=category_val,
                    tags=tags_val,
                )

        # 6. JS content variable (xinhuamm.net CMS: var contentTxt = "<p>...</p>")
        content_html = self._extract_js_content_vars(html_text)
        if content_html:
            extracted = self._extract_with_readability(
                content_html, url, skip_trim=True,
            )
            if extracted is not None:
                # Title is typically NOT in the JS body HTML — fill from
                # the parent page's <title> / og:title / meta tags.
                if not extracted["title"]:
                    extracted["title"] = self._extract_title_from_html(html_text)
                if not extracted["summary"]:
                    extracted["summary"] = self._extract_meta(
                        html_text, r'name=["\']description["\']'
                    ) or self._extract_meta(
                        html_text, r'property=["\']og:description["\']'
                    )
                if not extracted["published_at"]:
                    extracted["published_at"] = self._extract_meta(
                        html_text, r'property=["\']article:published_time["\']'
                    )
                if not extracted["author"]:
                    extracted["author"] = self._extract_meta(
                        html_text, r'name=["\']author["\']'
                    )
                return extracted

        return None

    def _find_json_candidates(self, html_text: str):
        """Yield parsed JSON objects from known SPA embedding patterns.

        Patterns tried:
        1. ``__SSR__ = {...}`` (Vite SSR, e.g. wallstreetcn.com)
        2. ``__NEXT_DATA__ = {...}`` (Next.js JS assignment)
        3. ``<script id="__NEXT_DATA__" ...>`` (Next.js script tag, e.g. thepaper.cn)
        4. ``__NUXT__ = {...}`` (Nuxt)
        5. ``<script type="application/ld+json">`` (JSON-LD / Schema.org)
        """
        # 1. Vite SSR: __SSR__ = {...}
        for data in self._extract_bracketed_json(html_text, r'__SSR__\s*=\s*(\{)'):
            if data:
                yield data

        # 2. Next.js (JS assignment): __NEXT_DATA__ = {...}
        for data in self._extract_bracketed_json(html_text, r'__NEXT_DATA__\s*=\s*(\{)'):
            if data:
                yield data

        # 3. Next.js (script tag): <script id="__NEXT_DATA__" ...>{...}</script>
        for data in self._extract_bracketed_json(
            html_text, r'<script[^>]*\bid=["\']__NEXT_DATA__["\'][^>]*>\s*(\{)'
        ):
            if data:
                yield data

        # 4. Nuxt: __NUXT__ = {...}
        for data in self._extract_bracketed_json(html_text, r'__NUXT__\s*=\s*(\{)'):
            if data:
                yield data

        # 5. JSON-LD
        for data in self._extract_json_ld(html_text):
            if data:
                yield data

    @staticmethod
    def _extract_bracketed_json(html_text: str, pattern: str):
        """Find ``pattern`` in *html_text*, then bracket-match to get the
        full JSON string, parse, and return as a list (may be empty).

        The *pattern* must capture the position of the opening ``{``.
        """
        match = re.search(pattern, html_text)
        if not match:
            return []

        start = match.start(1)
        depth = 0
        end = start
        for i in range(start, len(html_text)):
            ch = html_text[i]
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
            return [json.loads(html_text[start:end])]
        except (json.JSONDecodeError, ValueError):
            return []

    @staticmethod
    def _extract_json_ld(html_text: str):
        """Extract articles from ``<script type="application/ld+json">`` tags.

        Returns a list of parsed JSON-LD objects that *might* be articles.
        """
        pattern = re.compile(
            r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            re.DOTALL | re.IGNORECASE,
        )
        results = []
        for match in pattern.finditer(html_text):
            try:
                data = json.loads(match.group(1).strip())
                results.append(data)
            except (json.JSONDecodeError, ValueError):
                continue
        return results
    @staticmethod
    def _find_article_in_json(data):
        """Recursively search *data* for an object that has both a
        ``title`` (str) and ``content`` (str) key.

        If multiple candidates exist, the one with the longest ``content``
        is returned.  For JSON-LD objects, also accepts
        ``@type: Article/NewsArticle`` with ``headline``+``articleBody``.

        Returns a dict with keys ``title``, ``content``, ``author``,
        ``datePublished``, ``description``, ``keywords``, ``articleSection``,
        or None.
        """

        def _get_author(obj: dict) -> str:
            """Extract author name from a JSON-LD object."""
            author = obj.get("author", "")
            if isinstance(author, str):
                return author
            if isinstance(author, dict):
                return author.get("name", "")
            if isinstance(author, list):
                names = []
                for a in author:
                    if isinstance(a, str):
                        names.append(a)
                    elif isinstance(a, dict):
                        n = a.get("name", "")
                        if n:
                            names.append(n)
                return ", ".join(names)
            return ""

        best = None
        best_len = 0

        def _search(obj):
            nonlocal best, best_len
            if isinstance(obj, dict):
                # JSON-LD style: @type Article + headline + articleBody
                obj_type = obj.get("@type", "")
                if obj_type in ("Article", "NewsArticle"):
                    headline = obj.get("headline", "")
                    body = obj.get("articleBody", "")
                    if isinstance(headline, str) and isinstance(body, str):
                        content_len = len(body)
                        if content_len > best_len:
                            best_len = content_len
                            best = {
                                "title": headline,
                                "content": body,
                                "author": _get_author(obj),
                                "datePublished": obj.get("datePublished", ""),
                                "description": obj.get("description", ""),
                                "keywords": obj.get("keywords", []),
                                "articleSection": obj.get("articleSection", ""),
                            }

                # Generic SPA embedded article: title/name + content
                title = obj.get("title") or obj.get("name", "")
                content = obj.get("content")
                if (isinstance(title, str) and isinstance(content, str)
                        and len(title) > 0):
                    content_len = len(content)
                    if content_len > best_len:
                        best_len = content_len
                        best = {
                            "title": title,
                            "content": content,
                            "author": obj.get("author", ""),
                            "datePublished": obj.get("datePublished")
                                or obj.get("date", ""),
                            "description": obj.get("description", ""),
                            "keywords": obj.get("keywords", []),
                            "articleSection": obj.get("articleSection", ""),
                        }

                # Recurse into nested objects
                for v in obj.values():
                    _search(v)

            elif isinstance(obj, list):
                for item in obj:
                    _search(item)

        _search(data)
        return best


