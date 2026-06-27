# coding=utf-8
"""Tests for run_notifier time-range filtering and ISO 8601 conversion."""

from unittest.mock import MagicMock, patch

import pytest

from news.notifier import _iso_to_db_format, run_notifier


class TestIsoToDbFormat:
    def test_converts_utc_z_suffix(self):
        assert _iso_to_db_format("2026-06-27T08:00:00Z") == "2026-06-27 08:00:00"

    def test_converts_with_offset(self):
        assert _iso_to_db_format("2026-06-27T08:00:00+00:00") == "2026-06-27 08:00:00"

    def test_converts_microseconds(self):
        assert _iso_to_db_format("2026-06-27T08:00:00.123456Z") == "2026-06-27 08:00:00"

    def test_empty_string_returns_none(self):
        assert _iso_to_db_format("") is None

    def test_none_returns_none(self):
        assert _iso_to_db_format(None) is None

    def test_invalid_returns_none(self):
        assert _iso_to_db_format("not-a-date") is None

    def test_unparseable_garbage_returns_none(self):
        assert _iso_to_db_format("abc123") is None


class TestRunNotifierTimeFiltering:
    """Integration-level: run_notifier passes start_time/end_time to db.get_all."""

    _PATCHES = [
        "storage.sqlite.Sqlite",
        "news.notifier.S3Client",
        "news.keywords.load_frequency_words",
        "news.keywords.match_and_group",
        "news.notifier.build_html_report",
        "news.notifier.save_html_report",
        "news.notifier.send_email",
        "utils.format_date_today",
        "utils.format_time_now",
    ]

    def _setup_mocks(self, **kwargs):
        """Return a list of patchers, each configured with **kwargs."""
        return [patch(p, **kwargs) if p in ("storage.sqlite.Sqlite",) else patch(p) for p in self._PATCHES]

    def _run_with(self, config, **run_kwargs):
        """Run notifier with mocked dependencies."""
        mock_db = MagicMock()
        mock_db.get_all.return_value = []

        patchers = {
            "storage.sqlite.Sqlite": patch("storage.sqlite.Sqlite", return_value=mock_db),
            "news.notifier.S3Client": patch("news.notifier.S3Client"),
            "news.keywords.load_frequency_words": patch("news.keywords.load_frequency_words", return_value=([], [], [])),
            "news.keywords.match_and_group": patch("news.keywords.match_and_group", return_value={}),
            "news.notifier.build_html_report": patch("news.notifier.build_html_report", return_value="<html></html>"),
            "news.notifier.save_html_report": patch("news.notifier.save_html_report"),
            "news.notifier.send_email": patch("news.notifier.send_email"),
            "utils.format_date_today": patch("utils.format_date_today", return_value="2026-06-27"),
            "utils.format_time_now": patch("utils.format_time_now", return_value="08:30"),
        }

        with patchers["storage.sqlite.Sqlite"] as mock_db_patched, \
             patchers["news.notifier.S3Client"] as mock_s3_cls, \
             patchers["news.keywords.load_frequency_words"], \
             patchers["news.keywords.match_and_group"], \
             patchers["news.notifier.build_html_report"], \
             patchers["news.notifier.save_html_report"], \
             patchers["news.notifier.send_email"], \
             patchers["utils.format_date_today"], \
             patchers["utils.format_time_now"], \
             patch("os.path.exists", return_value=False):

            mock_s3 = mock_s3_cls.init_by_config.return_value
            mock_s3.object_exists.return_value = False

            run_notifier(config, **run_kwargs)

        return mock_db

    def test_passes_start_time_to_db(self):
        """start_time is converted and passed to db.get_all."""
        config = {
            "app": {"timezone": "Asia/Shanghai"},
            "storage": {"local": {"data_dir": "/tmp"}, "cloud": {}},
            "notification": {"email": {}},
        }
        mock_db = self._run_with(config, start_time="2026-06-27T07:00:00Z")
        mock_db.get_all.assert_called_once_with(
            "2026-06-27", start_time="2026-06-27 07:00:00", end_time=None
        )

    def test_passes_end_time_to_db(self):
        config = {
            "app": {"timezone": "Asia/Shanghai"},
            "storage": {"local": {"data_dir": "/tmp"}, "cloud": {}},
            "notification": {"email": {}},
        }
        mock_db = self._run_with(config, end_time="2026-06-27T18:00:00Z")
        mock_db.get_all.assert_called_once_with(
            "2026-06-27", start_time=None, end_time="2026-06-27 18:00:00"
        )

    def test_no_time_params_calls_get_all_without_filters(self):
        """Backward-compatible: no start_time/end_time → get_all(date) with no filters."""
        config = {
            "app": {"timezone": "Asia/Shanghai"},
            "storage": {"local": {"data_dir": "/tmp"}, "cloud": {}},
            "notification": {"email": {}},
        }
        mock_db = self._run_with(config)
        mock_db.get_all.assert_called_once_with("2026-06-27", start_time=None, end_time=None)

    def test_invalid_start_time_falls_back_to_none(self):
        """Invalid ISO 8601 → log warning, pass None as start_time."""
        config = {
            "app": {"timezone": "Asia/Shanghai"},
            "storage": {"local": {"data_dir": "/tmp"}, "cloud": {}},
            "notification": {"email": {}},
        }
        mock_db = self._run_with(config, start_time="garbage")
        mock_db.get_all.assert_called_once_with("2026-06-27", start_time=None, end_time=None)
