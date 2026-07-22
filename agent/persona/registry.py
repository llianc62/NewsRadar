"""PersonaRegistry - 角色注册中心（仿 ai-hedge-fund ANALYST_CONFIG）。

前端右侧团队面板从此 registry 渲染角色列表（名称 + description + category 分组）。
新增角色：在 ``_PERSONA_SPECS`` 加一条 ``PersonaSpec``。
"""

from __future__ import annotations

from dataclasses import dataclass

from .base import PersonaAgent
from .editor import EditorPersona
from .experts.blackswan import BlackswanPersona
from .experts.factcheck import FactcheckPersona
from .experts.industry import IndustryPersona
from .experts.macro import MacroPersona
from .experts.sentiment import SentimentPersona
from .investors.buffett import BuffettPersona
from .investors.graham import GrahamPersona
from .investors.taleb import TalebPersona
from .investors.wood import WoodPersona


@dataclass
class PersonaSpec:
    """角色规格。"""

    name: str            # "buffett"
    display_name: str    # "巴菲特"
    description: str     # "价值/护城河"
    category: str        # "investor" | "expert" | "editor"
    kb_namespace: str    # "investing/buffett"
    cls: type            # PersonaAgent 子类
    order: int = 0       # 面板排序


# 角色规格表（投资人 + 专家 + 主编，按 order 排序）
_PERSONA_SPECS: list[PersonaSpec] = [
    # ── 投资人视角 ──
    PersonaSpec(
        "buffett", "巴菲特", "价值/护城河/长期持有",
        "investor", "investing/buffett", BuffettPersona, order=10,
    ),
    PersonaSpec(
        "graham", "格雷厄姆", "深度价值/安全边际",
        "investor", "investing/graham", GrahamPersona, order=11,
    ),
    PersonaSpec(
        "taleb", "塔勒布", "反脆弱/黑天鹅/尾部",
        "investor", "investing/taleb", TalebPersona, order=12,
    ),
    PersonaSpec(
        "wood", "伍德", "颠覆式创新/指数增长",
        "investor", "investing/wood", WoodPersona, order=13,
    ),
    # ── 新闻分析专家视角 ──
    PersonaSpec(
        "macro", "宏观分析师", "利率/汇率/周期/政策",
        "expert", "macro-economics", MacroPersona, order=20,
    ),
    PersonaSpec(
        "sentiment", "舆情分析师", "市场情绪极端/转向",
        "expert", "market/sentiment", SentimentPersona, order=21,
    ),
    PersonaSpec(
        "industry", "行业研究员", "关键词驱动/竞争格局",
        "expert", "industry-research", IndustryPersona, order=22,
    ),
    PersonaSpec(
        "factcheck", "事实核查员", "主张拆解/证据强度",
        "expert", "factcheck", FactcheckPersona, order=23,
    ),
    PersonaSpec(
        "blackswan", "黑天鹅视角", "极端情绪/尾部风险",
        "expert", "risk/blackswan", BlackswanPersona, order=24,
    ),
    # ── 主编（聚合者，不出现在团队面板 selectable，但需注册） ──
    PersonaSpec(
        "editor", "新闻主编", "综合各角色观点",
        "editor", "", EditorPersona, order=99,
    ),
]

PERSONA_REGISTRY: dict[str, PersonaSpec] = {s.name: s for s in _PERSONA_SPECS}


def list_personas() -> list[PersonaSpec]:
    """返回按 order 排序的全部角色规格。"""
    return sorted(_PERSONA_SPECS, key=lambda s: s.order)


def get_persona_spec(name: str) -> PersonaSpec | None:
    """按 name 查角色规格，无则返回 None。"""
    return PERSONA_REGISTRY.get(name)
