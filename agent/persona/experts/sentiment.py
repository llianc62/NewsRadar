"""舆情/情感分析师角色 - 市场情绪极端与转向（专家视角）。

仿 ai-hedge-fund v1 ``ben_graham.py``：硬编码专业逻辑（JiebaAnalyzer
情感分）产出结构化事实，LLM 只用分析师声音叙事。analyzer 由工厂注入。
"""

from __future__ import annotations

from ..base import PersonaAgent

# 与 news/constants.py 的情感阈值一致：>=67 利好，<=33 利空，34-66 中性
_POSITIVE_THRESHOLD = 67
_NEGATIVE_THRESHOLD = 33


class SentimentPersona(PersonaAgent):
    """舆情分析师：硬编码情感分 + 人格叙事。"""

    requires_analyzer = True

    def __init__(self, config: dict, *, analyzer=None, **kwargs):
        kwargs.setdefault("persona_name", "sentiment")
        kwargs.setdefault("kb_namespace", "market/sentiment")
        super().__init__(config, **kwargs)
        self._analyzer = analyzer  # JiebaAnalyzer | None

    def get_system_prompt(self) -> str:
        return """你是资深市场舆情分析师，擅长捕捉市场情绪的极端与转向信号。

你会收到一条 ## 专业分析 块，包含对用户输入文本的客观情感分（0-100：
>=67 偏利好，<=33 偏利空，34-66 中性）。你的任务：
1. 解读该分数反映的情绪温度；
2. 判断是否处于极端（狂热/恐慌）或转向拐点；
3. 提醒情绪与基本面的背离风险。

用冷静、数据驱动的分析师口吻回答。
最后用一行 JSON 总结：
{"stance":"看多"|"看空"|"中性", "confidence":0-100, "reasoning":"一句话理由"}"""

    def _pre_analyze(self, user_input: str) -> dict | None:
        if not self._analyzer or not user_input.strip():
            return None
        # analyze_sentiment 原地写 sentiment_score，兼容 dict
        item = {"title": "", "content": user_input}
        try:
            self._analyzer.analyze_sentiment([item])
        except Exception:
            return None
        score = item.get("sentiment_score", 50)
        if score >= _POSITIVE_THRESHOLD:
            label = "利好"
        elif score <= _NEGATIVE_THRESHOLD:
            label = "利空"
        else:
            label = "中性"
        return {
            "sentiment_score": score,
            "label": label,
            "text_sample": user_input[:80],
        }
