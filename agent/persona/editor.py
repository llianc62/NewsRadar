"""新闻主编 - 多角色编排的聚合者。

Phase 1 各角色产出 :class:`PersonaSignal` 后，主编读取全部信号（经
``## 专业分析`` 块注入）综合成一份给用户的答复。主编本身是 :class:`PersonaAgent`，
``set_signals()`` 由 :class:`PersonaOrchestrator` 在 Phase 2 调用前写入。
"""

from __future__ import annotations

from .base import PersonaAgent


class EditorPersona(PersonaAgent):
    """新闻主编：综合各角色观点，产出平衡的编辑部立场。"""

    # 主编只做综合叙事，无需工具 -> DirectExecutor 真流式（避免 ReAct 假流式）
    prefer_direct_executor = True

    def __init__(self, config: dict, **kwargs):
        kwargs.setdefault("persona_name", "editor")
        kwargs.setdefault("kb_namespace", "")  # 主编不挂私有知识库
        super().__init__(config, **kwargs)
        self._signals: list = []

    def set_signals(self, signals) -> None:
        """注入 Phase 1 各角色信号（由 Orchestrator 调用）。"""
        self._signals = list(signals or [])

    def get_system_prompt(self) -> str:
        return """你是 NewsRadar 新闻主编，负责综合多位分析师与投资人的观点。

你会收到一条 ## 专业分析 块，列出各角色的信号（立场/信心/理由）。你的任务：
1. 提炼各方共识与分歧，不偏听单一角色；
2. 标注分歧最大处与最值得关注的风险；
3. 给出平衡的编辑部立场，而非简单多数表决。

用沉稳、克制的编辑部口吻回答用户。
最后用一行 JSON 总结：
{"stance":"看多"|"看空"|"中性", "confidence":0-100, "reasoning":"一句话理由"}"""

    def _pre_analyze(self, user_input: str) -> dict | None:
        if not self._signals:
            return None
        lines = []
        for sig in self._signals:
            name = getattr(sig, "display_name", "") or getattr(sig, "persona", "")
            stance = getattr(sig, "stance", "")
            conf = getattr(sig, "confidence", 0)
            reason = getattr(sig, "reasoning", "")
            lines.append(f"- {name}（{stance}，信心{conf}）：{reason}")
        return {"各角色信号": "\n".join(lines)}
