"""Site-specific parsers — one module per news source."""

from news.parser.parser import HtmlParser
from news.parser.registry import registry
from news.parser.sites.cankaoxiaoxi import CkxxappParser
from news.parser.sites.cls import ClsParser
from news.parser.sites.fastbull import FastbullParser
from news.parser.sites.ifeng import IfengParser
from news.parser.sites.ithome import IthomeParser
from news.parser.sites.juejin import JuejinParser
from news.parser.sites.kaopu import KaopuParser
from news.parser.sites.sspai import SspaiParser
from news.parser.sites.thepaper import ThepaperParser
from news.parser.sites.wallstreetcn import WallstreetcnParser
from news.parser.sites.zaobao import ZaobaoParser

# Default parser for unregistered source_ids
registry.set_default(HtmlParser())

# Site-specific parser registrations
# — one parser instance per domain; multiple source_ids share the same instance
_wsc = WallstreetcnParser()
_cls = ClsParser()

registry.register("wallstreetcn-hot", _wsc, domains=["wallstreetcn.com"])
registry.register("wallstreetcn-news", _wsc)
registry.register("cls-hot", _cls, domains=["cls.cn"])
registry.register("cls-depth", _cls)
registry.register("thepaper", ThepaperParser(), domains=["thepaper.cn"])
registry.register("ifeng", IfengParser(), domains=["ifeng.com"])
registry.register("cankaoxiaoxi", CkxxappParser(), domains=["ckxxapp.ckxx.net", "cankaoxiaoxi.com"])
registry.register("zaobao", ZaobaoParser(), domains=["zaochenbao.com"])
registry.register("kaopu", KaopuParser(), domains=["kaopu.news"])
registry.register("fastbull-news", FastbullParser(), domains=["fastbull.com"])
registry.register("ithome", IthomeParser(), domains=["ithome.com"])
registry.register("sspai", SspaiParser(), domains=["sspai.com"])
registry.register("juejin", JuejinParser(), domains=["juejin.cn"])
