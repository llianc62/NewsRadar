# coding=utf-8
"""Analyzer factory."""

from typing import Optional

from news.analyzer.analyzer import Analyzer


__all__ = ["create_analyzer", "Analyzer"]


def create_analyzer(config: dict, db=None) -> Optional[Analyzer]:
    """根据配置创建分析器。

    config.yaml:
        analyzer:
          enabled: true
          backend: jieba       # jieba | agent（未来）
    """
    analyzer_cfg = config.get("analyzer", {})
    if not analyzer_cfg.get("enabled", True):
        return None

    backend = analyzer_cfg.get("backend", "jieba")
    if backend == "agent":
        from .agent import AgentAnalyzer
        return AgentAnalyzer(config, db)

    from .jieba import JiebaAnalyzer
    return JiebaAnalyzer(config, db)
