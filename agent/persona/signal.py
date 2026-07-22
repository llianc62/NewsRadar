"""角色信号 - 各 PersonaAgent 产出的结构化观点（仿 ai-hedge-fund ``BenGrahamSignal``）。

每个角色 LLM 回复末尾附一行 JSON ``{"stance","confidence","reasoning"}``，
:class:`PersonaOrchestrator` 解析为 :class:`PersonaSignal` 供主编聚合 + 前端展示。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class PersonaSignal(BaseModel):
    """单个角色对一条消息的观点信号。"""

    persona: str = ""               # 角色名 "buffett"
    display_name: str = ""          # 展示名 "巴菲特"
    stance: str = ""                # 看多 | 看空 | 中性
    confidence: int = Field(default=0, ge=0, le=100)
    reasoning: str = ""             # 一句话理由
    raw: str = ""                   # 完整 LLM 回复（调试/展示用）


class OrchestratorResult(BaseModel):
    """编排结果：主编综合答复 + 各角色信号。"""

    reply: str = ""
    signals: list[PersonaSignal] = Field(default_factory=list)
