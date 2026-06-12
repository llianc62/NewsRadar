# coding=utf-8
"""Data models for news items."""

from dataclasses import dataclass, field
from typing import Dict, List


@dataclass
class NewsItem:
    """Unified news item (hot-list + RSS)."""

    title: str
    source_id: str
    source_name: str = ""
    source_type: str = "hotlist"  # 'hotlist' or 'rss'
    tier: int = 4
    priority: int = 0
    url: str = ""
    mobile_url: str = ""
    rank: int = 0
    guid: str = ""
    published_at: str = ""
    summary: str = ""
    content: str = ""
    author: str = ""
    notified: int = 0
    first_crawl_time: str = ""
    last_crawl_time: str = ""
    crawl_count: int = 1
    category: str = ""
    tags: List[str] = field(default_factory=list)
    ranks: List[int] = field(default_factory=list)


@dataclass
class NewsData:
    """Collection of news items from a single crawl session."""

    date: str
    items: Dict[str, List[NewsItem]] = field(default_factory=dict)

