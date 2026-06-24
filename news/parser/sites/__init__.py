"""Site-specific parsers — one module per news source."""

from news.parser.parser import HtmlParser
from news.parser.registry import parser_registry
from news.parser.sites.thepaper import ThepaperParser

# Default parser for unregistered source_ids
parser_registry.set_default(HtmlParser())

# Site-specific parser registrations
parser_registry.register("thepaper", ThepaperParser())
