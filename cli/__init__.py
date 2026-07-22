# coding=utf-8
"""NewsRadar CLI package."""

import typer

app = typer.Typer(name="newsradar")

from cli import crawl, notify, grab  # noqa: F401, E402
import cli.db  # noqa: F401, E402  - register db subcommand group
import cli.knowledge  # noqa: F401, E402  - register knowledge subcommand group
import cli.agent  # noqa: F401, E402  - register agent subcommand group
