# coding=utf-8
"""Grab command — manual testing for content extraction on a single URL.

Use ``grab-one`` to test downloading, parsing, and image extraction
against a single URL before running the full cloud ``crawl`` pipeline.
"""

import typer

from cli import app
from config.loader import load_config
from news.crawler import Crawler, OutputStyle


@app.command()
def grab_one(
    url: str = typer.Argument(..., help="Web page URL to fetch and parse"),
    output_style: OutputStyle = typer.Option(
        ..., "--output-style", "-o",
        help="Storage target for the result [required]",
    ),
    with_content: bool = typer.Option(
        True, "--content", help="Download article body and save to storage"
    ),
    with_image: bool = typer.Option(
        False, "--images", help="Download article images (implies Markdown output)"
    ),
):
    """Fetch a single URL, parse content, optionally download images.

    Default: download article body → Markdown files (no images).
    ``--image``: also download images referenced in the article.
    ``--no-content``: fetch metadata only, skip body download.
    """
    config = load_config("config.yaml")
    crawler = Crawler(config=config)
    crawler.fetch(url, output_style,
                  with_content=with_content, with_image=with_image)
    crawler.close()
