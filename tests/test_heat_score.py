# coding=utf-8
"""Tests for unified heat score calculation (tier-base × time-decay)."""

import math
from datetime import datetime, timedelta, timezone

import pytest

from news.analyzer.analyzer import Analyzer


# ── Concrete subclass for testing (Analyzer is ABC) ───────────────

class _TestAnalyzer(Analyzer):
    """Concrete analyzer — sentiment is a no-op, heat inherited from base."""

    def analyze_sentiment(self, items):
        pass


# ── Config helpers ─────────────────────────────────────────────────

def _cfg(**heat_overrides):
    """Build a minimal config dict for _TestAnalyzer with defaults overridden."""
    defaults = {
        "half_life_hours": 12,
        "tier_base": {1: 60, 2: 44, 3: 28, 4: 12},
        "boost_cap": {1: 25, 2: 30, 3: 35, 4: 40},
    }
    defaults.update(heat_overrides)
    return {"analyzer": {"heat": defaults}}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ago_iso(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


# ═══════════════════════════════════════════════════════════════════════
# _time_decay
# ═══════════════════════════════════════════════════════════════════════

class TestTimeDecay:
    """Unit tests for Analyzer._time_decay."""

    def test_zero_age_returns_one(self):
        assert Analyzer._time_decay(0.0, half_life=12) == 1.0

    def test_negative_age_returns_one(self):
        assert Analyzer._time_decay(-5.0, half_life=12) == 1.0

    def test_exactly_half_life(self):
        # e^(-ln2 * 12 / 12) = e^(-ln2) = 0.5
        assert Analyzer._time_decay(12.0, half_life=12) == pytest.approx(0.5)

    def test_two_half_lives(self):
        # e^(-ln2 * 24 / 12) = e^(-2*ln2) = 0.25
        assert Analyzer._time_decay(24.0, half_life=12) == pytest.approx(0.25)

    def test_default_half_life_is_12(self):
        assert Analyzer._time_decay(12.0) == pytest.approx(0.5)

    def test_custom_half_life(self):
        # e^(-ln2 * 6 / 6) = 0.5
        assert Analyzer._time_decay(6.0, half_life=6) == pytest.approx(0.5)


# ═══════════════════════════════════════════════════════════════════════
# _parse_published_at
# ═══════════════════════════════════════════════════════════════════════

class TestParsePublishedAt:
    """Unit tests for Analyzer._parse_published_at."""

    def test_empty_string(self):
        assert Analyzer._parse_published_at("") is None

    def test_iso_with_timezone_offset(self):
        dt = Analyzer._parse_published_at("2026-06-28T10:30:00+08:00")
        assert dt is not None
        assert dt.year == 2026
        assert dt.hour == 10

    def test_iso_with_z_suffix(self):
        dt = Analyzer._parse_published_at("2026-06-28T10:30:00Z")
        assert dt is not None
        assert dt.hour == 10

    def test_date_only(self):
        dt = Analyzer._parse_published_at("2026-06-28")
        assert dt is not None
        assert dt.year == 2026
        assert dt.month == 6
        assert dt.day == 28

    def test_datetime_no_tz(self):
        dt = Analyzer._parse_published_at("2026-06-28 10:30:00")
        assert dt is not None
        assert dt.hour == 10

    def test_datetime_with_T_no_tz(self):
        dt = Analyzer._parse_published_at("2026-06-28T10:30:00")
        assert dt is not None
        assert dt.hour == 10

    def test_unparseable_returns_none(self):
        assert Analyzer._parse_published_at("not a date") is None


# ═══════════════════════════════════════════════════════════════════════
# _age_hours
# ═══════════════════════════════════════════════════════════════════════

class TestAgeHours:
    """Unit tests for Analyzer._age_hours."""

    def test_empty_string_returns_zero(self):
        assert Analyzer._age_hours("") == 0.0

    def test_recent_date(self):
        one_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        assert Analyzer._age_hours(one_hour_ago) == pytest.approx(1.0, abs=0.2)

    def test_date_only_assumes_midnight_utc(self):
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        age = Analyzer._age_hours(today_str)
        assert 0 <= age <= 24


# ═══════════════════════════════════════════════════════════════════════
# analyze_heat
# ═══════════════════════════════════════════════════════════════════════

class TestAnalyzeHeat:
    """Integration tests for Analyzer.analyze_heat with dict items."""

    # ── Tier base scores ─────────────────────────────────────────────

    def test_tier1_fresh_hotlist_top1(self):
        """T1 #1/20, fresh → 60 + 23.75 = 83.75 → 84."""
        a = _TestAnalyzer(_cfg())
        item = {"tier": 1, "ranks": [[1, 20]], "published_at": _now_iso()}
        a.analyze_heat([item])
        # boost = (1 - 1/20) * 25 = 23.75, raw = 60 + 23.75 = 83.75 → 84
        assert item["heat_score"] == pytest.approx(84, abs=1)

    def test_tier1_fresh_hotlist_mid(self):
        """T1 #10/20, fresh → 60 + 12.5 = 72.5 → 72 (banker's rounding)."""
        a = _TestAnalyzer(_cfg())
        item = {"tier": 1, "ranks": [[10, 20]], "published_at": _now_iso()}
        a.analyze_heat([item])
        # boost = (1 - 10/20) * 25 = 12.5, raw = 72.5 → 72
        assert item["heat_score"] == pytest.approx(72, abs=1)

    def test_tier1_fresh_hotlist_last(self):
        """T1 #20/20, fresh → base only, no boost."""
        a = _TestAnalyzer(_cfg())
        item = {"tier": 1, "ranks": [[20, 20]], "published_at": _now_iso()}
        a.analyze_heat([item])
        assert item["heat_score"] == pytest.approx(60, abs=1)

    def test_tier2_fresh_hotlist_top1(self):
        """T2 #1/20, fresh → 44 + 28.5 = 72.5 → 72."""
        a = _TestAnalyzer(_cfg())
        item = {"tier": 2, "ranks": [[1, 20]], "published_at": _now_iso()}
        a.analyze_heat([item])
        assert item["heat_score"] == pytest.approx(72, abs=1)

    def test_tier3_fresh_hotlist_top1(self):
        """T3 #1/20, fresh → 28 + 33.25 = 61.25 → 61."""
        a = _TestAnalyzer(_cfg())
        item = {"tier": 3, "ranks": [[1, 20]], "published_at": _now_iso()}
        a.analyze_heat([item])
        assert item["heat_score"] == pytest.approx(61, abs=1)

    def test_tier4_fresh_hotlist_top1(self):
        """T4 #1/20, fresh → 12 + 38 = 50."""
        a = _TestAnalyzer(_cfg())
        item = {"tier": 4, "ranks": [[1, 20]], "published_at": _now_iso()}
        a.analyze_heat([item])
        assert item["heat_score"] == pytest.approx(50, abs=1)

    # ── RSS (no ranks) ──────────────────────────────────────────────

    def test_rss_tier1_fresh(self):
        """RSS T1 has no ranks → boost=0, pure tier_base."""
        a = _TestAnalyzer(_cfg())
        item = {"tier": 1, "ranks": [], "source_type": "rss", "published_at": _now_iso()}
        a.analyze_heat([item])
        assert item["heat_score"] == pytest.approx(60, abs=1)

    def test_rss_tier4_fresh(self):
        a = _TestAnalyzer(_cfg())
        item = {"tier": 4, "ranks": [], "source_type": "rss", "published_at": _now_iso()}
        a.analyze_heat([item])
        assert item["heat_score"] == pytest.approx(12, abs=1)

    def test_rss_no_ranks_key(self):
        """Item without 'ranks' key at all → treated as RSS, boost=0."""
        a = _TestAnalyzer(_cfg())
        item = {"tier": 1, "source_type": "rss", "published_at": _now_iso()}
        a.analyze_heat([item])
        assert item["heat_score"] == pytest.approx(60, abs=1)

    # ── Time decay ──────────────────────────────────────────────────

    def test_hotlist_6h_old(self):
        """T1 #1/20, 6h old → (60+23.75) × 0.707 ≈ 59."""
        a = _TestAnalyzer(_cfg())
        item = {"tier": 1, "ranks": [[1, 20]], "published_at": _ago_iso(6)}
        a.analyze_heat([item])
        # decay(6h, hl=12) = e^(-ln2*6/12) = e^(-0.5*ln2) = 0.7071
        # raw = 83.75, heat = 83.75 * 0.7071 ≈ 59.2 → 59
        assert item["heat_score"] == pytest.approx(59, abs=1)

    def test_rss_6h_old(self):
        """RSS T1, 6h old → 60 × 0.707 ≈ 42."""
        a = _TestAnalyzer(_cfg())
        item = {"tier": 1, "ranks": [], "published_at": _ago_iso(6)}
        a.analyze_heat([item])
        assert item["heat_score"] == pytest.approx(42, abs=1)

    def test_24h_old(self):
        """24h old → decay = 0.25."""
        a = _TestAnalyzer(_cfg())
        item = {"tier": 1, "ranks": [[1, 20]], "published_at": _ago_iso(24)}
        a.analyze_heat([item])
        # 83.75 * 0.25 = 20.9 → 21
        assert item["heat_score"] == pytest.approx(21, abs=1)

    # ── Empty published_at ──────────────────────────────────────────

    def test_empty_published_at_max_freshness(self):
        """Empty published_at → age=0 → decay=1.0."""
        a = _TestAnalyzer(_cfg())
        item = {"tier": 1, "ranks": [[1, 20]], "published_at": ""}
        a.analyze_heat([item])
        assert item["heat_score"] == pytest.approx(84, abs=1)

    def test_missing_published_at_key(self):
        """No published_at key → defaults to '' → age=0."""
        a = _TestAnalyzer(_cfg())
        item = {"tier": 1, "ranks": [[1, 20]]}
        a.analyze_heat([item])
        assert item["heat_score"] == pytest.approx(84, abs=1)

    # ── Missing tier ────────────────────────────────────────────────

    def test_missing_tier_defaults_to_4(self):
        a = _TestAnalyzer(_cfg())
        item = {"ranks": [[1, 20]], "published_at": _now_iso()}
        a.analyze_heat([item])
        # tier=4: base=12, boost=(1-1/20)*40=38, raw=50
        assert item["heat_score"] == pytest.approx(50, abs=1)

    # ── Batch processing ────────────────────────────────────────────

    def test_multiple_items_all_scored(self):
        a = _TestAnalyzer(_cfg())
        now = _now_iso()
        items = [
            {"tier": 1, "ranks": [[1, 20]], "published_at": now},
            {"tier": 4, "ranks": [], "published_at": now},
        ]
        a.analyze_heat(items)
        assert items[0]["heat_score"] == pytest.approx(84, abs=1)
        assert items[1]["heat_score"] == pytest.approx(12, abs=1)

    # ── Config defaults ─────────────────────────────────────────────

    def test_empty_config_uses_defaults(self):
        """No heat config at all → defaults still work."""
        a = _TestAnalyzer({"analyzer": {}})
        item = {"tier": 1, "ranks": [[1, 20]], "published_at": _now_iso()}
        a.analyze_heat([item])
        assert item["heat_score"] == pytest.approx(84, abs=1)

    def test_custom_half_life(self):
        """Custom half_life=3 → faster decay."""
        a = _TestAnalyzer({"analyzer": {"heat": {"half_life_hours": 3}}})
        # 3h old, hl=3 → decay = e^(-ln2*3/3) = 0.5
        item = {"tier": 1, "ranks": [[1, 20]], "published_at": _ago_iso(3)}
        a.analyze_heat([item])
        # raw = 60 + 23.75 = 83.75, heat = 83.75 * 0.5 ≈ 42
        assert item["heat_score"] == pytest.approx(42, abs=1)

    # ── Edge cases ──────────────────────────────────────────────────

    def test_total_zero_in_ranks(self):
        """rank[0][1] = 0 → no boost (avoid division by zero)."""
        a = _TestAnalyzer(_cfg())
        item = {"tier": 1, "ranks": [[5, 0]], "published_at": _now_iso()}
        a.analyze_heat([item])
        assert item["heat_score"] == pytest.approx(60, abs=1)

    def test_tier_greater_than_4(self):
        """Unknown tier → fallback to tier 4 defaults."""
        a = _TestAnalyzer(_cfg())
        item = {"tier": 99, "ranks": [[1, 20]], "published_at": _now_iso()}
        a.analyze_heat([item])
        assert item["heat_score"] == pytest.approx(50, abs=1)
