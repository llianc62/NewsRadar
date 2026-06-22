# coding=utf-8
"""AgentAnalyzer — LLM-based analysis (future)."""

from news.analyzer.analyzer import Analyzer


class AgentAnalyzer(Analyzer):
    """LLM 分析器（未来实现，需 API key + config 开关）。"""

    def __init__(self, config: dict, db=None):
        super().__init__(config, db)
        raise NotImplementedError("AgentAnalyzer 尚未实现")

    def analyze_heat(self, source_id: str, items: list, db_map: dict) -> None:
        raise NotImplementedError("AgentAnalyzer 尚未实现")

    def analyze_sentiment(self, items: list) -> None:
        raise NotImplementedError("AgentAnalyzer 尚未实现")
