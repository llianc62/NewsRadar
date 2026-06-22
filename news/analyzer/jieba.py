# coding=utf-8
"""JiebaAnalyzer — local offline analysis using jieba."""

import math
import os
from typing import Any, Dict, List, Optional

from news.analyzer.analyzer import Analyzer

# 词典文件默认路径
_DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "data")


def _load_dict(filepath: str) -> Dict[str, float]:
    """Load a word-weight dictionary file.

    Format: one entry per line — ``word  weight`` (space-separated).
    Lines starting with ``#`` are comments.
    """
    d = {}
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.rsplit(None, 1)  # word  weight
                if len(parts) == 2:
                    try:
                        d[parts[0]] = float(parts[1])
                    except ValueError:
                        pass
    except FileNotFoundError:
        pass
    return d


class JiebaAnalyzer(Analyzer):
    """基于 jieba 的本地离线分析器。

    负责：
    - heat_score: 热度分计算（从 PostgreSQL 迁移）
    - sentiment_score: 基于词典的情感分析
    """

    def __init__(self, config: dict, db=None):
        super().__init__(config, db)
        # 情感词典惰性加载
        self._positive_dict: Optional[Dict[str, float]] = None
        self._negative_dict: Optional[Dict[str, float]] = None
        self._negation_set: Optional[set] = None
        self._degree_dict: Optional[Dict[str, float]] = None

    # ── Heat score ─────────────────────────────────────────────────

    @staticmethod
    def _calc_heat_score(
        prev_heat: Optional[int],
        prev_ranks: list,       # [[7,20], [5,20]]
        new_ranks_entry: list,  # [rank, total] from current round
    ) -> int:
        """Calculate heat score, returns 0-100."""
        new_rank, new_total = new_ranks_entry
        if not prev_ranks or prev_heat is None:
            # First appearance: percentile
            return round(max(0, min(100, (1 - new_rank / new_total) * 100)))

        # Still on the list: incremental adjustment
        last_r, last_t = prev_ranks[-1]
        last_pct = (1 - last_r / last_t) * 100
        new_pct = (1 - new_rank / new_total) * 100
        delta = new_pct - last_pct  # percentage-point difference

        return round(max(0, min(100, prev_heat + delta * 0.3)))

    def analyze_heat(self, source_id: str, items: list, db_map: dict) -> None:
        """Process heat score for hotlist items of one source.

        db_map 格式: {url: {"heat_score": int, "ranks": [[int,int],...]}}
        """
        valid_items = [it for it in items if it.ranks]

        # ① Compare sets
        this_urls = {item.url for item in valid_items if item.url}
        db_urls = set(db_map.keys())

        new_urls = this_urls - db_urls
        existing_urls = this_urls & db_urls
        dropped_urls = db_urls - this_urls

        # ② First appearance — percentile
        for item in valid_items:
            if item.url in new_urls:
                r, t = item.ranks[0]
                item.heat_score = round(
                    max(0, min(100, (1 - r / t) * 100))
                )
                item.ranks = [[r, t]]

        # ③ Still on list — delta adjustment
        for item in valid_items:
            if item.url in existing_urls:
                prev = db_map[item.url]
                item.heat_score = self._calc_heat_score(
                    prev_heat=prev["heat_score"],
                    prev_ranks=prev["ranks"],
                    new_ranks_entry=item.ranks[0],
                )
                item.ranks = (prev["ranks"] or []) + [item.ranks[0]]

        # ④ Dropped from list — ×0.7 decay (requires DB write)
        if dropped_urls and self._db is not None:
            with self._db.get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """UPDATE news_articles
                           SET heat_score = CAST(
                               ROUND(GREATEST(0, LEAST(100,
                                   COALESCE(heat_score, 0) * 0.7
                               ))) AS INTEGER
                           )
                           WHERE source_id = %s
                             AND source_type = 'hotlist'
                             AND url = ANY(%s)""",
                        (source_id, list(dropped_urls)),
                    )
            print(
                f"[Analyzer] Heat decay: {len(dropped_urls)} URLs dropped"
                f" from {source_id}"
            )

    # ── Sentiment (Task 6 实现) ────────────────────────────────────

    def analyze_sentiment(self, items: list) -> None:
        """TODO: Task 6 实现。"""
        raise NotImplementedError("analyze_sentiment will be implemented in Task 6")
