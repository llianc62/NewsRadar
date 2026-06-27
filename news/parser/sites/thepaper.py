"""ThepaperParser — 澎湃新闻 (thepaper.cn) HTML → Markdown 解析."""

from __future__ import annotations

import json
import re
import html as _html

from news.parser.parser import HtmlParser


class ThepaperParser(HtmlParser):
    """澎湃新闻解析器 — 从 __NEXT_DATA__ JSON 提取正文 HTML。

    澎湃新闻的文章页使用 Next.js SSR，正文 HTML 嵌入在
    ``<script id="__NEXT_DATA__" type="application/json">`` 中。
    """

    def _extract(self, html: str, url: str = "") -> tuple[str, dict]:
        """从 __NEXT_DATA__ JSON 提取正文 HTML 和元数据。"""
        for candidate in self._find_next_data_candidates(html):
            article = self._find_article_in_json(candidate)
            if not article:
                continue
            if not self._is_valid_article(article):
                continue

            content_html = article["content"]

            title = article.get("title", "")
            summary = article.get("description", "")
            published_at = article.get("datePublished", "")
            tags = article.get("keywords", [])
            if isinstance(tags, str):
                tags = [t.strip() for t in tags.split(",") if t.strip()]

            return content_html, {
                "title": title,
                "published_at": published_at,
                "summary": summary,
                "tags": tags,
            }

        return html, {}

    def _preprocess(self, html: str, url: str) -> str:
        """修复懒加载图片，包裹裸 img。"""
        html = self._fix_lazy_images(html)
        html = re.sub(r'(?<!>)\s*<img\s', '<p><img ', html, count=1)
        html = re.sub(r'<img([^>]*?)>\s*(?!<)', r'<img\1></p>', html)
        return html

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
        """Recursively find article object in JSON tree.

        Matches dicts with ``title``/``name`` + ``content``/``articleBody``
        keys, or JSON-LD ``@type: Article/NewsArticle``.
        Returns a normalised dict with a ``title`` key.
        """
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
                                "author": obj.get("author", ""),
                                "datePublished": obj.get("datePublished", ""),
                                "description": obj.get("description", ""),
                                "keywords": obj.get("keywords", []),
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
                                or obj.get("date", "")
                                or obj.get("pubTime", "")
                                or obj.get("publishTime", ""),
                            "description": obj.get("description", ""),
                            "keywords": obj.get("keywords", []),
                        }

                # Recurse into nested objects
                for v in obj.values():
                    _search(v)

            elif isinstance(obj, list):
                for item in obj:
                    _search(item)

        _search(data)
        return best

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
