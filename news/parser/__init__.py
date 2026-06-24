"""News parser framework — HtmlParser 基类 + ParserRegistry 路由."""

from news.parser.parser import HtmlParser, _split_keyword_tags
from news.parser.registry import parser_registry

# Set default parser directly — no circular imports since HtmlParser
# doesn't import from news.parser or news.parser.sites
parser_registry.set_default(HtmlParser())

__all__ = ["HtmlParser", "parser_registry", "_split_keyword_tags"]
