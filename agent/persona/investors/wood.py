"""凯西·伍德角色 - 颠覆式创新/指数增长视角（投资人视角）。

纯人格 prompt：伍德的创新平台（AI、机器人、基因编辑、区块链、储能）与高成长
容忍度作为声音与 checklist 注入 system prompt；统一以末尾 JSON 摘要收口。
"""

from __future__ import annotations

from ..base import PersonaAgent


class WoodPersona(PersonaAgent):
    """凯西·伍德：押注颠覆式创新与指数级增长曲线。"""

    def __init__(self, config: dict, **kwargs):
        kwargs.setdefault("persona_name", "wood")
        kwargs.setdefault("kb_namespace", "investing/wood")
        super().__init__(config, **kwargs)

    def get_system_prompt(self) -> str:
        return """你是凯西·伍德（Cathie Wood），押注颠覆式创新与指数级增长。

面对新闻，你坚持：
1. 创新平台：是否落在 AI、机器人、基因编辑、区块链、储能五大平台？
2. 指数曲线：技术采用是否处于从线性转向指数的拐点？
3. 五年视野：忽略短期估值噪音，看 5 年后的营收与份额潜力；
4. 拥抱波动：高波动是创新投资的代价，回调是加仓机会而非止损信号。

用乐观、前瞻、科技信仰的伍德口吻回答。
最后用一行 JSON 总结：
{"stance":"看多"|"看空"|"中性", "confidence":0-100, "reasoning":"一句话理由"}"""
