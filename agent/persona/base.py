"""PersonaAgent - 角色扮演 agent 基类（仿 ai-hedge-fund LLMAgent）。

角色 = ``name`` + ``get_system_prompt()`` + 可选知识库检索 + 可选硬编码专业分析。
继承 ``DefaultAgent`` 复用 brain/executor/memory/tools，不包装、不 peer。

铁律（ai-hedge-fund VISION）"The LLM never touches the trade"：硬编码逻辑
（``_pre_analyze``）产出结构化事实，LLM 只用角色声音叙事。
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from ..agent import DefaultAgent
from ..data import Context

if TYPE_CHECKING:
    from ..knowledge import KnowledgeEngine


class PersonaAgent(DefaultAgent):
    """角色 agent 基类。

    子类只需定义 ``get_system_prompt()``（人格声音），可选 override
    ``_pre_analyze()`` 注入硬编码专业逻辑产出的结构化事实
    （仿 ai-hedge-fund ``ben_graham`` 的 Graham Number 计算）。

    Args:
        config: 模型配置 dict（``config["models"]``）。
        persona_name: 角色标识（如 ``"buffett"``），不可空。
        knowledge: 可选 ``KnowledgeEngine``，按 ``kb_namespace`` 检索知识。
        kb_namespace: 知识库命名空间（如 ``"investing/buffett"``）。
    """

    #: 子类置 True 表示构造需要 ``analyzer`` 参数（硬编码专业逻辑依赖）。
    requires_analyzer: bool = False

    #: 子类置 True 表示优先用 ``DirectExecutor``（真流式、无工具），适合纯叙事
    #: 聚合角色（如主编）--避免 ReActExecutor 的假流式与无谓的 MCP 子进程。
    prefer_direct_executor: bool = False

    def __init__(
        self,
        config: dict,
        *,
        persona_name: str,
        knowledge: "KnowledgeEngine | None" = None,
        kb_namespace: str = "",
        system_prompt: str = "",
        base_prompt: str = "",
        executor=None,
        memory=None,
        tools=None,
        running_mode: str = "normal",
    ):
        super().__init__(
            config=config,
            executor=executor,
            memory=memory,
            system_prompt=system_prompt,
            tools=tools,
            running_mode=running_mode,
            knowledge=knowledge,
            kb_namespace=kb_namespace,
        )
        if not persona_name:
            raise ValueError("persona_name 不能为空")
        self.persona_name = persona_name
        self._base_prompt = base_prompt

    def get_system_prompt(self) -> str:
        """人格声音--子类 override。默认返回基座提示词 + 个性提示词。"""
        prompt = self.system_prompt
        if self._base_prompt:
            prompt = self._base_prompt + "\n\n" + prompt
        return prompt

    def _pre_analyze(self, user_input: str) -> dict | None:
        """硬编码专业逻辑钩子--子类 override。

        返回结构化事实 dict（如 ``{"sentiment_score": 72, "label": "利好"}``），
        渲染进 ``## 专业分析`` 块。基类返回 ``None``（无分析）。
        """
        return None

    async def _make_ctx(
        self, user_input: str, session_id: str, model_name: str
    ) -> Context:
        ctx = await super()._make_ctx(user_input, session_id, model_name)
        ctx.persona_name = self.persona_name
        ctx.system_prompt = self.get_system_prompt()

        # 硬编码专业分析（sync CPU 工作 -> to_thread）
        analysis = await asyncio.to_thread(self._pre_analyze, user_input)
        ctx.analysis_context = self._render_analysis(analysis) if analysis else None

        return ctx

    @staticmethod
    def _render_analysis(analysis: dict) -> str:
        """将 ``_pre_analyze`` 的 dict 渲染成 ``## 专业分析`` 块文本。"""
        return "\n".join(f"{k}: {v}" for k, v in analysis.items())
