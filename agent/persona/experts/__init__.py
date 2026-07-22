"""新闻分析专家视角角色。"""

from .blackswan import BlackswanPersona
from .factcheck import FactcheckPersona
from .industry import IndustryPersona
from .macro import MacroPersona
from .sentiment import SentimentPersona

__all__ = [
    "BlackswanPersona",
    "FactcheckPersona",
    "IndustryPersona",
    "MacroPersona",
    "SentimentPersona",
]
