# coding=utf-8
"""Abstract base class for news fetchers."""

from abc import ABC, abstractmethod
from typing import Any, Dict, List


class Fetcher(ABC):
    """Abstract base for all news data source fetchers.

    Each subclass implements :meth:`fetch` to retrieve news from a
    specific data source (hot-list API, RSS feeds, etc.) and returns a
    flat list of standardised item dicts.

    Returns:
        ``list[dict]`` — each dict follows the standard fetch-item
        schema defined in :mod:`news.constants`.
        Failures are logged internally by each implementation.
    """

    @abstractmethod
    def fetch(self) -> List[Dict[str, Any]]:
        """Fetch news data from the source.

        Returns:
            Flat list of standardised item dicts.
        """
        ...
