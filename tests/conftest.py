"""Shared fixtures for parser tests."""

import os
from pathlib import Path

import pytest

# Load .env before any config loading — needed because credentials were
# moved from config.yaml to .env.
_env_path = Path(__file__).resolve().parent.parent / ".env"
if _env_path.exists():
    import dotenv
    dotenv.load_dotenv(_env_path, override=True)

pytest_plugins = "tests.conftest_db"


@pytest.fixture
def parser():
    """Return a default-configured HtmlParser instance."""
    from news.parser import HtmlParser

    return HtmlParser()
