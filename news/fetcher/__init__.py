# coding=utf-8
"""News fetcher package.

Provides a pluggable fetcher hierarchy for different news data sources::

    Fetcher (abstract base)
    ├── NewsnowFetcher — NewsNow hot-list API
    └── RssFetcher     — RSS/Atom/JSON Feed
"""

from news.fetcher.fetcher import Fetcher
from news.fetcher.newsnow import NewsnowFetcher
from news.fetcher.rss import RssFetcher

__all__ = ["Fetcher", "NewsnowFetcher", "RssFetcher"]
