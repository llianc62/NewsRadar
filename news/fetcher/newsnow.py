# coding=utf-8
"""NewsNow hot-list API fetcher.

Provides:
- ``NewsFetcher`` — low-level HTTP client for the NewsNow API
- ``NewsnowFetcher`` — high-level :class:`Fetcher` subclass
"""

import json
import random
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, Union

import requests

from news.fetcher.fetcher import Fetcher

# Default request headers (Chrome UA, Accept JSON, zh-CN)
_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/91.0.4472.124 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Connection": "keep-alive",
    "Cache-Control": "no-cache",
}


# ═══════════════════════════════════════════════════════════════════
# NewsFetcher — low-level NewsNow API client
# ═══════════════════════════════════════════════════════════════════


class NewsFetcher:
    """Low-level HTTP client for the NewsNow hot-list API.

    Handles retry with exponential backoff, jittered rate limiting,
    and response parsing.  Used internally by :class:`NewsnowFetcher`.
    """

    def __init__(self, url: str, timeout: int = 20):
        self.url = url
        self.timeout = timeout

    def fetch_data(
        self,
        id_info: Union[str, Tuple[str, str]],
        max_retries: int = 2,
    ) -> Tuple[Optional[str], str, str]:
        """Fetch data for a single platform with exponential backoff.

        Args:
            id_info: Platform ID string, or (platform_id, alias) tuple.
            max_retries: Maximum retry attempts (default 2 = up to 3 total).

        Returns:
            (response_text, platform_id, alias) tuple.
            response_text is None when all attempts fail.
        """
        if isinstance(id_info, tuple):
            id_value, alias = id_info
        else:
            id_value = id_info
            alias = id_value

        url = f"{self.url}?id={id_value}&latest"

        for attempt in range(max_retries + 1):
            try:
                response = requests.get(
                    url,
                    headers=_DEFAULT_HEADERS,
                    timeout=self.timeout,
                )
                response.raise_for_status()

                data_json = json.loads(response.text)
                status = data_json.get("status", "unknown")

                if status not in ("success", "cache"):
                    raise ValueError(f"Unexpected response status: {status}")

                status_label = "fresh" if status == "success" else "cached"
                print(f"Fetched {id_value} successfully ({status_label})")
                return response.text, id_value, alias

            except Exception as e:
                if attempt < max_retries:
                    # Exponential backoff: base 3-5s, double each retry
                    base = random.uniform(3, 5)
                    wait = base * (2 ** attempt)
                    print(
                        f"Request for {id_value} failed: {e}. "
                        f"Retrying in {wait:.2f}s (attempt {attempt + 1}/{max_retries})..."
                    )
                    time.sleep(wait)
                else:
                    print(f"Request for {id_value} failed after {max_retries} retries: {e}")
                    return None, id_value, alias

        return None, id_value, alias

    def crawl_websites(
        self,
        ids_list: List[Union[str, Tuple[str, str]]],
        interval: int = 2000,
    ) -> Dict:
        """Batch-crawl multiple platforms with rate limiting and jitter.

        Args:
            ids_list: List of platform IDs (str) or (id, alias) tuples.
            interval: Minimum interval between requests in milliseconds.

        Returns:
            {source_id: {title: {"ranks": [1,2], "url": "...", "mobileUrl": "..."}}}
            Failed sources are logged and excluded from results.
        """
        results: Dict[str, Dict] = {}
        failed_ids: List[str] = []

        for i, id_info in enumerate(ids_list):
            id_value = id_info[0] if isinstance(id_info, tuple) else id_info

            response, _, _ = self.fetch_data(id_info)

            if response:
                try:
                    data = json.loads(response)
                    results[id_value] = {}

                    updated_ts = data.get("updatedTime")
                    if updated_ts:
                        published_date = datetime.fromtimestamp(
                            updated_ts / 1000
                        ).strftime("%Y-%m-%d")
                    else:
                        published_date = datetime.now().strftime("%Y-%m-%d")

                    for index, item in enumerate(data.get("items", []), 1):
                        title = item.get("title")
                        # Skip invalid titles (None, float, empty)
                        if (
                            title is None
                            or isinstance(title, float)
                            or not str(title).strip()
                        ):
                            continue
                        title = str(title).strip()
                        url = item.get("url", "")
                        mobile_url = item.get("mobileUrl", "")

                        if title in results[id_value]:
                            results[id_value][title]["ranks"].append(index)
                        else:
                            results[id_value][title] = {
                                "ranks": [index],
                                "url": url,
                                "mobileUrl": mobile_url,
                                "published_at": published_date,
                            }
                except json.JSONDecodeError:
                    print(f"Failed to parse response for {id_value}")
                    failed_ids.append(id_value)
                except Exception as e:
                    print(f"Error processing data for {id_value}: {e}")
                    failed_ids.append(id_value)
            else:
                failed_ids.append(id_value)

            # Rate limiting with jitter (skip after the last request)
            if i < len(ids_list) - 1:
                jitter = random.uniform(-0.15, 0.15) * interval
                actual_interval = max(50, interval + jitter)
                time.sleep(actual_interval / 1000)

        print(f"Success: {list(results.keys())}, Failed: {failed_ids}")
        return results


# ═══════════════════════════════════════════════════════════════════
# NewsnowFetcher — Fetcher subclass
# ═══════════════════════════════════════════════════════════════════


class NewsnowFetcher(Fetcher):
    """Fetch hot-list news from the NewsNow API.

    Receives the full application config and extracts the
    ``crawler.newsnow`` section internally.

    Usage::

        fetcher = NewsnowFetcher(config)
        results = fetcher.fetch()
    """

    def __init__(self, config: dict):
        cfg = config.get("crawler", {}).get("newsnow", {})
        self._enabled = cfg.get("enabled", True)
        self._url = cfg.get("url", "https://newsnow.busiyi.world/api/s")
        self._timeout = cfg.get("timeout", 20)
        self._interval = cfg.get("interval", 2000)
        self._sources = cfg.get("sources", [])

        self._client = NewsFetcher(url=self._url, timeout=self._timeout)

    @property
    def enabled(self) -> bool:
        """Whether this fetcher has sources configured."""
        return self._enabled and len(self._sources) > 0

    def fetch(self) -> List[Dict[str, Any]]:
        """Fetch hot-list data from all configured sources.

        Returns:
            Flat list of standardised item dicts.
        """
        if not self.enabled:
            return []

        # Build source-id -> display-name lookup
        id_to_name: Dict[str, str] = {
            s["id"]: s.get("name", s["id"]) for s in self._sources
        }

        ids_list = [(s["id"], s["name"]) for s in self._sources]
        results = self._client.crawl_websites(ids_list, self._interval)

        items: List[Dict[str, Any]] = []
        for source_id, titles_data in results.items():
            source_name = id_to_name.get(source_id, source_id)
            for title, info in titles_data.items():
                ranks = info.get("ranks", [])
                items.append({
                    "title": title,
                    "source_id": source_id,
                    "source_name": source_name,
                    "source_type": "hotlist",
                    "url": info.get("url", ""),
                    "mobile_url": info.get("mobileUrl", ""),
                    "rank": ranks[0] if ranks else 99,
                    "guid": "",
                    "published_at": info.get("published_at", ""),
                    "summary": "",
                    "author": "",
                    "content": "",
                    "category": "",
                    "tags": [],
                    "ranks": ranks,
                })

        return items
