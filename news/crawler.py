# coding=utf-8
"""Crawler orchestrator — shared fetch logic for local daemon and cloud CI.

Both environments fetch from the same sources using the same code path.
The only difference is the storage destination:

* **Cloud CI** (``cli.py crawl``): fetch → SQLite → S3 upload
* **Local daemon** (``main.py``):   fetch → PostgreSQL → optional content fetch
"""

import time
from typing import Any, Dict, List, Optional, Tuple

import requests

from news.models import (
    NewsData,
    convert_crawl_results_to_news_data,
    convert_rss_items_to_news_data,
)
from news.fetcher import NewsFetcher, RSSFetcher
from utils import format_date_folder, format_time_display


def build_source_tiers(config: dict) -> dict:
    """Build {source_id: {tier, priority}} mapping from config.

    Used by both cloud and local paths to attach tier/priority
    metadata to news items before storage.
    """
    tiers = {}
    for source in config.get("platforms", {}).get("sources", []):
        tiers[source["id"]] = {
            "tier": source.get("tier", 4),
            "priority": source.get("priority", 0),
        }
    for feed in config.get("rss", {}).get("feeds", []):
        if feed.get("enabled", True):
            tiers[feed["id"]] = {
                "tier": feed.get("tier", 3),
                "priority": feed.get("priority", 0),
            }
    return tiers


def _enrich_with_content(news_data: NewsData, request_interval: int) -> None:
    """Download article pages and extract Markdown content for each item.

    Modifies *news_data* in place, setting ``item.content`` for items
    where extraction succeeds.  Items whose URL is empty or whose page
    cannot be fetched are left with ``content=""``.

    Args:
        news_data: The combined :class:`NewsData` from all sources.
        request_interval: Milliseconds to sleep between HTTP requests.
    """
    TIMEOUT = 30
    MAX_CONTENT_LENGTH = 100000

    # Check for trafilatura (optional dependency)
    has_trafilatura = False
    try:
        import trafilatura  # noqa: F401
        has_trafilatura = True
    except ImportError:
        pass

    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/91.0.4472.124 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    })

    total_items = sum(len(v) for v in news_data.items.values())
    processed = 0

    for _source_id, items in news_data.items.items():
        for item in items:
            if not item.url:
                continue

            if processed > 0:
                time.sleep(request_interval / 1000)

            try:
                resp = session.get(item.url, timeout=TIMEOUT)
                resp.raise_for_status()
                html_text = resp.text
            except requests.RequestException as e:
                print(f"[Content] HTTP error for {item.url}: {e}")
                processed += 1
                continue

            markdown = None
            if has_trafilatura:
                import trafilatura
                result = trafilatura.extract(
                    html_text,
                    url=item.url,
                    output_format="markdown",
                    with_metadata=True,
                    include_tables=True,
                    include_images=True,
                    include_links=True,
                    include_formatting=True,
                )
                if result and len(result.strip()) > 50:
                    markdown = result.strip()

            # Fallback when trafilatura is unavailable or fails to extract
            if markdown is None:
                import re
                from html import unescape
                text = re.sub(
                    r'<(script|style|nav|header|footer|aside)[^>]*>.*?</\1>',
                    '', html_text, flags=re.DOTALL | re.IGNORECASE,
                )
                text = re.sub(r'<[^>]+>', ' ', text)
                text = unescape(text)
                text = re.sub(r'\s+', ' ', text).strip()
                paragraphs = [p.strip() for p in text.split('\n') if len(p.strip()) > 80]
                if paragraphs:
                    markdown = '\n\n'.join(paragraphs)
                elif len(text) > 100:
                    markdown = text

            if markdown:
                if len(markdown) > MAX_CONTENT_LENGTH:
                    markdown = markdown[:MAX_CONTENT_LENGTH] + "\n\n... (truncated)"
                item.content = markdown

            processed += 1

    session.close()
    print(f"[Content] Enriched {sum(1 for items in news_data.items.values() for it in items if it.content)}/{total_items} items with content")


def fetch_all(config: dict, with_content: bool = False) -> Tuple[NewsData, dict]:
    """Fetch from all configured news sources (hot-list + RSS).

    This is the single shared fetch path — used identically by the
    local daemon and the cloud CI crawler.

    Args:
        config: Full application config dict.

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
    if config["platforms"]["enabled"]:
        request_interval = config["crawler"]["request_interval"]
        sources = config["platforms"]["sources"]
        ids_list = [(s["id"], s["name"]) for s in sources]

        print(f"\n[Hot-list] Fetching {len(ids_list)} platforms...")
        fetcher = NewsFetcher()
        results, id_to_name, failed_ids = fetcher.crawl_websites(
            ids_list, request_interval
        )

        if results:
            hotlist_data = convert_crawl_results_to_news_data(
                results, id_to_name, failed_ids, time_str, date
            )
            all_items.update(hotlist_data.items)
            all_id_to_name.update(id_to_name)
            all_failed_ids.extend(failed_ids)

    # ── Fetch RSS ──────────────────────────────────────────────────
    rss_cfg = config["rss"]
    if rss_cfg["enabled"]:
        print("\n[RSS] Fetching feeds...")
        rss_fetcher = RSSFetcher.from_config(rss_cfg)
        rss_results, rss_id_to_name, rss_failed_ids = rss_fetcher.fetch_all()

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

    if with_content:
        request_interval = config.get("crawler", {}).get("request_interval", 2000)
        _enrich_with_content(news_data, request_interval)

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
