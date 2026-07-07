from __future__ import annotations
from collections.abc import AsyncIterator
from agent.types import LlmConfig
from agent.llm import build_llm


class Agent:
    """Phase 0：默认（无角色）agent，直调 LLM，支持流式。"""

    def __init__(self, llm_cfg: LlmConfig):
        self.llm = build_llm(llm_cfg)

    async def chat_stream(self, message: str) -> AsyncIterator[str]:
        """流式调 LLM，逐 token yield。"""
        async for chunk in self.llm.astream(message):
            content = chunk.content
            if content:
                yield content

    async def chat(self, message: str) -> str:
        """非流式版本，兼容简单场景。"""
        response = await self.llm.ainvoke(message)
        return response.content
