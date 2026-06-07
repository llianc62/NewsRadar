# coding=utf-8
"""Shared constants used across the news, storage, and web packages.

Tier display labels and colours are the single source of truth for both
the email notifier (HTML report) and the web frontend (Jinja2 templates).

Source-type and sync-status strings are used by storage backends
(SQLite and PostgreSQL) to keep dedup logic and status tracking
consistent.
"""

# ── Tier display (used by notifier HTML report and web templates) ──

TIER_LABELS = {1: "T1·官媒", 2: "T2·主流", 3: "T3·垂直", 4: "T4·资讯"}

TIER_COLORS = {
    1: "#059669",
    2: "#2563eb",
    3: "#d97706",
    4: "#6b7280",
}

TIER_BG = {
    1: "#ecfdf5",
    2: "#eff6ff",
    3: "#fffbeb",
    4: "#f3f4f6",
}

# CSS-variable-based colours (used by web frontend for HSL theming)
TIER_COLORS_CSS = {
    1: "hsl(var(--danger))",
    2: "hsl(var(--warning))",
    3: "hsl(var(--success))",
    4: "hsl(var(--info))",
}

TIER_BG_CSS = {
    1: "hsl(var(--danger) / 0.1)",
    2: "hsl(var(--warning) / 0.1)",
    3: "hsl(var(--success) / 0.1)",
    4: "hsl(var(--info) / 0.1)",
}

# ── Source types ────────────────────────────────────────────────────

SOURCE_TYPE_HOTLIST = "hotlist"
SOURCE_TYPE_RSS = "rss"

# ── Sync status ─────────────────────────────────────────────────────

SYNC_STATUS_LOCAL = "local"
SYNC_STATUS_CLOUD = "cloud"

# ── Defaults ────────────────────────────────────────────────────────

DEFAULT_TIER = 4
DEFAULT_PRIORITY = 0
DEFAULT_TIMEZONE = "Asia/Shanghai"

# ── Sentiment thresholds (0-100 scale) ──────────────────────────────

SENTIMENT_POSITIVE_THRESHOLD = 67   # >= 67 = 利好
SENTIMENT_NEGATIVE_THRESHOLD = 33   # <= 33 = 利空
# 34-66 = 中性
