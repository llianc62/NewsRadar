"""News parser framework — HtmlParser 基类 + ParserRegistry 路由."""

from news.parser.parser import HtmlParser, _split_keyword_tags
from news.parser.registry import parser_registry
import news.parser.sites  # noqa: F401 — register site-specific parsers + default

__all__ = ["HtmlParser", "parser_registry"]
