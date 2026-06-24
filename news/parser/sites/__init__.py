"""Site-specific parsers — one module per news source."""

from news.parser.parser import HtmlParser
from news.parser.registry import parser_registry
from news.parser.sites.cankaoxiaoxi import CkxxappParser
from news.parser.sites.cls import ClsParser
from news.parser.sites.ifeng import IfengParser
from news.parser.sites.thepaper import ThepaperParser
from news.parser.sites.wallstreetcn import WallstreetcnParser
from news.parser.sites.zaobao import ZaobaoParser

# Default parser for unregistered source_ids
parser_registry.set_default(HtmlParser())

# Site-specific parser registrations
parser_registry.register("thepaper", ThepaperParser())
parser_registry.register("ifeng", IfengParser())
parser_registry.register("cankaoxiaoxi", CkxxappParser())
parser_registry.register("cls-hot", ClsParser())
parser_registry.register("cls-depth", ClsParser())
parser_registry.register("wallstreetcn-hot", WallstreetcnParser())
parser_registry.register("wallstreetcn-news", WallstreetcnParser())
parser_registry.register("zaobao", ZaobaoParser())
