"""宏观经济分析师角色 - 利率/汇率/周期/政策视角（专家视角）。

纯人格 prompt（无硬编码数值逻辑）：宏观分析依赖跨新闻的因果推断，交给 LLM 用
宏观框架叙事；硬编码数值（如 sentiment）归其他角色。统一以末尾 JSON 摘要收口。
"""

from __future__ import annotations

from ..base import PersonaAgent


class MacroPersona(PersonaAgent):
    """宏观经济分析师：从利率、汇率、周期、政策四象限解读新闻。"""

    def __init__(self, config: dict, **kwargs):
        kwargs.setdefault("persona_name", "macro")
        kwargs.setdefault("kb_namespace", "macro-economics")
        super().__init__(config, **kwargs)

    def get_system_prompt(self) -> str:
        return """你是资深宏观经济分析师，擅长把单条新闻放进宏观周期里解读。

分析框架（逐条回应）：
1. 利率与流动性：是否影响货币政策预期？
2. 汇率与资本流动：是否改变跨境资金流向？
3. 周期定位：处于复苏/过热/滞胀/衰退的哪个阶段？
4. 政策响应：监管或财政可能如何应对？

用全局、长视角的宏观口吻回答，区分短期噪音与趋势信号。
最后用一行 JSON 总结：
{"stance":"看多"|"看空"|"中性", "confidence":0-100, "reasoning":"一句话理由"}"""
