# coding=utf-8
"""
配置加载模块

负责从 YAML 配置文件和环境变量加载配置。
模式参考 ``trendradar/core/loader.py``：
- 每个配置段有独立的 ``_load_*_config()`` 函数
- 环境变量优先于配置文件（12-factor 风格）
- 类型安全的 ``_get_env_*()`` 辅助函数

Usage::

    from config import load_config

    config = load_config("config.yaml")
    timezone = config["app"]["timezone"]       # 环境变量已合并
    s3_cfg   = config["storage"]["remote"]      # 可传给 Storage
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Optional

import yaml


# =========================================================================
# 环境变量辅助函数（与 trendradar/core/loader.py 模式一致）
# =========================================================================

def _get_env_str(key: str, default: str = "") -> str:
    """从环境变量获取字符串值，未设置返回 default"""
    value = os.environ.get(key, "").strip()
    return value if value else default


def _get_env_int(key: str, default: int = 0) -> int:
    """从环境变量获取整数值，未设置或格式错误返回 default"""
    value = os.environ.get(key, "").strip()
    if not value:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _get_env_bool(key: str) -> Optional[bool]:
    """从环境变量获取布尔值，未设置返回 None"""
    value = os.environ.get(key, "").strip().lower()
    if not value:
        return None
    return value in ("true", "1", "yes")


# =========================================================================
# 各配置段加载函数
# =========================================================================

def _load_app_config(raw: Dict) -> Dict:
    """加载应用配置"""
    app = raw.get("app", {})
    return {
        "timezone": _get_env_str("NEWSNOW_TIMEZONE") or app.get("timezone", "Asia/Shanghai"),
    }


def _load_crawler_config(raw: Dict) -> Dict:
    """加载爬虫配置"""
    crawler = raw.get("crawler", {})
    return {
        "request_interval": _get_env_int("NEWSNOW_REQUEST_INTERVAL")
        or crawler.get("request_interval", 2000),
    }


def _load_platforms_config(raw: Dict) -> Dict:
    """加载平台配置"""
    platforms = raw.get("platforms", {})
    return {
        "enabled": platforms.get("enabled", True),
        "sources": platforms.get("sources", []),
    }


def _load_rss_config(raw: Dict) -> Dict:
    """加载 RSS 配置"""
    rss = raw.get("rss", {})
    env_enabled = _get_env_bool("NEWSNOW_RSS_ENABLED")
    return {
        "enabled": env_enabled if env_enabled is not None else rss.get("enabled", False),
        "request_interval": rss.get("request_interval", 1000),
        "timeout": rss.get("timeout", 20),
        "feeds": rss.get("feeds", []),
    }


def _load_notification_config(raw: Dict) -> Dict:
    """加载通知配置"""
    notification = raw.get("notification", {})
    email = notification.get("email", {})
    return {
        "frequency_words": notification.get("frequency_words", "frequency_words.txt"),
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
            # EMAIL_PASSWORD 兼容 GitHub Actions 已有的 secrets 命名
            "password": _get_env_str("EMAIL_PASSWORD")
            or _get_env_str("NEWSNOW_EMAIL_PASSWORD")
            or email.get("password", ""),
        },
    }


def _load_storage_config(raw: Dict) -> Dict:
    """加载存储配置"""
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


# =========================================================================
# 主加载函数
# =========================================================================

def load_config(path: str = "config.yaml") -> Dict[str, Any]:
    """加载配置文件，合并环境变量。

    环境变量优先级高于配置文件（12-factor 风格）。
    所有环境变量映射集中在本模块的 ``_load_*_config()`` 函数中。

    Returns:
        结构化的配置字典，可直接按路径访问：
        ``config["app"]["timezone"]``, ``config["storage"]["remote"]["bucket_name"]``
    """
    config_path = os.environ.get("CONFIG_PATH", path)

    if not Path(config_path).exists():
        raise FileNotFoundError(f"配置文件不存在: {config_path}")

    with open(config_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    print(f"配置文件加载成功: {config_path}")

    config = {
        "app": _load_app_config(raw),
        "crawler": _load_crawler_config(raw),
        "platforms": _load_platforms_config(raw),
        "rss": _load_rss_config(raw),
        "notification": _load_notification_config(raw),
        "storage": _load_storage_config(raw),
    }

    # 打印配置来源信息
    _print_config_sources(config)

    return config


def _print_config_sources(config: Dict) -> None:
    """打印关键配置来源信息（环境变量 vs 配置文件）"""
    sources = []

    # S3
    remote = config["storage"]["remote"]
    if remote["endpoint_url"]:
        src = "环境变量" if os.environ.get("S3_ENDPOINT_URL") or os.environ.get("NEWSNOW_S3_ENDPOINT_URL") else "配置文件"
        sources.append(f"S3存储({src})")
    else:
        sources.append("S3存储(未配置)")

    # Email
    email = config["notification"]["email"]
    if email["from_addr"] and email["to_addr"]:
        src = "环境变量" if os.environ.get("NEWSNOW_EMAIL_FROM_ADDR") else "配置文件"
        sources.append(f"邮件通知({src})")
    else:
        sources.append("邮件通知(未配置)")

    print(f"配置来源: {', '.join(sources)}")
