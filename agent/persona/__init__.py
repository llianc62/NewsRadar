"""角色扮演 agent 子系统（Phase B/C）——已废弃。

此模块功能已被 AgentDefinition + AgentFactory 组合模式取代。
保留代码供参考，新代码不应使用。
请使用 agent/factory.py 的 AgentFactory 构建角色 Agent。
"""

from .base import PersonaAgent
from .editor import EditorPersona
from .manager import PersonaManager
from .orchestrator import PersonaOrchestrator, parse_signal
from .registry import (
    PERSONA_REGISTRY,
    PersonaSpec,
    get_persona_spec,
    list_personas,
)
from .signal import OrchestratorResult, PersonaSignal

__all__ = [
    "PersonaAgent",
    "PersonaManager",
    "PersonaOrchestrator",
    "PersonaSignal",
    "OrchestratorResult",
    "PersonaSpec",
    "EditorPersona",
    "PERSONA_REGISTRY",
    "list_personas",
    "get_persona_spec",
    "parse_signal",
]
