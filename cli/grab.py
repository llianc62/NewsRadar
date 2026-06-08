# coding=utf-8
"""Grab command — test content extraction on a single URL."""

import typer

from cli import app
from config.loader import load_config
from news.grabber import Grabber, OutputStyle


@app.command()
def grab_one(
    url: str = typer.Argument(..., help="Web page URL to fetch and parse"),
    output_style: OutputStyle = typer.Option(
        OutputStyle.MARKDOWN, "--output-style", "-o",
        help="Storage target for the result",
    ),
    with_images: bool = typer.Option(
        False, "--images", help="Download and process images in the article"
    ),
):
    """Fetch a single URL, parse content, save per output_style."""
    config = load_config("config.yaml")

    image_processor = None
    if with_images:
        from news.parser import ImageProcessor
        image_processor = ImageProcessor(storage_backend="local", config=config)

    grabber = Grabber(config=config, image_processor=image_processor)
    grabber.run(url, output_style)
