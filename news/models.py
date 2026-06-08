# coding=utf-8
"""Data models for news items."""

from dataclasses import dataclass, field
from typing import Any, Dict, List


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
    ranks: List[int] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """转为纯 dict，供 Grabber 等组件使用。"""
        d = {
            "title": self.title,
            "source_id": self.source_id,
            "source_name": self.source_name,
            "source_type": self.source_type,
            "tier": self.tier,
            "priority": self.priority,
            "url": self.url,
            "mobile_url": self.mobile_url,
            "rank": self.rank,
            "guid": self.guid,
            "published_at": self.published_at,
            "summary": self.summary,
            "content": self.content,
            "author": self.author,
            "notified": self.notified,
            "first_crawl_time": self.first_crawl_time,
            "last_crawl_time": self.last_crawl_time,
            "crawl_count": self.crawl_count,
        }
        if self.ranks:
            d["ranks"] = self.ranks
        return d


@dataclass
class NewsData:
    """Collection of news items from a single crawl session."""

    date: str
    crawl_time: str
    items: Dict[str, List[NewsItem]] = field(default_factory=dict)
    id_to_name: Dict[str, str] = field(default_factory=dict)
    failed_ids: List[str] = field(default_factory=list)


def convert_crawl_results_to_news_data(
    results: Dict[str, Dict],
    id_to_name: Dict[str, str],
    failed_ids: List[str],
    crawl_time: str,
    crawl_date: str,
) -> NewsData:
    """Convert raw crawler results dict to NewsData.

    Args:
        results: {source_id: {title: {ranks: [], url: "", mobileUrl: ""}}}
        id_to_name: source_id -> display name mapping
        failed_ids: list of source_ids that failed
        crawl_time: HH:MM format
        crawl_date: YYYY-MM-DD format

    Returns:
        NewsData object
    """
    items = {}

    for source_id, titles_data in results.items():
        source_name = id_to_name.get(source_id, source_id)
        news_list = []

        for title, data in titles_data.items():
            ranks = data.get("ranks", [])
            url = data.get("url", "")
            mobile_url = data.get("mobileUrl", "")

            rank = ranks[0] if ranks else 99

            news_item = NewsItem(
                title=title,
                source_id=source_id,
                source_name=source_name,
                source_type="hotlist",
                rank=rank,
                url=url,
                mobile_url=mobile_url,
                published_at=data.get("published_at", ""),
                first_crawl_time=crawl_time,
                last_crawl_time=crawl_time,
                crawl_count=1,
                ranks=ranks,
            )
            news_list.append(news_item)

        items[source_id] = news_list

    return NewsData(
        date=crawl_date,
        crawl_time=crawl_time,
        items=items,
        id_to_name=id_to_name,
        failed_ids=failed_ids,
    )


def convert_rss_items_to_news_data(
    rss_data: Dict[str, List],
    id_to_name: Dict[str, str],
    failed_ids: List[str],
    crawl_time: str,
    crawl_date: str,
) -> NewsData:
    """Convert RSS fetch results to NewsData format.

    Args:
        rss_data: {feed_id: [entry_dict, ...]} from RSSFetcher
        id_to_name: feed_id -> display name mapping
        failed_ids: list of feed_ids that failed
        crawl_time: HH:MM format
        crawl_date: YYYY-MM-DD format

    Returns:
        NewsData object
    """
    items = {}

    for feed_id, feed_entries in rss_data.items():
        source_name = id_to_name.get(feed_id, feed_id)
        news_list = []

        for entry in feed_entries:
            news_item = NewsItem(
                title=entry.get("title", ""),
                source_id=feed_id,
                source_name=source_name,
                source_type="rss",
                url=entry.get("url", ""),
                guid=entry.get("guid", ""),
                published_at=entry.get("published_at", ""),
                summary=entry.get("summary", ""),
                author=entry.get("author", ""),
                first_crawl_time=crawl_time,
                last_crawl_time=crawl_time,
                crawl_count=1,
            )
            news_list.append(news_item)

        items[feed_id] = news_list

    return NewsData(
        date=crawl_date,
        crawl_time=crawl_time,
        items=items,
        id_to_name=id_to_name,
        failed_ids=failed_ids,
    )
