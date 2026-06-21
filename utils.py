# coding=utf-8
"""Time formatting and URL normalization utilities."""

from datetime import datetime
from typing import Optional
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import pytz

DEFAULT_TIMEZONE = "Asia/Shanghai"
# ── Time utilities ──────────────────────────────────────────────

def get_configured_time(timezone: str = DEFAULT_TIMEZONE) -> datetime:
    """Get current time in configured timezone."""
    tz = pytz.timezone(timezone)
    return datetime.now(tz)


def format_date_today(timezone: str = DEFAULT_TIMEZONE) -> str:
    """Get today's date as YYYY-MM-DD."""
    return get_configured_time(timezone).strftime("%Y-%m-%d")


def format_time_now(timezone: str = DEFAULT_TIMEZONE) -> str:
    """Get current time as HH:MM."""
    return get_configured_time(timezone).strftime("%H:%M")


def format_datetime_now(timezone: str = DEFAULT_TIMEZONE) -> str:
    """Get current datetime as YYYY-MM-DD HH:MM:SS (default for published_at)."""
    return get_configured_time(timezone).strftime("%Y-%m-%d %H:%M:%S")


# ── Filename sanitization ───────────────────────────────────────

def sanitize_filename(title: str, max_len: int = 80) -> str:
    """将标题转为安全的文件名，保留中英文可读性。

    - 保留中文、英文、数字
    - 空格和空白替换为 ``-``
    - 移除文件系统非法字符：``/ \\ : * ? " < > |``
    - 多个连字符合并为一个
    - 截断至 *max_len* 字符以内
    - 空字符串返回 ``"untitled"``
    """
    import re

    # 移除文件系统非法字符
    safe = re.sub(r'[/\\:*?"<>|]', '', title)
    # 空格和空白替换为连字符
    safe = re.sub(r'\s+', '-', safe)
    # 多个连字符合并
    safe = re.sub(r'-{2,}', '-', safe)
    # 去除首尾连字符
    safe = safe.strip('-')
    # 截断
    if len(safe) > max_len:
        safe = safe[:max_len].rstrip('-')
    return safe or "untitled"


# ── URL normalization ───────────────────────────────────────────

PLATFORM_PARAMS_TO_REMOVE = {
    "weibo": {"band_rank", "Refer", "t"},
}

COMMON_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "ref", "referrer", "source", "channel",
    "_t", "timestamp", "_", "random",
    "share_token", "share_id", "share_from",
}


def normalize_url(url: str, platform_id: str = "") -> str:
    """Normalize URL by removing tracking parameters.

    Args:
        url: Raw URL string
        platform_id: Source platform ID (for platform-specific params)

    Returns:
        Normalized URL string
    """
    if not url:
        return url
    try:
        parsed = urlparse(url)
        if not parsed.query:
            return url

        params = parse_qs(parsed.query, keep_blank_values=True)
        remove_params = set(COMMON_TRACKING_PARAMS)
        if platform_id and platform_id in PLATFORM_PARAMS_TO_REMOVE:
            remove_params.update(PLATFORM_PARAMS_TO_REMOVE[platform_id])

        cleaned = {
            k: v for k, v in params.items()
            if k.lower() not in {p.lower() for p in remove_params}
        }

        new_query = urlencode(cleaned, doseq=True)
        new_parsed = parsed._replace(query=new_query, fragment="")
        return urlunparse(new_parsed)
    except Exception:
        return url
