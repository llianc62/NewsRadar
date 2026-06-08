# coding=utf-8
"""Notify command."""

from cli import app
from config.loader import load_config


@app.command()
def notify():
    """Generate keyword-matched HTML report and send via email."""
    config = load_config("config.yaml")
    from news.notifier import run_notifier
    run_notifier(config)
