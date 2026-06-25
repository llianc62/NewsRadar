"""Parser registry — source_id → Parser 路由，支持域名匹配."""

from __future__ import annotations

from typing import Optional, Dict, Any
from urllib.parse import urlparse


class Registry:
    """source_id → Parser 实例的路由表。

    路由优先级：
    1. source_id 精确匹配
    2. URL hostname 域名匹配（如 ``wallstreetcn.com``）
    3. 默认 Parser 兜底
    """

    def __init__(self):
        self._parsers: dict[str, object] = {}
        self._domains: dict[str, object] = {}  # hostname → parser
        self._default: object | None = None  # 从 __init__.py 注入

    def set_default(self, parser: object) -> None:
        """注入默认 Parser（HtmlParser 实例），避免循环 import。

        在 __init__.py 中调用：registry.set_default(HtmlParser())
        """
        self._default = parser

    def register(
        self, source_id: str, parser: object,
        domains: list[str] | None = None,
    ) -> None:
        """注册 source_id → Parser 映射。同名 source_id 后来者覆盖前者。

        Args:
            source_id: 新闻源标识（如 ``"thepaper"``、``"ifeng"``）
            parser: Parser 实例
            domains: 可选域名列表（如 ``["wallstreetcn.com"]``），
                     用于 URL 路由匹配
        """
        self._parsers[source_id] = parser
        if domains:
            for domain in domains:
                self._domains[domain] = parser

    def _resolve(self, source_id: str, url: str = "") -> object | None:
        """三阶段路由：source_id → domain → default."""
        # 1. Exact source_id match
        parser = self._parsers.get(source_id)
        if parser is not None:
            return parser

        # 2. Domain match from URL hostname
        if url:
            hostname = urlparse(url).hostname or ""
            parser = self._domains.get(hostname)
            if parser is None:
                # Try parent domain (e.g. "www.wallstreetcn.com" → "wallstreetcn.com")
                for domain, p in self._domains.items():
                    if hostname.endswith("." + domain):
                        parser = p
                        break
            if parser is not None:
                return parser

        # 3. Default fallback
        return self._default

    def parse(
        self, source_id: str, html: str, url: str = ""
    ) -> Optional[Dict[str, Any]]:
        """根据 source_id 或 URL hostname 路由到对应 Parser 解析。

        Args:
            source_id: 新闻源标识（如 ``"thepaper"``、``"ifeng"``），
                       来自 item["source_id"]。若为 ``"manual"`` 等未注册
                       值，则通过 URL hostname 匹配。
            html: 原始 HTML 文本
            url: 来源 URL（用于域名匹配 + 传给 readability 元数据提取）

        Returns:
            Dict 含 markdown/title/author/published_at/summary/category/tags，
            或 None 如果所有提取路径均失败。
        """
        parser = self._resolve(source_id, url)
        if parser is None:
            return None
        return parser.parse(html, url)


# 全局单例 — 模块加载时创建，由 __init__.py 和 sites/__init__.py 填充
registry = Registry()
