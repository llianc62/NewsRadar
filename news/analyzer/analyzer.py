# coding=utf-8
"""Analyzer abstract base class."""

from abc import ABC, abstractmethod


class Analyzer(ABC):
    """分析器抽象基类。

    子类：
    - JiebaAnalyzer: 本地离线分析（heat_score + sentiment_score）
    - AgentAnalyzer: LLM 分析（未来，需 API key + config 开关）
    """

    def __init__(self, config: dict, db=None):
        self._config = config
        self._db = db

    @abstractmethod
    def analyze_heat(self, source_id: str, items: list, db_map: dict) -> None:
        """计算热度分。原地修改 item.heat_score 和 item.ranks。

        db_map 由调用方（Crawler）查询当天 DB 快照后传入，
        格式: {url: {"heat_score": int, "ranks": [[int,int],...]}}
        """
        ...

    @abstractmethod
    def analyze_sentiment(self, items: list) -> None:
        """计算情感分。原地修改 item.sentiment_score。

        items 为 dict 列表，每个 dict 需有 "title" 和 "content" 键。
        """
        ...
