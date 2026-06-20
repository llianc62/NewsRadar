"""Shared fixtures for parser tests."""

import pytest

pytest_plugins = "tests.conftest_db"


@pytest.fixture
def parser():
    """Return a default-configured HtmlParser instance."""
    from news.parser import HtmlParser

    return HtmlParser()
