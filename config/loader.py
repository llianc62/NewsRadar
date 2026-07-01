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
        "daemon_interval_minutes": crawler.get("daemon_interval_minutes", 60),
        "sync_interval_minutes": crawler.get("sync_interval_minutes", 60),
        "max_retry": _get_env_int("CRAWLER_MAX_RETRY")
        or crawler.get("max_retry", 3),
        "max_workers": crawler.get("max_workers", 8),
        "timeout": crawler.get("timeout", 30),
        "newsnow": _load_newsnow_config(raw),
        "rss": _load_rss_config(raw),
    }


def _load_newsnow_config(raw: Dict) -> Dict:
    newsnow = raw.get("crawler", {}).get("newsnow", {})
    return {
        "enabled": newsnow.get("enabled", True),
        "url": newsnow.get("url", "https://newsnow.busiyi.world/api/s"),
        "timeout": newsnow.get("timeout", 20),
        "interval": newsnow.get("interval", 2000),
        "sources": newsnow.get("sources", []),
    }


def _load_rss_config(raw: Dict) -> Dict:
    rss = raw.get("crawler", {}).get("rss", {})
    env_enabled = _get_env_bool("RSS_ENABLED")
    return {
        "enabled": env_enabled if env_enabled is not None else rss.get("enabled", False),
        "interval": rss.get("interval", 1000),
        "timeout": rss.get("timeout", 20),
        "sources": rss.get("sources", []),
    }


def _load_notification_config(raw: Dict) -> Dict:
    notification = raw.get("notification", {})
    email = notification.get("email", {})
    return {
        "frequency_words": notification.get("frequency_words", "frequency_words.txt"),
        "keyword_limit_news": notification.get("keyword_limit_news", 0),
        "email": {
            "from_addr": _get_env_str("EMAIL_FROM_ADDR")
            or email.get("from_addr", ""),
            "to_addr": _get_env_str("EMAIL_TO_ADDR")
            or email.get("to_addr", ""),
            "password": _get_env_str("EMAIL_PASSWORD")
            or email.get("password", ""),
        },
    }


def _load_storage_config(raw: Dict) -> Dict:
    """Load storage config with separate cloud and resource sections.

    ``cloud`` — SQLite DB file transfer (CI ↔ daemon bridge).
        Env: ``CLOUD_S3_*`` → YAML.

    ``resource`` — project files/images (local MinIO object storage).
        Env: ``RESOURCE_S3_*`` → YAML.
    """
    storage = raw.get("storage", {})
    local = storage.get("local", {})
    cloud = storage.get("cloud", {})
    resource = storage.get("resource", {})
    return {
        "local": {
            "data_path": local.get("data_path", "output"),
        },
        "cloud": {
            "endpoint_url": _get_env_str("CLOUD_S3_ENDPOINT_URL")
            or cloud.get("endpoint_url", ""),
            "bucket_name": _get_env_str("CLOUD_S3_BUCKET_NAME")
            or cloud.get("bucket_name", ""),
            "access_key_id": _get_env_str("CLOUD_S3_ACCESS_KEY_ID")
            or cloud.get("access_key_id", ""),
            "secret_access_key": _get_env_str("CLOUD_S3_SECRET_ACCESS_KEY")
            or cloud.get("secret_access_key", ""),
            "region": _get_env_str("CLOUD_S3_REGION")
            or cloud.get("region", ""),
        },
        "resource": {
            "endpoint_url": _get_env_str("RESOURCE_S3_ENDPOINT_URL")
            or resource.get("endpoint_url", ""),
            "bucket_name": _get_env_str("RESOURCE_S3_BUCKET_NAME")
            or resource.get("bucket_name", ""),
            "access_key_id": _get_env_str("RESOURCE_S3_ACCESS_KEY_ID")
            or resource.get("access_key_id", ""),
            "secret_access_key": _get_env_str("RESOURCE_S3_SECRET_ACCESS_KEY")
            or resource.get("secret_access_key", ""),
            "region": _get_env_str("RESOURCE_S3_REGION")
            or resource.get("region", ""),
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


def _load_web_config(raw: Dict) -> Dict:
    web = raw.get("web", {})
    return {
        "host": _get_env_str("WEB_HOST") or web.get("host", "0.0.0.0"),
        "port": _get_env_int("WEB_PORT") or web.get("port", 8000),
    }


def _load_analyzer_config(raw: Dict) -> Dict:
    analyzer = raw.get("analyzer", {})
    heat_cfg = analyzer.get("heat", {})
    return {
        "enabled": analyzer.get("enabled", True),
        "backend": analyzer.get("backend", "jieba"),
        "heat": {
            "half_life_hours": heat_cfg.get("half_life_hours", 12),
            "tier_base": heat_cfg.get("tier_base", {1: 60, 2: 44, 3: 28, 4: 12}),
            "boost_cap": heat_cfg.get("boost_cap", {1: 25, 2: 30, 3: 35, 4: 40}),
        },
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
        ``config["app"]["timezone"]``, ``config["storage"]["cloud"]["bucket_name"]``, ``config["storage"]["resource"]["bucket_name"]``
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
        "notification": _load_notification_config(raw),
        "storage": _load_storage_config(raw),
        "postgresql": _load_postgresql_config(raw),
        "web": _load_web_config(raw),
        "analyzer": _load_analyzer_config(raw),
    }

    _print_config_sources(config)
    return config


def _print_config_sources(config: Dict) -> None:
    """Print key config sources (env var vs file)."""
    sources = []

    cloud = config["storage"]["cloud"]
    if cloud["endpoint_url"]:
        src = "env" if os.environ.get("CLOUD_S3_ENDPOINT_URL") else "file"
        sources.append(f"Cloud({src})")
    else:
        sources.append("Cloud(unconfigured)")

    resource = config["storage"]["resource"]
    if resource["endpoint_url"]:
        src = "env" if os.environ.get("RESOURCE_S3_ENDPOINT_URL") else "file"
        sources.append(f"Resource({src})")
    else:
        sources.append("Resource(unconfigured)")

    email = config["notification"]["email"]
    if email["from_addr"] and email["to_addr"]:
        src = "env" if os.environ.get("EMAIL_FROM_ADDR") else "file"
        sources.append(f"Email({src})")
    else:
        sources.append("Email(unconfigured)")

    print(f"Config sources: {', '.join(sources)}")
