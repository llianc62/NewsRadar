"""News parser framework — HtmlParser 基类 + ParserRegistry 路由."""

from news.parser.parser import HtmlParser
from news.parser.registry import parser_registry

__all__ = ["HtmlParser", "parser_registry"]
