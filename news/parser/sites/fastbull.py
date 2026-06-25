"""FastbullParser — 法布财经 (fastbull.com) HTML → Markdown 解析."""

from __future__ import annotations

from typing import Any, Dict, Optional

from news.parser.parser import HtmlParser


class FastbullParser(HtmlParser):
    """法布财经解析器。

    fastbull.com 的 ``<meta name="keywords">`` 存放的是页面标题而非真实
    关键词，导致 trafilatura 提取出整句标题作为 tag。这里覆写 ``_extract``，
    在通用解析后清洗掉无效标签，让下游的 jieba 关键词提取接管。
    """

    def _extract(self, html: str, url: str) -> Optional[Dict[str, Any]]:
        result = self._extract_with_readability(html, url)
        if result is None:
            return None

        tags = result.get("tags", [])
        if not tags:
            return result

        title = result.get("title", "")

        # 过滤快讯的垃圾标签：与标题重复、或过长的句子
        cleaned = [t for t in tags if t != title and len(t) <= 20]
        result["tags"] = cleaned
        return result
