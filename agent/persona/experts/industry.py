"""行业研究员角色 - 关键词驱动的行业归类与竞争格局（专家视角）。

硬编码专业逻辑：复用 ``JiebaAnalyzer.analyze_keywords`` 提取关键词（TF-IDF/
TextRank），产出结构化事实（关键词列表），LLM 只用研究员声音叙事。
analyzer 由工厂注入。
"""

from __future__ import annotations

from ..base import PersonaAgent


class IndustryPersona(PersonaAgent):
    """行业研究员：关键词提取 -> 行业归类 -> 竞争格局解读。"""

    requires_analyzer = True

    def __init__(self, config: dict, *, analyzer=None, **kwargs):
        kwargs.setdefault("persona_name", "industry")
        kwargs.setdefault("kb_namespace", "industry-research")
        super().__init__(config, **kwargs)
        self._analyzer = analyzer

    def get_system_prompt(self) -> str:
        return """你是资深行业研究员，擅长从新闻文本快速定位所属行业与竞争格局。

你会收到一条 ## 专业分析 块，包含对用户输入文本提取的关键词。你的任务：
1. 据关键词判定所属行业/赛道；
2. 评估该新闻对行业竞争格局（集中度、准入、替代品）的影响；
3. 指出产业链上下游的受益/受损方。

用聚焦、行业内部人的口吻回答。
最后用一行 JSON 总结：
{"stance":"看多"|"看空"|"中性", "confidence":0-100, "reasoning":"一句话理由"}"""

    def _pre_analyze(self, user_input: str) -> dict | None:
        if not self._analyzer or not user_input.strip():
            return None
        item = {"title": "", "content": user_input, "tags": []}
        try:
            self._analyzer.analyze_keywords([item])
        except Exception:
            return None
        tags = item.get("tags") or []
        if not tags:
            return None
        return {"关键词": "、".join(tags[:8]), "text_sample": user_input[:80]}
