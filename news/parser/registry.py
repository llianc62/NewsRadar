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
