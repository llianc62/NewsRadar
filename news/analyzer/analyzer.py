# coding=utf-8
"""Analyzer abstract base class — heat score + sentiment."""

import math
from datetime import datetime, timezone
from typing import Optional

from abc import ABC, abstractmethod


class Analyzer(ABC):
    """分析器抽象基类。

    子类:
    - JiebaAnalyzer: 本地离线分析（heat_score + sentiment_score）
    - AgentAnalyzer: LLM 分析（未来，需 API key + config 开关）

    提供默认热度分实现（子类可覆写），情感分析保持抽象。
    """

    def __init__(self, config: dict, db=None):
        self._config = config
        self._db = db

        # 热度参数（带默认值，config 缺失时不崩溃）
        heat_cfg = config.get("analyzer", {}).get("heat", {})
        self._half_life_hours: float = float(heat_cfg.get("half_life_hours", 12))
        self._tier_base: dict = heat_cfg.get(
            "tier_base", {1: 60, 2: 44, 3: 28, 4: 12}
        )
        self._boost_cap: dict = heat_cfg.get(
            "boost_cap", {1: 25, 2: 30, 3: 35, 4: 40}
        )

    # ── Heat score (concrete, overridable) ──────────────────────────

    def analyze_heat(self, items: list) -> None:
        """计算热度分，原地修改 item['heat_score']。

        公式: heat = (tier_base + rank_boost) × time_decay

        - tier_base: 源层级基础分，从 config 读取
        - rank_boost: 排名加分 = (1 - rank/total) × boost_cap，无 ranks 则为 0
        - time_decay: 指数衰减 e^(-ln2 × age / half_life)
        - published_at 为空时取 age=0（满分新鲜度）
        """
        for item in items:
            tier = self._get_tier(item)
            base = self._tier_base.get(tier, self._tier_base.get(4, 12))
            boost = self._calc_rank_boost(item, tier)
            raw = base + boost
            published_at = (
                item.get("published_at", "")
                if isinstance(item, dict)
                else getattr(item, "published_at", "")
            )
            age_hours = self._age_hours(published_at)
            decay = self._time_decay(age_hours, self._half_life_hours)
            item["heat_score"] = round(raw * decay)

    @staticmethod
    def _get_tier(item) -> int:
        """从 item 提取 tier，缺省返回 4。兼容 dict/NewsItem。"""
        if isinstance(item, dict):
            return item.get("tier", 4)
        return getattr(item, "tier", 4)

    def _calc_rank_boost(self, item, tier: int) -> float:
        """计算排名加分 = (1 - rank/total) × boost_cap。

        无 ranks 返回 0（RSS 条目走这个分支）。
        """
        ranks = (
            item.get("ranks", []) if isinstance(item, dict)
            else getattr(item, "ranks", [])
        )
        if not ranks:
            return 0.0
        rank, total = ranks[0]
        if total <= 0:
            return 0.0
        cap = self._boost_cap.get(tier, self._boost_cap.get(4, 40))
        return (1 - rank / total) * cap

    @staticmethod
    def _time_decay(age_hours: float, half_life: float = 12.0) -> float:
        """指数衰减: e^(-ln2 × age / half_life)。age ≤ 0 返回 1.0。"""
        if age_hours <= 0:
            return 1.0
        return math.exp(-math.log(2) * age_hours / half_life)

    @staticmethod
    def _age_hours(published_at: str) -> float:
        """从 published_at 计算发表距今的小时数。空字符串返回 0.0。"""
        dt = Analyzer._parse_published_at(published_at)
        if dt is None:
            return 0.0
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        return (now - dt).total_seconds() / 3600.0

    @staticmethod
    def _parse_published_at(published_at: str) -> Optional[datetime]:
        """解析 ISO 8601 及常见日期格式。

        支持:
        - 2026-06-28T10:30:00+08:00 / 2026-06-28T10:30:00Z
        - 2026-06-28 / 2026-06-28 10:30:00
        解析失败返回 None。
        """
        if not published_at:
            return None
        try:
            return datetime.fromisoformat(published_at.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            pass
        for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(published_at, fmt)
            except ValueError:
                continue
        return None

    # ── Sentiment (abstract) ────────────────────────────────────────

    @abstractmethod
    def analyze_sentiment(self, items: list) -> None:
        """计算情感分。原地修改 item['sentiment_score']。

        items 为 dict 列表，每个 dict 需有 "title" 和 "content" 键。
        """
        ...
