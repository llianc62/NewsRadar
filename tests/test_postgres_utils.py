"""Tests for module-level utilities in storage/postgres.py."""
import pytest
from storage.postgres import _load_schema, _to_timestamptz, _contains_cjk


class TestLoadSchema:
    def test_loads_schema_file(self):
        """postgres.sql 应能读取并包含 CREATE TABLE 语句。"""
        sql = _load_schema()
        assert "CREATE TABLE" in sql
        assert "news_articles" in sql

    def test_raises_when_file_missing(self, monkeypatch):
        """文件不存在时抛 FileNotFoundError。"""
        import storage.postgres as mod
        monkeypatch.setattr(mod.Path, "exists", lambda self: False)
        monkeypatch.setattr(mod.Path, "read_text",
                            lambda self, **kw: (_ for _ in ()).throw(FileNotFoundError()))
        with pytest.raises(FileNotFoundError):
            _load_schema()


class TestToTimestamptz:
    def test_iso8601_full(self):
        """完整 ISO 8601 时间字符串。"""
        from datetime import datetime, timezone, timedelta
        result = _to_timestamptz("2026-06-21T10:30:00+08:00", None)
        assert result == datetime(2026, 6, 21, 10, 30,
                                  tzinfo=timezone(timedelta(hours=8)))

    def test_utc_z_suffix(self):
        """UTC Z 后缀。"""
        from datetime import datetime, timezone
        result = _to_timestamptz("2026-06-21T02:30:00Z", None)
        assert result == datetime(2026, 6, 21, 2, 30, tzinfo=timezone.utc)

    def test_hhmm_with_fallback(self):
        """HH:MM 格式 + fallback_date。"""
        from datetime import datetime, timezone, timedelta
        tz = timezone(timedelta(hours=8))
        result = _to_timestamptz("10:30", "2026-06-21")
        assert result == datetime(2026, 6, 21, 10, 30, tzinfo=tz)

    def test_empty_string(self):
        assert _to_timestamptz("", None) is None

    def test_none(self):
        assert _to_timestamptz(None, None) is None

    def test_invalid_format(self):
        """无效格式返回 None。"""
        assert _to_timestamptz("not-a-time", None) is None

    def test_hhmm_without_fallback(self):
        """HH:MM 格式无 fallback_date 时返回 None。"""
        assert _to_timestamptz("10:30", None) is None

    def test_invalid_hour_value(self):
        """非法小时值不抛异常，返回 None。"""
        assert _to_timestamptz("99:99", "2026-06-21") is None


class TestContainsCjk:
    def test_pure_chinese(self):
        assert _contains_cjk("英伟达") is True

    def test_mixed_cjk_ascii(self):
        assert _contains_cjk("NVIDIA 英伟达 GPU") is True

    def test_japanese_kanji(self):
        assert _contains_cjk("日本経済") is True

    def test_pure_ascii(self):
        assert _contains_cjk("NVIDIA") is False

    def test_numbers_and_symbols(self):
        assert _contains_cjk("GPT-4") is False

    def test_empty_string(self):
        assert _contains_cjk("") is False

    def test_chinese_punctuation_only(self):
        """中文标点（，。）不在 CJK 字符范围，返回 False。"""
        assert _contains_cjk("，。！") is False
