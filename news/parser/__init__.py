"""News parser framework — HtmlParser 基类 + ParserRegistry 路由."""

from news.parser.parser import HtmlParser, split_keyword_tags
from news.parser.registry import registry

# Import sites to trigger parser registration (no circular imports)
import news.parser.sites as _sites  # noqa: F401

__all__ = ["HtmlParser", "registry", "split_keyword_tags"]
