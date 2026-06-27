# coding=utf-8
"""Tests for :mod:`news.notifier_template`.

Covers template loading and Jinja2 rendering with empty and realistic
inputs.
"""

import pytest

from news.constants import TIER_BG, TIER_COLORS, TIER_LABELS
from news.notifier import (
    build_html_report,
    load_template,
    render_template,
)

_TIER_CTX = {
    "TIER_LABELS": TIER_LABELS,
    "TIER_COLORS": TIER_COLORS,
    "TIER_BG": TIER_BG,
}


class TestLoadTemplate:
    """Template file loading."""

    def test_loads_email_report_template(self):
        raw = load_template("email_report.html")
        assert len(raw) > 3000
        assert "<!DOCTYPE html>" in raw
        assert "新闻速报" in raw

    def test_missing_template_raises(self):
        with pytest.raises(FileNotFoundError):
            load_template("nonexistent.html")


class TestRenderTemplate:
    """Jinja2 rendering."""

    def test_renders_empty_groups(self):
        html = render_template(
            "email_report.html",
            date="2026-06-27",
            time_str="08:30",
            total_count=0,
            grouped_items={},
            **_TIER_CTX,
        )
        assert "📰 新闻速报" in html
        assert "2026-06-27" in html
        assert "08:30" in html
        assert "0 条" in html

    def test_filters_unmatched_items(self):
        """build_html_report excludes __unmatched__ group."""
        grouped = {
            "科技": [
                {"title": "匹配新闻", "source_name": "来源",
                 "source_type": "hotlist", "url": "", "rank": "",
                 "summary": "", "tier": 3, "heat_score": 50},
            ],
            "__unmatched__": [
                {"title": "未匹配新闻", "source_name": "未知来源",
                 "source_type": "hotlist", "url": "", "rank": "",
                 "summary": "", "tier": 4, "heat_score": 30},
            ],
        }
        html = build_html_report(grouped, "2026-06-27", "08:30", 2)
        assert "匹配新闻" in html
        assert "未匹配新闻" not in html
        assert "__unmatched__" not in html

    def test_tier_color_defaults(self):
        """Items with missing or out-of-range tier get T4 defaults."""
        grouped = {
            "test": [
                {"title": "X", "source_name": "", "source_type": "hotlist",
                 "url": "", "rank": "", "summary": "", "tier": 99,
                 "heat_score": None},
            ],
        }
        html = render_template(
            "email_report.html",
            date="2026-06-27", time_str="08:30",
            total_count=1, grouped_items=grouped,
            **_TIER_CTX,
        )
        assert "T4·资讯" in html
        assert "#6b7280" in html  # Default tier color

    def test_rank_badge_colors(self):
        """Top-3 red, top-10 orange, rest gray."""
        grouped = {
            "test": [
                {"title": f"News {r}", "source_name": "S",
                 "source_type": "hotlist", "url": "", "rank": str(r),
                 "summary": "", "tier": 3, "heat_score": 50}
                for r in [1, 5, 15]
            ],
        }
        html = render_template(
            "email_report.html",
            date="2026-06-27", time_str="08:30",
            total_count=3, grouped_items=grouped,
            **_TIER_CTX,
        )
        assert "#dc2626" in html  # Red for rank 1
        assert "#ea580c" in html  # Orange for rank 5

    def test_heat_color_thresholds(self):
        """>=80 red, >=60 orange, <60 gray."""
        grouped = {
            "test": [
                {"title": f"Heat {h}", "source_name": "S",
                 "source_type": "hotlist", "url": "", "rank": "",
                 "summary": "", "tier": 3, "heat_score": h}
                for h in [90, 70, 30]
            ],
        }
        html = render_template(
            "email_report.html",
            date="2026-06-27", time_str="08:30",
            total_count=3, grouped_items=grouped,
            **_TIER_CTX,
        )
        assert "#dc2626" in html  # Red for heat 90
        assert "#ea580c" in html  # Orange for heat 70

    def test_rss_badge_replaces_rank(self):
        """RSS items show RSS badge instead of rank badge."""
        grouped = {
            "test": [
                {"title": "RSS News", "source_name": "Blog",
                 "source_type": "rss", "url": "", "rank": "1",
                 "summary": "", "tier": 3, "heat_score": 60},
            ],
        }
        html = render_template(
            "email_report.html",
            date="2026-06-27", time_str="08:30",
            total_count=1, grouped_items=grouped,
            **_TIER_CTX,
        )
        assert "RSS" in html
        # Rank badge should NOT appear (RSS overrides it)
        assert ">#1<" not in html

    def test_summary_truncated(self):
        """Summary should appear and be truncated to 200 chars."""
        long_summary = "x" * 300
        grouped = {
            "test": [
                {"title": "T", "source_name": "S",
                 "source_type": "hotlist", "url": "", "rank": "",
                 "summary": long_summary, "tier": 3, "heat_score": 50},
            ],
        }
        html = render_template(
            "email_report.html",
            date="2026-06-27", time_str="08:30",
            total_count=1, grouped_items=grouped,
            **_TIER_CTX,
        )
        assert long_summary[:200] in html
        assert long_summary[:201] not in html  # Truncated

    def test_no_url_renders_as_span(self):
        """Missing URL renders title as <span> not <a>."""
        grouped = {
            "test": [
                {"title": "No Link", "source_name": "S",
                 "source_type": "hotlist", "url": "", "rank": "",
                 "summary": "", "tier": 4, "heat_score": None},
            ],
        }
        html = render_template(
            "email_report.html",
            date="2026-06-27", time_str="08:30",
            total_count=1, grouped_items=grouped,
            **_TIER_CTX,
        )
        assert "No Link" in html
        # Should use span, not anchor
        assert '<span style="color:#1a1a1a' in html
