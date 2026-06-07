# coding=utf-8
"""NewsRadar CLI — thin entry point for GitHub Actions.

Usage::

    python cli.py crawl     # Fetch news → SQLite → S3 upload
    python cli.py notify    # Download DB → keyword match → email report

This file is intentionally minimal. All logic lives in the ``news``
and ``storage`` packages.  ``main.py`` is the local daemon.
"""

import sys

from config.loader import load_config


def cmd_crawl() -> None:
    """Fetch news from all sources, save to SQLite, upload to S3."""
    config = load_config("config.yaml")
    from news.crawler import fetch_all, save_to_sqlite, build_source_tiers

    news_data, source_tiers = fetch_all(config)
    save_to_sqlite(news_data, source_tiers, config)


def cmd_notify() -> None:
    """Generate keyword-matched HTML report and send via email."""
    config = load_config("config.yaml")
    from news.notifier import run_notifier

    run_notifier(config)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python cli.py [crawl|notify]")
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "crawl":
        cmd_crawl()
    elif cmd == "notify":
        cmd_notify()
    else:
        print(f"Unknown command: {cmd}")
        print("Usage: python cli.py [crawl|notify]")
        sys.exit(1)
