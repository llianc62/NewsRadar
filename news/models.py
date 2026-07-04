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
    summary: str = ""
    content: str = ""
    author: str = ""
    category: str = ""
    tags: List[str] = field(default_factory=list)
    ranks: List = field(default_factory=list)  # [[rank, total], ...]
    heat_score: int = 0         # 热度值 0-100
    sentiment_score: int = 0    # 情感值 0-100（50=中性）
    published_at: str = ""
    crawled_at: str = ""        # 云端抓取时间（来自 SQLite created_at）


@dataclass
class NewsData:
    """Collection of news items from a single crawl session."""

    date: str
    items: Dict[str, List[NewsItem]] = field(default_factory=dict)

