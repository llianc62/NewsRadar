# coding=utf-8
"""Notify command."""

import typer

from cli import app
from config.loader import load_config
from news.notifier import run_notifier


@app.command()
def notify(
    dry_run: bool = typer.Option(
        False, "--dry-run",
        help="Render and save the HTML report but do not send email",
    ),
    start_time: str | None = typer.Option(
        None, "--start-time",
        help="仅通知此时间（ISO 8601）之后创建的新闻",
    ),
    end_time: str | None = typer.Option(
        None, "--end-time",
        help="仅通知此时间（ISO 8601）之前创建的新闻",
    ),
):
    """Generate keyword-matched HTML report and send via email.

    The report is always saved to ``output/html/<date>/<time>.html``.
    Use ``--dry-run`` to preview the report without sending.
    Use ``--start-time`` / ``--end-time`` for incremental notifications.
    """
    config = load_config("config.yaml")
    run_notifier(
        config,
        dry_run=dry_run,
        start_time=start_time,
        end_time=end_time,
    )
