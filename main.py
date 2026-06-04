# coding=utf-8
"""NewsNow Crawler — main entry point.

Usage:
    python main.py crawl     # Fetch news, save to SQLite, upload to S3
    python main.py notify    # Generate HTML report and send email
"""

import os
import sys

import yaml

from models import (
    convert_crawl_results_to_news_data,
    convert_rss_items_to_news_data,
)
from utils import (
    get_configured_time,
    format_date_folder,
    format_time_display,
    DEFAULT_TIMEZONE,
)
from fetcher import DataFetcher, RSSFetcher
from storage import Storage


def load_config(path: str = "config.yaml") -> dict:
    """Load config.yaml, merging environment variable overrides for S3."""
    with open(path, encoding="utf-8") as f:
        config = yaml.safe_load(f)
    return config


def build_source_tiers(config: dict) -> dict:
    """Build {source_id: {tier, priority}} mapping from config."""
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


def cmd_crawl(config: dict):
    """Run the crawler: fetch + store + upload."""
    timezone = config.get("app", {}).get("timezone", DEFAULT_TIMEZONE)
    now = get_configured_time(timezone)
    date = format_date_folder(timezone)
    time_str = format_time_display(timezone)

    print(f"=== Crawler === {date} {time_str}")

    # Init storage
    storage_config = config.get("storage", {})
    storage = Storage(
        data_dir=storage_config.get("local", {}).get("data_dir", "output"),
        timezone=timezone,
        s3_config=storage_config.get("remote") or None,
    )
    source_tiers = build_source_tiers(config)

    # ── Fetch hot-list ─────────────────────────────────
    platforms_config = config.get("platforms", {})
    if platforms_config.get("enabled", True):
        crawler_config = config.get("crawler", {})
        request_interval = crawler_config.get("request_interval", 2000)

        sources = platforms_config.get("sources", [])
        ids_list = [(s["id"], s["name"]) for s in sources]

        print(f"\n[Hot-list] Fetching {len(ids_list)} platforms...")
        fetcher = DataFetcher()
        results, id_to_name, failed_ids = fetcher.crawl_websites(
            ids_list, request_interval
        )

        if results:
            news_data = convert_crawl_results_to_news_data(
                results, id_to_name, failed_ids, time_str, date
            )
            storage.save_news_data(news_data, source_tiers)

    # ── Fetch RSS ──────────────────────────────────────
    rss_config = config.get("rss", {})
    if rss_config.get("enabled", False):
        print(f"\n[RSS] Fetching feeds...")
        rss_fetcher = RSSFetcher.from_config(rss_config)
        rss_results, rss_id_to_name, rss_failed_ids = rss_fetcher.fetch_all()

        if rss_results:
            rss_news_data = convert_rss_items_to_news_data(
                rss_results, rss_id_to_name, rss_failed_ids, time_str, date
            )
            storage.save_news_data(rss_news_data, source_tiers)

    storage.cleanup()
    total = sum(len(v) for v in news_data.items.values()) if results else 0
    print(f"=== Done: {len(results) if results else 0} platforms, {total} items ===")


def cmd_notify(config: dict):
    """Run the notifier: query unnotified -> match keywords -> report -> email."""
    from frequency import load_frequency_words, match_and_group
    from notifier import build_html_report, send_email

    timezone = config.get("app", {}).get("timezone", DEFAULT_TIMEZONE)
    date = format_date_folder(timezone)
    time_str = format_time_display(timezone)

    print(f"=== Notifier === {date} {time_str}")

    # Init storage
    storage_config = config.get("storage", {})
    storage = Storage(
        data_dir=storage_config.get("local", {}).get("data_dir", "output"),
        timezone=timezone,
        s3_config=storage_config.get("remote") or None,
    )

    # Get unnotified items
    rows = storage.get_unnotified(date)
    if not rows:
        print("No new items to notify")
        storage.cleanup()
        return

    # Convert rows to dicts
    items = [dict(row) for row in rows]
    print(f"Unnotified items: {len(items)}")

    # Load keywords and match
    freq_path = config.get("notification", {}).get(
        "frequency_words", "frequency_words.txt"
    )
    if os.path.exists(freq_path):
        word_groups, filter_words, global_filters = load_frequency_words(freq_path)
        max_per = config.get("notification", {}).get("max_news_per_keyword", 0)
        grouped = match_and_group(items, word_groups, global_filters, max_per)
        print(f"Matched groups: {list(grouped.keys())}")
    else:
        grouped = {"全部新闻": items}

    # Build HTML report
    html = build_html_report(grouped, date, time_str, len(items))

    # Send email
    email_config = config.get("notification", {}).get("email", {})
    smtp_server = email_config.get("smtp_server", "smtp.qq.com")
    smtp_port = email_config.get("smtp_port", 587)
    from_addr = email_config.get("from_addr", "")
    to_addr = email_config.get("to_addr", "")
    password = email_config.get("password") or os.environ.get("EMAIL_PASSWORD", "")

    if not all([from_addr, to_addr, password]):
        print("[Email] Missing config — skipping send")
    else:
        send_email(html, smtp_server, smtp_port, from_addr, to_addr, password)

    # Mark as notified
    storage.mark_notified(date)

    storage.cleanup()
    print("=== Done ===")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python main.py [crawl|notify]")
        sys.exit(1)

    cmd = sys.argv[1]
    config = load_config()

    if cmd == "crawl":
        cmd_crawl(config)
    elif cmd == "notify":
        cmd_notify(config)
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
