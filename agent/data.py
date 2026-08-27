"""Agent 数据模型 — Message、Context、AgentResult 等核心类型。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Message:
    """统一消息类型,覆盖 system/user/assistant/tool 四种角色。"""
    role: str  # "system" | "user" | "assistant" | "tool"
    content: str | None = None
    # assistant 专用
    tool_calls: list[dict] | None = None
    usage: dict | None = None
    reasoning_content: str | None = None
    model_used: str | None = None                # 新增:模型版本
    # tool 专用
    name: str | None = None
    tool_call_id: str | None = None
    tool_result: "ToolResult | None" = None      # 新增:执行详情(归 tool 消息)
    # 通用
    timestamp: float = 0.0

    def __post_init__(self):
        if self.timestamp == 0.0:
            self.timestamp = time.time()


@dataclass
class MemoryBlock:
    """可注入 prompt 的背景块 -- 长期记忆/知识等。"""
    title: str
    content: str
    source: str = ""    # "memory" / "knowledge"
    order: int = 0


@dataclass
class ToolResult:
    """单次工具执行的结构化记录 -- 归 tool 消息的 tool_result 字段(不存 ctx)。"""
    name: str
    args: dict
    result: str = ""
    error: str = ""
    timing_ms: int = 0
    retries: int = 0
    success: bool = True
    tool_call_id: str = ""


@dataclass
class Context:
    """单次 agent 调用的共享上下文 -- 输入区 + 执行区。"""

    # ── 输入区 ─────────────────────────────────
    user_input: str = ""
    session_id: str = ""
    system_prompt: str = ""
    model_name: str = "default"
    running_mode: str = "normal"
    memories: list[MemoryBlock] = field(default_factory=list)

    # ── 执行区 ─────────────────────────────────
    messages: list[Message] = field(default_factory=list)
    step_count: int = 0

    # ── token 状态 ─────────────────────────────
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    max_context_tokens: int = 128000
    reserve_tokens: int = 4000

    @property
    def final_output(self) -> str:
        """最终 assistant 输出(messages 最后一条 assistant 的 content)。"""
        for msg in reversed(self.messages):
            if msg.role == "assistant" and msg.content:
                return msg.content
        return ""

    @property
    def context_usage_ratio(self) -> float:
        """当前上下文使用率(0.0 ~ 1.0+)。"""
        total = self.total_input_tokens + self.total_output_tokens
        limit = self.max_context_tokens - self.reserve_tokens
        return total / limit if limit > 0 else 0.0


@dataclass
class AgentResult:
    """Agent 调用的返回结果。"""

    content: str
    model_used: str = ""
    total_tokens: int = 0
    step_count: int = 0


@dataclass
class AgentConfig:
    """DefaultAgent 组件配置——由 AgentFactory 构建后注入。"""

    brain: Any = None  # ModelHub
    executor: Any = None  # DirectExecutor | ReActExecutor
    memory: Any = None  # MemoryModule
    tools: Any = None  # ToolRegistry
    knowledge: Any = None  # KnowledgeEngine | None
    system_prompt: str = ""


@dataclass
class AgentDefinition:
    """角色定义——运行时创建 Agent 的全部信息。"""

    id: str  # UUID
    name: str  # 显示名称
    description: str = ""
    system_prompt: str = ""  # 角色提示词（大字段）
    tools: list[str] = field(default_factory=list)  # 工具名列表
    knowledge_id: str | None = None  # 关联知识库 UUID
    metadata: dict = field(default_factory=dict)
    created_at: str = ""  # ISO format
    updated_at: str = ""


@dataclass
class AgentKnowledge:
    """知识库定义——namespace 的实体层。"""

    id: str  # UUID
    name: str  # 显示名称
    description: str = ""
    namespace: str = ""  # 内部 namespace，如 "kb_<uuid>"
    created_at: str = ""
    updated_at: str = ""