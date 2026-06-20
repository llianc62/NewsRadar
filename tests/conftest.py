"""Shared fixtures for parser tests."""

import pytest


@pytest.fixture
def parser():
    """Return a default-configured HtmlParser instance."""
    from news.parser import HtmlParser

    return HtmlParser()
