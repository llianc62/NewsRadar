# coding=utf-8
"""
Configuration loader module.

Responsible for loading configuration from YAML files and environment
variables.  Follows the 12-factor pattern: environment variables take
precedence over file values.

Each config section has its own ``_load_*_config()`` function.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Optional

import yaml


# =========================================================================
# Environment variable helpers
# =========================================================================

def _get_env_str(key: str, default: str = "") -> str:
    value = os.environ.get(key, "").strip()
    return value if value else default


def _get_env_int(key: str, default: int = 0) -> int:
    value = os.environ.get(key, "").strip()
    if not value:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _get_env_bool(key: str) -> Optional[bool]:
    value = os.environ.get(key, "").strip().lower()
    if not value:
        return None
    return value in ("true", "1", "yes")


# =========================================================================
# Config section loaders
# =========================================================================

def _load_app_config(raw: Dict) -> Dict:
    app = raw.get("app", {})
    return {
        "timezone": _get_env_str("NEWSNOW_TIMEZONE") or app.get("timezone", "Asia/Shanghai"),
    }


def _load_crawler_config(raw: Dict) -> Dict:
    crawler = raw.get("crawler", {})
    return {
        "request_interval": _get_env_int("NEWSNOW_REQUEST_INTERVAL")
        or crawler.get("request_interval", 2000),
        "daemon_interval_minutes": crawler.get("daemon_interval_minutes", 60),
    }


def _load_platforms_config(raw: Dict) -> Dict:
    platforms = raw.get("platforms", {})
    return {
        "enabled": platforms.get("enabled", True),
        "sources": platforms.get("sources", []),
    }


def _load_rss_config(raw: Dict) -> Dict:
    rss = raw.get("rss", {})
    env_enabled = _get_env_bool("NEWSNOW_RSS_ENABLED")
    return {
        "enabled": env_enabled if env_enabled is not None else rss.get("enabled", False),
        "request_interval": rss.get("request_interval", 1000),
        "timeout": rss.get("timeout", 20),
        "feeds": rss.get("feeds", []),
    }


def _load_notification_config(raw: Dict) -> Dict:
    notification = raw.get("notification", {})
    email = notification.get("email", {})
    return {
        "frequency_words": notification.get("frequency_words", "news/frequency_words.txt"),
        "max_news_per_keyword": notification.get("max_news_per_keyword", 0),
        "email": {
            "smtp_server": _get_env_str("NEWSNOW_EMAIL_SMTP_SERVER")
            or email.get("smtp_server", "smtp.qq.com"),
            "smtp_port": _get_env_int("NEWSNOW_EMAIL_SMTP_PORT")
            or email.get("smtp_port", 587),
            "from_addr": _get_env_str("NEWSNOW_EMAIL_FROM_ADDR")
            or email.get("from_addr", ""),
            "to_addr": _get_env_str("NEWSNOW_EMAIL_TO_ADDR")
            or email.get("to_addr", ""),
            "password": _get_env_str("EMAIL_PASSWORD")
            or _get_env_str("NEWSNOW_EMAIL_PASSWORD")
            or email.get("password", ""),
        },
    }


def _load_storage_config(raw: Dict) -> Dict:
    storage = raw.get("storage", {})
    local = storage.get("local", {})
    remote = storage.get("remote", {})
    return {
        "local": {
            "data_dir": local.get("data_dir", "output"),
        },
        "remote": {
            "endpoint_url": _get_env_str("S3_ENDPOINT_URL")
            or _get_env_str("NEWSNOW_S3_ENDPOINT_URL")
            or remote.get("endpoint_url", ""),
            "bucket_name": _get_env_str("S3_BUCKET_NAME")
            or _get_env_str("NEWSNOW_S3_BUCKET_NAME")
            or remote.get("bucket_name", ""),
            "access_key_id": _get_env_str("S3_ACCESS_KEY_ID")
            or _get_env_str("NEWSNOW_S3_ACCESS_KEY_ID")
            or remote.get("access_key_id", ""),
            "secret_access_key": _get_env_str("S3_SECRET_ACCESS_KEY")
            or _get_env_str("NEWSNOW_S3_SECRET_ACCESS_KEY")
            or remote.get("secret_access_key", ""),
            "region": _get_env_str("S3_REGION")
            or _get_env_str("NEWSNOW_S3_REGION")
            or remote.get("region", ""),
        },
    }


def _load_postgresql_config(raw: Dict) -> Dict:
    pg = raw.get("postgresql", {})
    return {
        "host": _get_env_str("PG_HOST") or pg.get("host", "localhost"),
        "port": _get_env_int("PG_PORT") or pg.get("port", 5432),
        "database": _get_env_str("PG_DATABASE") or pg.get("database", "newsradar"),
        "user": _get_env_str("PG_USER") or pg.get("user", "newsradar"),
        "password": _get_env_str("PG_PASSWORD") or pg.get("password", ""),
        "min_connections": pg.get("min_connections", 2),
        "max_connections": pg.get("max_connections", 10),
    }


def _load_minio_config(raw: Dict) -> Dict:
    minio = raw.get("minio", {})
    return {
        "endpoint_url": _get_env_str("MINIO_ENDPOINT_URL") or minio.get("endpoint_url", ""),
        "bucket_name": _get_env_str("MINIO_BUCKET_NAME") or minio.get("bucket_name", ""),
        "access_key_id": _get_env_str("MINIO_ACCESS_KEY_ID") or minio.get("access_key_id", ""),
        "secret_access_key": _get_env_str("MINIO_SECRET_ACCESS_KEY") or minio.get("secret_access_key", ""),
        "region": _get_env_str("MINIO_REGION") or minio.get("region", ""),
    }


def _load_web_config(raw: Dict) -> Dict:
    web = raw.get("web", {})
    return {
        "host": _get_env_str("NEWSNOW_WEB_HOST") or web.get("host", "0.0.0.0"),
        "port": _get_env_int("NEWSNOW_WEB_PORT") or web.get("port", 8000),
    }


# =========================================================================
# Main loader
# =========================================================================

def load_config(path: str = "config.yaml") -> Dict[str, Any]:
    """Load configuration file, merging environment variables.

    Environment variables take precedence over file values (12-factor style).
    All env var mapping is centralized in the ``_load_*_config()`` functions.

    Returns:
        Structured config dict accessible by path:
        ``config["app"]["timezone"]``, ``config["storage"]["remote"]["bucket_name"]``
    """
    config_path = os.environ.get("CONFIG_PATH", path)

    if not Path(config_path).exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(config_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    print(f"Config loaded: {config_path}")

    config = {
        "app": _load_app_config(raw),
        "crawler": _load_crawler_config(raw),
        "platforms": _load_platforms_config(raw),
        "rss": _load_rss_config(raw),
        "notification": _load_notification_config(raw),
        "storage": _load_storage_config(raw),
        "postgresql": _load_postgresql_config(raw),
        "minio": _load_minio_config(raw),
        "web": _load_web_config(raw),
    }

    _print_config_sources(config)
    return config


def _print_config_sources(config: Dict) -> None:
    """Print key config sources (env var vs file)."""
    sources = []

    remote = config["storage"]["remote"]
    if remote["endpoint_url"]:
        src = "env" if os.environ.get("S3_ENDPOINT_URL") or os.environ.get("NEWSNOW_S3_ENDPOINT_URL") else "file"
        sources.append(f"S3({src})")
    else:
        sources.append("S3(unconfigured)")

    email = config["notification"]["email"]
    if email["from_addr"] and email["to_addr"]:
        src = "env" if os.environ.get("NEWSNOW_EMAIL_FROM_ADDR") else "file"
        sources.append(f"Email({src})")
    else:
        sources.append("Email(unconfigured)")

    print(f"Config sources: {', '.join(sources)}")
