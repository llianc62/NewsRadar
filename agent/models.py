from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Context:
    """单次 chat() 调用的共享上下文。"""

    # 输入
    user_input: str
    session_id: str = ""
    system_prompt: str = ""
    model_name: str = "default"
    running_mode: str = "normal"

    # 模块写入（由 Memory/Knowledge 在 on_before 中填充）
    memory_context: Any = None
    knowledge_context: Any = None

    # 执行过程（工具调用记录）
    history: list[dict] = field(default_factory=list)
    tool_calls: list[dict] = field(default_factory=list)
    tool_results: list[str] = field(default_factory=list)

    # 输出
    assistant_output: str = ""

    # 元数据
    step_count: int = 0
    model_used: str = ""
    total_tokens: int = 0


@dataclass
class AgentResult:
    """Agent 调用的返回结果。"""

    content: str
    model_used: str = ""
    total_tokens: int = 0
    tool_calls: list[dict] = field(default_factory=list)
    tool_results: list[str] = field(default_factory=list)
    step_count: int = 0


@dataclass
class AgentConfig:
    """DefaultAgent 组件配置——由 AgentFactory 构建后注入。"""

    brain: Any = None           # ModelHub
    executor: Any = None        # DirectExecutor | ReActExecutor
    memory: Any = None          # MemoryModule
    tools: Any = None           # ToolRegistry
    knowledge: Any = None       # KnowledgeEngine | None
    system_prompt: str = ""


@dataclass
class AgentDefinition:
    """角色定义——运行时创建 Agent 的全部信息。"""

    id: str                          # UUID
    name: str                        # 显示名称
    description: str = ""
    system_prompt: str = ""          # 角色提示词（大字段）
    tools: list[str] = field(default_factory=list)  # 工具名列表
    knowledge_id: str | None = None  # 关联知识库 UUID
    metadata: dict = field(default_factory=dict)
    created_at: str = ""             # ISO format
    updated_at: str = ""


@dataclass
class AgentKnowledge:
    """知识库定义——namespace 的实体层。"""

    id: str                          # UUID
    name: str                        # 显示名称
    description: str = ""
    namespace: str = ""              # 内部 namespace，如 "kb_<uuid>"
    created_at: str = ""
    updated_at: str = ""
