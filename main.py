# coding=utf-8
"""NewsNow Crawler — main entry point.

Usage:
    python main.py crawl     # Fetch news, save to SQLite, upload to S3
    python main.py notify    # Generate HTML report and send email
"""

import os
import sys

from config import load_config

from models import (
    convert_crawl_results_to_news_data,
    convert_rss_items_to_news_data,
)
from utils import (
    format_date_folder,
    format_time_display,
    DEFAULT_TIMEZONE,
)
from fetcher import DataFetcher, RSSFetcher
from storage import Storage



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
    """Run the crawler: fetch + store (PostgreSQL or SQLite fallback)."""
    timezone = config["app"]["timezone"]
    date = format_date_folder(timezone)
    time_str = format_time_display(timezone)

    print(f"=== Crawler === {date} {time_str}")

    # Check if PostgreSQL is available
    pg_config = config.get("postgresql", {})
    use_pg = bool(pg_config.get("host"))

    source_tiers = build_source_tiers(config)

    if use_pg:
        # ── PostgreSQL path ────────────────────────────
        from database import init_db, save_news_data, close_db

        init_db(config["postgresql"])

        total_new = 0
        total_updated = 0

        # ── Fetch hot-list ─────────────────────────────────
        if config["platforms"]["enabled"]:
            request_interval = config["crawler"]["request_interval"]
            sources = config["platforms"]["sources"]
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
                counts = save_news_data(news_data, source_tiers, sync_status="local")
                total_new += counts["new"]

        # ── Fetch RSS ──────────────────────────────────────
        rss_cfg = config["rss"]
        if rss_cfg["enabled"]:
            print("\n[RSS] Fetching feeds...")
            rss_fetcher = RSSFetcher.from_config(rss_cfg)
            rss_results, rss_id_to_name, rss_failed_ids = rss_fetcher.fetch_all()

            if rss_results:
                rss_news_data = convert_rss_items_to_news_data(
                    rss_results, rss_id_to_name, rss_failed_ids, time_str, date
                )
                counts = save_news_data(rss_news_data, source_tiers, sync_status="local")
                total_new += counts["new"]

        close_db()
    else:
        # ── SQLite fallback (GitHub Actions / legacy) ───
        storage = Storage(
            data_dir=config["storage"]["local"]["data_dir"],
            timezone=timezone,
            s3_config=config["storage"]["remote"] or None,
        )

        # ── Fetch hot-list ─────────────────────────────────
        if config["platforms"]["enabled"]:
            request_interval = config["crawler"]["request_interval"]
            sources = config["platforms"]["sources"]
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
        rss_cfg = config["rss"]
        if rss_cfg["enabled"]:
            print("\n[RSS] Fetching feeds...")
            rss_fetcher = RSSFetcher.from_config(rss_cfg)
            rss_results, rss_id_to_name, rss_failed_ids = rss_fetcher.fetch_all()

            if rss_results:
                rss_news_data = convert_rss_items_to_news_data(
                    rss_results, rss_id_to_name, rss_failed_ids, time_str, date
                )
                storage.save_news_data(rss_news_data, source_tiers)

        storage.cleanup()

    print(f"=== Done ===")


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


def cmd_sync(config: dict):
    """Sync cloud SQLite data into local PostgreSQL."""
    from sync import sync_from_cloud

    print("=== Cloud Sync ===")

    pg_config = config["postgresql"]
    s3_config = config["storage"]["remote"]

    if not s3_config.get("bucket_name") or not s3_config.get("endpoint_url"):
        print("[Sync] S3 not configured - cannot sync. Set S3_* env vars.")
        return

    result = sync_from_cloud(
        pg_config=pg_config,
        s3_config=s3_config,
        data_dir=config["storage"]["local"]["data_dir"],
    )
    print(f"Result: {result}")


def cmd_init_db(config: dict):
    """Initialize PostgreSQL schema only."""
    from database import init_db, close_db

    print("=== Init DB ===")
    init_db(config["postgresql"])
    print("Schema created successfully.")
    close_db()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python main.py [crawl|notify|sync|init-db]")
        sys.exit(1)

    cmd = sys.argv[1]
    cfg = load_config("config.yaml")

    if cmd == "crawl":
        cmd_crawl(cfg)
    elif cmd == "notify":
        cmd_notify(cfg)
    elif cmd == "sync":
        cmd_sync(cfg)
    elif cmd == "init-db":
        cmd_init_db(cfg)
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
