# coding=utf-8
"""Crawler orchestrator — shared fetch logic for local daemon and cloud CI.

Both environments fetch from the same sources using the same code path.
The only difference is the storage destination:

* **Cloud CI** (``cli.py crawl``): fetch → SQLite → S3 upload
* **Local daemon** (``main.py``):   fetch → PostgreSQL → optional content fetch
"""

from typing import Any, Dict, List, Optional, Tuple

from news.models import (
    NewsData,
    convert_crawl_results_to_news_data,
    convert_rss_items_to_news_data,
)
from news.fetcher import NewsnowFetcher, RssFetcher
from news.grabber import Grabber, OutputStyle
from utils import format_date_folder, format_time_display


def build_source_tiers(config: dict) -> dict:
    """Build {source_id: {tier, priority}} mapping from config.

    Used by both cloud and local paths to attach tier/priority
    metadata to news items before storage.
    """
    tiers = {}
    for source in config.get("crawler", {}).get("newsnow", {}).get("sources", []):
        tiers[source["id"]] = {
            "tier": source.get("tier", 4),
            "priority": source.get("priority", 0),
        }
    for feed in config.get("crawler", {}).get("rss", {}).get("feeds", []):
        if feed.get("enabled", True):
            tiers[feed["id"]] = {
                "tier": feed.get("tier", 3),
                "priority": feed.get("priority", 0),
            }
    return tiers



def fetch_all(
    config: dict,
    with_content: bool = False,
    output_style: OutputStyle | None = None,
) -> Tuple[NewsData, dict]:
    """Fetch from all configured news sources (hot-list + RSS).

    This is the single shared fetch path — used identically by the
    local daemon and the cloud CI crawler.

    When *with_content* is True and *output_style* is given, the
    :class:`~news.grabber.Grabber` downloads article HTML, parses
    Markdown, and saves content via the specified backend.

    Args:
        config: Full application config dict.
        with_content: Whether to download and parse article content.
        output_style: Storage target for content (MARKDOWN/HTML/SQLITE/POSTGRESQL).

    Returns:
        (news_data, source_tiers) tuple.
        *news_data* is a combined :class:`NewsData` from all sources.
        *source_tiers* is the ``{source_id: {tier, priority}}`` mapping.
    """
    timezone = config["app"]["timezone"]
    date = format_date_folder(timezone)
    time_str = format_time_display(timezone)

    print(f"=== Crawler === {date} {time_str}")

    source_tiers = build_source_tiers(config)
    all_items: Dict[str, List] = {}
    all_id_to_name: Dict[str, str] = {}
    all_failed_ids: List[str] = []

    # ── Fetch hot-list ─────────────────────────────────────────────
    newsnow_cfg = config["crawler"]["newsnow"]
    if newsnow_cfg["enabled"]:
        sources = newsnow_cfg["sources"]
        ids_list = [(s["id"], s["name"]) for s in sources]

        print(f"\n[Hot-list] Fetching {len(ids_list)} platforms...")
        fetcher = NewsnowFetcher(config)
        results, id_to_name, failed_ids = fetcher.fetch()

        if results:
            hotlist_data = convert_crawl_results_to_news_data(
                results, id_to_name, failed_ids, time_str, date
            )
            all_items.update(hotlist_data.items)
            all_id_to_name.update(id_to_name)
            all_failed_ids.extend(failed_ids)

    # ── Fetch RSS ──────────────────────────────────────────────────
    rss_cfg = config["crawler"]["rss"]
    if rss_cfg["enabled"]:
        print("\n[RSS] Fetching feeds...")
        rss_fetcher = RssFetcher(config, timezone)
        rss_results, rss_id_to_name, rss_failed_ids = rss_fetcher.fetch()

        if rss_results:
            rss_data = convert_rss_items_to_news_data(
                rss_results, rss_id_to_name, rss_failed_ids, time_str, date
            )
            all_items.update(rss_data.items)
            all_id_to_name.update(rss_id_to_name)
            all_failed_ids.extend(rss_failed_ids)

    news_data = NewsData(
        date=date,
        crawl_time=time_str,
        items=all_items,
        id_to_name=all_id_to_name,
        failed_ids=all_failed_ids,
    )

    if with_content and output_style:
        items_dicts = [
            item.to_dict()
            for items in all_items.values()
            for item in items
        ]
        grabber = Grabber(config=config)
        grabber.run_batch(items_dicts, output_style)

        # Map content back from dicts to original NewsItem objects
        content_map = {
            d["url"]: d["content"]
            for d in items_dicts
            if d.get("content")
        }
        for items in all_items.values():
            for item in items:
                if item.url in content_map:
                    item.content = content_map[item.url]

    print(f"=== Fetch complete: {sum(len(v) for v in all_items.values())} items, "
          f"{len(all_failed_ids)} failed sources ===")

    return news_data, source_tiers


# ── Storage helpers (dispatch to the right backend) ──────────────


def save_to_sqlite(news_data: NewsData, source_tiers: dict, config: dict) -> None:
    """Save fetched data to SQLite and upload to S3 (cloud CI path)."""
    from storage.sqlite import Storage

    storage_config = config.get("storage", {})
    storage = Storage(
        data_dir=storage_config.get("local", {}).get("data_dir", "output"),
        timezone=config.get("app", {}).get("timezone", "Asia/Shanghai"),
        s3_config=storage_config.get("remote") or None,
    )
    storage.save_news_data(news_data, source_tiers)
    storage.cleanup()


def save_to_postgres(news_data: NewsData, source_tiers: dict, db) -> None:
    """Save fetched data to PostgreSQL (local daemon path).

    Args:
        news_data: The fetched NewsData.
        source_tiers: {source_id: {tier, priority}} mapping.
        db: A connected :class:`storage.postgres.Database` instance.
    """
    from storage.postgres import Database

    if not db.is_connected:
        db.connect()
        db.init_schema()

    result = db.save_news_data(news_data, source_tiers, sync_status="local")
    print(f"[Crawler] PG save result: {result}")
