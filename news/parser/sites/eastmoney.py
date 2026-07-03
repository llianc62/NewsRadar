"""EastmoneyParser — 东方财富 (eastmoney.com) HTML → Markdown 解析.

东方财富文章页使用传统服务端渲染，正文在 ``#ContentBody`` 容器内。
readability-lxml 对短新闻（带股票K线图）会返回空文档，
因此绕过 readability，直接对提取后的正文进行 markdown 转换。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone, timedelta
from typing import Any

from bs4 import BeautifulSoup

from news.parser.parser import HtmlParser

_CST = timezone(timedelta(hours=8))

# 正文中常见的噪音元素：评论区、声明、免责等
_STRIP_SELECTORS = [
    "p[style*='display:none']",
    "p[style*='display: none']",
    "p[style*='height:1px']",
    ".statement",
    ".editor",
    ".plwrap",
    ".review",
    ".declare",
    "#SOHUCS",
    "script",
    "style",
]


class EastmoneyParser(HtmlParser):
    """东方财富解析器 — 从 ``#ContentBody`` 提取正文，绕过 readability-lxml。

    readability-lxml 对东方财富的短新闻（含股票K线图）会返回空文档，
    因此本解析器跳过 readability，直接对 ``#ContentBody`` 的 HTML
    进行 markdown 转换。
    """

    def _extract(self, html: str, url: str = "") -> tuple[str, dict]:
        """从页面中提取正文 HTML 和元数据。

        正文容器: ``#ContentBody`` (或 ``.txtinfos``)
        标题容器: ``.contentwrap div.title``（避免匹配导航栏 span.title）
        元数据容器: ``.infos`` (含发布时间和来源)
        """
        soup = BeautifulSoup(html, "lxml")
        metadata: dict[str, Any] = {}

        # ── 标题 ──────────────────────────────────────────────────
        content_wrap = soup.select_one(".contentwrap")
        scope = content_wrap if content_wrap else soup
        title_el = scope.select_one("div.title") or scope.select_one(".title")
        if title_el:
            metadata["title"] = title_el.get_text(strip=True)

        # ── 时间 / 来源 ───────────────────────────────────────────
        infos_el = soup.select_one(".infos")
        if infos_el:
            infos_text = infos_el.get_text(" ", strip=True)
            metadata.update(self._parse_infos(infos_text))

        # ── 正文 ──────────────────────────────────────────────────
        content_el = soup.select_one("#ContentBody") or soup.select_one(
            ".txtinfos"
        )
        if content_el is None:
            return html, metadata

        # 移除噪音元素
        for tag in content_el.select(", ".join(_STRIP_SELECTORS)):
            tag.decompose()

        content_html = str(content_el)
        return content_html, metadata

    def _readability_extract(self, html: str, url: str = "") -> str | None:
        """跳过 readability-lxml，直接返回预处理后的 HTML。

        readability-lxml 对东方财富短新闻（带股票K线图）会返回空文档，
        此方法覆盖基类实现，直接返回输入 HTML 用于后续 markdown 转换。
        """
        text_content = re.sub(r"<[^>]+>", "", html).strip()
        if not text_content:
            return None
        return html

    @staticmethod
    def _parse_infos(infos_text: str) -> dict[str, str]:
        """从 ``.infos`` 的文本中提取发布时间和来源。

        典型格式: ``"2026年07月02日 21:52来源：东方财富Choice数据"``
        """
        result: dict[str, str] = {}

        dt_match = re.search(
            r"(\d{4})年(\d{2})月(\d{2})日\s*(\d{2}):(\d{2})",
            infos_text,
        )
        if dt_match:
            try:
                y, m, d, hh, mm = (int(g) for g in dt_match.groups())
                dt = datetime(y, m, d, hh, mm, tzinfo=_CST)
                result["published_at"] = dt.isoformat()
            except (ValueError, OSError):
                pass

        src_match = re.search(r"来源[：:]\s*(.+?)(?:$|\s{2,})", infos_text)
        if src_match:
            result["author"] = src_match.group(1).strip()

        return result
