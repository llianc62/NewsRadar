"""SspaiParser — 少数派 (sspai.com) HTML → Markdown 解析."""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

from news.parser.parser import HtmlParser

# 少数派 meta keywords 存在 SSR 渲染 bug，JSON 对象被 HTML 编码后直接
# 塞进 content 属性，trafilatura 解析时会产出 "keyword": 等 JSON 碎片。
# 这些字符不应出现在合法 tag 中。
_INVALID_TAG_RE = re.compile(r'["{}]|":|:\s*\d|:\s*$')
_NUMERIC_TAG_RE = re.compile(r'^\d+(?:\.\d+)?$')


def _is_valid_tag(tag: str) -> bool:
    if _INVALID_TAG_RE.search(tag):
        return False
    if _NUMERIC_TAG_RE.match(tag):
        return False
    return True


class SspaiParser(HtmlParser):
    """少数派解析器 — 走通用 readability 管线，后处理过滤损坏的 tags。"""

    def _extract(self, html: str, url: str = "") -> Optional[Dict[str, Any]]:
        result = self._extract_with_readability(html, url)
        if result is None:
            return None
        tags = result.get("tags", [])
        if tags:
            result["tags"] = [t for t in tags if _is_valid_tag(t)]
        return result
