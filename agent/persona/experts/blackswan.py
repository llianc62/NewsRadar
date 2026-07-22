"""黑天鹅风险视角角色 - 极端情绪异常检测（专家视角）。

硬编码专业逻辑：复用 ``JiebaAnalyzer.analyze_sentiment`` 取情感分，当情感处于
极端区（偏离中性 50 超过 30 分）时标记为情绪异常，提示尾部风险。LLM 只用风险
视角声音叙事。analyzer 由工厂注入。
"""

from __future__ import annotations

from ..base import PersonaAgent

# 极端情绪阈值：偏离中性 50 超过 30 分视为异常（狂热/恐慌）
_EXTREME_DEVIATION = 30


class BlackswanPersona(PersonaAgent):
    """黑天鹅风险视角：捕捉极端情绪与尾部风险信号。"""

    requires_analyzer = True

    def __init__(self, config: dict, *, analyzer=None, **kwargs):
        kwargs.setdefault("persona_name", "blackswan")
        kwargs.setdefault("kb_namespace", "risk/blackswan")
        super().__init__(config, **kwargs)
        self._analyzer = analyzer

    def get_system_prompt(self) -> str:
        return """你是黑天鹅风险分析师，专门关注被忽视的尾部风险与极端信号。

你会收到一条 ## 专业分析 块，包含对用户输入文本的情感分与是否处于极端区。
你的任务：
1. 判断当前情绪是否处于极端（狂热/恐慌），极端往往是反转前兆；
2. 搜索二阶效应与连锁反应路径（谁会先受损）；
3. 提醒“这次不一样”叙事的危险，以及被市场忽视的脆弱点。

用警觉、逆向的风险分析师口吻回答，宁可多想一层坏情景。
最后用一行 JSON 总结：
{"stance":"看多"|"看空"|"中性", "confidence":0-100, "reasoning":"一句话理由"}"""

    def _pre_analyze(self, user_input: str) -> dict | None:
        if not self._analyzer or not user_input.strip():
            return None
        item = {"title": "", "content": user_input}
        try:
            self._analyzer.analyze_sentiment([item])
        except Exception:
            return None
        score = item.get("sentiment_score", 50)
        deviation = abs(score - 50)
        is_extreme = deviation >= _EXTREME_DEVIATION
        zone = "极端恐慌" if score < 50 - _EXTREME_DEVIATION else (
            "极端狂热" if score > 50 + _EXTREME_DEVIATION else "正常区间"
        )
        return {
            "sentiment_score": score,
            "情绪区间": zone,
            "极端异常": "是" if is_extreme else "否",
            "text_sample": user_input[:80],
        }
