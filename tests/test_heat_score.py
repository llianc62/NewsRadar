# coding=utf-8
"""Tests for heat score calculation and hotlist heat processing."""

import json

import pytest

from storage.postgres import PostgreSQL


class TestCalcHeatScore:
    """Unit tests for _calc_heat_score."""

    # ── First appearance ────────────────────────────────────────────

    def test_first_appearance_top_rank(self):
        """#1/20 → 95."""
        score = PostgreSQL._calc_heat_score(None, [], [1, 20])
        assert score == 95

    def test_first_appearance_mid_rank(self):
        """#7/20 → 65."""
        score = PostgreSQL._calc_heat_score(None, [], [7, 20])
        assert score == 65

    def test_first_appearance_bottom_rank(self):
        """#20/20 → 0."""
        score = PostgreSQL._calc_heat_score(None, [], [20, 20])
        assert score == 0

    def test_first_appearance_50_total_top(self):
        """#1/50 → 98."""
        score = PostgreSQL._calc_heat_score(None, [], [1, 50])
        assert score == 98

    def test_first_appearance_prev_heat_none(self):
        """prev_heat=None with valid ranks still counts as first."""
        score = PostgreSQL._calc_heat_score(None, [[7, 20]], [5, 20])
        assert score == 75

    # ── Still on list: rank up ──────────────────────────────────────

    def test_rank_up_increases_heat(self):
        """#7/20 → #5/20: 65% → 75%, +10pp → +3 heat."""
        prev_ranks = [[7, 20]]
        score = PostgreSQL._calc_heat_score(65, prev_ranks, [5, 20])
        assert score == 68  # 65 + round(10 * 0.3) = 68

    def test_rank_up_big_jump(self):
        """#20/20 → #1/20: 0% → 95%, +95pp → +28 heat."""
        prev_ranks = [[20, 20]]
        score = PostgreSQL._calc_heat_score(0, prev_ranks, [1, 20])
        assert score == 28  # 0 + round(95 * 0.3) = 28

    def test_rank_up_multi_round(self):
        """Accumulates across multiple rounds."""
        prev_ranks = [[7, 20], [5, 20]]  # last is [5,20]
        # prev_heat=68, #5→#2: 75%→90%, delta=15, 68+15*0.3=72.5→72
        score = PostgreSQL._calc_heat_score(68, prev_ranks, [2, 20])
        assert score == 72

    # ── Still on list: rank down ────────────────────────────────────

    def test_rank_down_decreases_heat(self):
        """#5/20 → #8/20: 75% → 60%, -15pp → -4.5 → -4 heat."""
        prev_ranks = [[5, 20]]
        score = PostgreSQL._calc_heat_score(75, prev_ranks, [8, 20])
        assert score == 70  # 75 + round(-15 * 0.3) = 75 - 4 = 71? No.
        # 75 - 4.5 = 70.5. Python round(70.5) = 70 (banker's rounding)

    def test_rank_down_severe(self):
        """#1/20 → #15/20: 95% → 25%, -70pp → -21 heat."""
        prev_ranks = [[1, 20]]
        score = PostgreSQL._calc_heat_score(95, prev_ranks, [15, 20])
        assert score == 74  # 95 + round(-70 * 0.3) = 74

    # ── Still on list: no change ────────────────────────────────────

    def test_same_rank_no_change(self):
        """Same rank → heat unchanged."""
        prev_ranks = [[5, 20]]
        score = PostgreSQL._calc_heat_score(75, prev_ranks, [5, 20])
        assert score == 75

    # ── Different total sizes ────────────────────────────────────────

    def test_total_changes_between_rounds(self):
        """#5/20 (75%) → #5/50 (90%): ranking stronger in larger pool."""
        prev_ranks = [[5, 20]]
        score = PostgreSQL._calc_heat_score(75, prev_ranks, [5, 50])
        assert score == 80  # 75 + round(15 * 0.3) = 80

    # ── Clamp boundaries ────────────────────────────────────────────

    def test_clamp_to_100(self):
        """Heat cannot exceed 100."""
        prev_ranks = [[5, 20]]
        score = PostgreSQL._calc_heat_score(99, prev_ranks, [1, 20])
        assert score == 100  # 99 + 6 = 105 → clamped to 100

    def test_clamp_to_0(self):
        """Heat cannot go below 0."""
        prev_ranks = [[10, 20]]
        score = PostgreSQL._calc_heat_score(1, prev_ranks, [20, 20])
        assert score == 0

    # ── Rounding ────────────────────────────────────────────────────

    def test_rounding(self):
        """delta × 0.3 = 1.5, Python round() uses banker's rounding."""
        prev_ranks = [[7, 20]]
        # 65% → 70%: delta=5, 5*0.3=1.5, 65+1.5=66.5
        # Python round(66.5)=66 (banker's rounding: ties to even)
        score = PostgreSQL._calc_heat_score(65, prev_ranks, [6, 20])
        assert score == 66


class TestProcessHotlistHeat:
    """Tests for _process_hotlist_heat using mocked DB."""

    @staticmethod
    def _make_db_row(url, heat_score, ranks):
        """Helper: build a RealDictRow-compatible return for fetchall."""
        return {"url": url, "heat_score": heat_score, "ranks": ranks}

    # ── Helper ──────────────────────────────────────────────────────

    @staticmethod
    def _make_item(title, source_id, url, rank, total, heat_score=0):
        """Build a NewsItem with ranks derived from rank + total."""
        from news.models import NewsItem
        return NewsItem(
            title=title,
            source_id=source_id,
            source_type="hotlist",
            url=url,
            rank=rank,
            ranks=[[rank, total]],
            heat_score=heat_score,
        )

    def test_new_url_first_appearance(self, db, mock_cursor):
        """URL not in DB → percentile-based score."""
        mock_cursor.fetchall.return_value = []  # DB has no records

        items = [self._make_item("Brand new", "test-source",
                                 "https://a.com/new1", rank=3, total=20)]
        db._process_hotlist_heat("test-source", items)

        assert items[0].heat_score == 85  # (1 - 3/20) * 100 = 85
        assert items[0].ranks == [[3, 20]]

    def test_existing_url_rank_up(self, db, mock_cursor):
        """URL in DB → delta adjustment from old heat."""
        mock_cursor.fetchall.return_value = [
            self._make_db_row("https://a.com/news1", 65, [[7, 20]]),
        ]

        items = [self._make_item("Existing news #1", "test-source",
                                 "https://a.com/news1", rank=5, total=20)]
        db._process_hotlist_heat("test-source", items)

        # 65% → 75%, delta=10, +3 → 68
        assert items[0].heat_score == 68
        assert items[0].ranks == [[7, 20], [5, 20]]

    def test_existing_url_rank_down(self, db, mock_cursor):
        """URL in DB → delta adjustment (negative)."""
        mock_cursor.fetchall.return_value = [
            self._make_db_row("https://a.com/news1", 65, [[7, 20]]),
        ]

        items = [self._make_item("Existing news #1", "test-source",
                                 "https://a.com/news1", rank=12, total=20)]
        db._process_hotlist_heat("test-source", items)

        # 65% → 40%, delta=-25, 65-7.5=57.5 → 58 (banker's rounding)
        assert items[0].heat_score == 58
        assert items[0].ranks == [[7, 20], [12, 20]]

    def test_dropped_url_decay(self, db, mock_cursor):
        """URL in DB but not this round → ×0.7."""
        mock_cursor.fetchall.return_value = [
            self._make_db_row("https://a.com/old1", 65, [[7, 20]]),
            self._make_db_row("https://a.com/old2", 50, [[10, 20]]),
        ]

        items: list = []  # Empty — no URLs in this round
        db._process_hotlist_heat("test-source", items)

        # Verify decay UPDATE was called
        from tests.conftest_db import capture_sql
        sql, params = capture_sql(mock_cursor)
        assert "SET heat_score" in sql
        assert "0.7" in sql
        assert "test-source" in params
        assert set(params[1]) == {"https://a.com/old1", "https://a.com/old2"}

    def test_mixed_scenario(self, db, mock_cursor):
        """New + existing + dropped all in one round."""
        mock_cursor.fetchall.return_value = [
            self._make_db_row("https://a.com/news1", 65, [[7, 20]]),
            self._make_db_row("https://a.com/news2", 50, [[10, 20]]),
        ]

        items = [
            self._make_item("Existing news #1", "test-source",
                            "https://a.com/news1", rank=3, total=20),
            self._make_item("Brand new", "test-source",
                            "https://a.com/new1", rank=1, total=20),
            # Note: news2 is NOT in items → it's dropped
        ]
        db._process_hotlist_heat("test-source", items)

        # Existing: 65% → 85%, delta=20, +6 → 71
        assert items[0].heat_score == 71
        assert items[0].ranks == [[7, 20], [3, 20]]

        # New: (1 - 1/20) * 100 = 95
        assert items[1].heat_score == 95
        assert items[1].ranks == [[1, 20]]

        # Decay UPDATE was called for news2
        from tests.conftest_db import capture_sql
        sql, params = capture_sql(mock_cursor)
        assert "0.7" in sql
        assert "https://a.com/news2" in params[1]

    def test_other_source_not_affected(self, db, mock_cursor):
        """Processing one source only queries that source."""
        mock_cursor.fetchall.return_value = []

        items = [self._make_item("Other source news", "other-source",
                                 "https://b.com/o1", rank=5, total=20)]
        db._process_hotlist_heat("other-source", items)

        assert items[0].heat_score == 75

    def test_cross_day_data_not_included(self, db, mock_cursor):
        """Yesterday's data is excluded by WHERE crawled_at::date = CURRENT_DATE."""
        mock_cursor.fetchall.return_value = []  # yesterday's data excluded

        items = [self._make_item("Today's news", "test-source",
                                 "https://a.com/today", rank=1, total=20)]
        db._process_hotlist_heat("test-source", items)

        # Should be first appearance (not matching yesterday's data)
        assert items[0].heat_score == 95
        assert items[0].ranks == [[1, 20]]

    def test_null_heat_score_treated_as_first(self, db, mock_cursor):
        """Records with heat_score=NULL → treated as first appearance."""
        mock_cursor.fetchall.return_value = [
            self._make_db_row("https://a.com/news1", None, [[7, 20]]),
        ]

        items = [self._make_item("News with null heat", "test-source",
                                 "https://a.com/news1", rank=5, total=20)]
        db._process_hotlist_heat("test-source", items)

        # prev_heat=None → falls into first-appearance branch
        assert items[0].heat_score == 75
        assert items[0].ranks == [[7, 20], [5, 20]]

    def test_empty_ranks_treated_as_first(self, db, mock_cursor):
        """Records with empty ranks → treated as first appearance."""
        mock_cursor.fetchall.return_value = [
            self._make_db_row("https://a.com/news1", 50, []),
        ]

        items = [self._make_item("News with empty ranks", "test-source",
                                 "https://a.com/news1", rank=3, total=20)]
        db._process_hotlist_heat("test-source", items)

        # prev_ranks=[] → falls into first-appearance branch
        assert items[0].heat_score == 85

    def test_item_with_heat_score_no_ranks_skipped(self, db, mock_cursor):
        """Item with heat_score but no ranks (synced data) → skipped, keeps heat_score."""
        from news.models import NewsItem

        mock_cursor.fetchall.return_value = []

        items = [
            NewsItem(title="Synced item", source_id="test-source",
                     source_type="hotlist", url="https://a.com/synced",
                     rank=0, heat_score=85),
            # ranks=[] (default), so it should be skipped
        ]
        db._process_hotlist_heat("test-source", items)

        # Skipped by valid_items filter (no ranks) → keeps snapshot heat_score
        assert items[0].heat_score == 85
        assert items[0].ranks == []
