# coding=utf-8
"""Crawl command — cloud CI entry point.

Fetches news from all configured sources, downloads article body,
and persists to SQLite.  This is the fixed cloud-CI workflow; for
manual testing use ``grab-one``.
"""

from cli import app
from config.loader import load_config
from news.crawler import Crawler, OutputStyle


@app.command()
def crawl():
    """Fetch news from all sources → download content → save to SQLite.

    Cloud CI fixed workflow — no flags needed.
    """
    config = load_config("config.yaml")
    crawler = Crawler(config)
    crawler.fetch_all(OutputStyle.SQLITE)
    crawler.close()
