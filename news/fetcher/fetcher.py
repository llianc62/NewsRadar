# coding=utf-8
"""Abstract base class for news fetchers."""

from abc import ABC, abstractmethod
from typing import Dict, List, Tuple


class Fetcher(ABC):
    """Abstract base for all news data source fetchers.

    Each subclass implements :meth:`fetch` to retrieve news from a
    specific data source (hot-list API, RSS feeds, etc.).

    Returns:
        ``(results, id_to_name, failed_ids)`` tuple:

        * *results* — ``{source_id: raw_data}`` dict, format varies by
          fetcher type.
        * *id_to_name* — ``{source_id: display_name}`` mapping.
        * *failed_ids* — list of source IDs that failed to fetch.
    """

    @abstractmethod
    def fetch(self) -> Tuple[Dict, Dict, List]:
        """Fetch news data from the source.

        Returns:
            (results, id_to_name, failed_ids)
        """
        ...
