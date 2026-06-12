# coding=utf-8
"""Notify command."""

from cli import app
from config.loader import load_config
from news.notifier import run_notifier

@app.command()
def notify():
    """Generate keyword-matched HTML report and send via email."""
    config = load_config("config.yaml")
    run_notifier(config)
