# coding=utf-8
"""Crawl command."""

import typer

from cli import app
from config.loader import load_config
from news.grabber import OutputStyle


@app.command()
def crawl(
    with_content: bool = typer.Option(
        False, "--content", help="Download article body and save to storage"
    ),
):
    """Fetch news from all sources (cloud CI — SQLite)."""
    config = load_config("config.yaml")
    from news.crawler import fetch_all, save_to_sqlite, build_source_tiers

    output_style = OutputStyle.SQLITE if with_content else None
    news_data, source_tiers = fetch_all(
        config, with_content=with_content, output_style=output_style,
    )
    save_to_sqlite(news_data, source_tiers, config)
