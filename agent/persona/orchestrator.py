"""PersonaOrchestrator - 多角色并行编排 + 主编聚合（仿 ai-hedge-fund fan-out）。

数据流::

    用户消息
      │
      ├─ Phase 1（asyncio.gather 并行 fan-out）
      │     每个 PersonaAgent 独立 chat() -> 解析末尾 JSON -> PersonaSignal
      │     失败的角色降级为“分析失败”信号，不阻塞其余角色
      ├─ Phase 2（主编聚合）
      │     EditorPersona.set_signals(signals) -> chat_stream() 真流式
      ▼
    主编综合答复 + 各角色信号

并发用 ``asyncio.Semaphore`` 限流，避免一次提问触发过多 LLM 并发调用。
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator

from .manager import PersonaManager
from .signal import OrchestratorResult, PersonaSignal

# 匹配回复末尾的 JSON 摘要行：{"stance":"...","confidence":N,"reasoning":"..."}
_JSON_LINE_RE = re.compile(r"\{[^{}]*\"stance\"[^{}]*\}", re.DOTALL)


def parse_signal(persona_name: str, display_name: str, content: str) -> PersonaSignal:
    """从角色 LLM 回复中解析末尾 JSON 摘要为 :class:`PersonaSignal`。

    解析失败时回退为空信号（stance 空），但保留 ``raw`` 全文供主编参考。
    """
    matches = _JSON_LINE_RE.findall(content)
    if matches:
        try:
            data = json.loads(matches[-1])
            return PersonaSignal(
                persona=persona_name,
                display_name=display_name,
                stance=str(data.get("stance", "")).strip(),
                confidence=_clamp_int(data.get("confidence", 0)),
                reasoning=str(data.get("reasoning", "")).strip(),
                raw=content,
            )
        except (json.JSONDecodeError, ValueError):
            pass
    return PersonaSignal(
        persona=persona_name, display_name=display_name, raw=content,
    )


def _clamp_int(value, lo: int = 0, hi: int = 100) -> int:
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return 0


class PersonaOrchestrator:
    """多角色编排器。

    Args:
        manager: :class:`PersonaManager`（懒构建各 PersonaAgent）。
        editor_name: 主编角色名（默认 ``"editor"``）。
        max_concurrent: Phase 1 并行角色数上限（限流 LLM 并发）。
    """

    def __init__(
        self,
        manager: PersonaManager,
        *,
        editor_name: str = "editor",
        max_concurrent: int = 4,
    ):
        self._manager = manager
        self._editor_name = editor_name
        self._sem = asyncio.Semaphore(max_concurrent)

    async def chat(
        self, message: str, persona_names: list[str], *, model_name: str = ""
    ) -> OrchestratorResult:
        """非流式：Phase 1 fan-out -> Phase 2 主编聚合，返回完整结果。"""
        signals = await self._fanout(message, persona_names, model_name)
        reply = await self._run_editor(message, signals, model_name)
        return OrchestratorResult(reply=reply, signals=signals)

    async def chat_stream(
        self, message: str, persona_names: list[str], *, model_name: str = ""
    ) -> AsyncIterator[dict]:
        """流式：Phase 1 静默并行 -> yield signals -> Phase 2 主编逐 token。

        yield 的 dict 事件：
        - ``{"type": "signals", "signals": [...]}``：Phase 1 完成，各角色信号
        - ``{"type": "token", "content": "..."}``：主编流式 token
        """
        signals = await self._fanout(message, persona_names, model_name)
        yield {
            "type": "signals",
            "signals": [s.model_dump() for s in signals],
        }
        editor = await self._editor()
        _set_signals(editor, signals)
        async for token in editor.chat_stream(message, model_name=model_name):
            yield {"type": "token", "content": token}

    # ── Phase 1: 并行 fan-out ──────────────────────────────────────

    async def _fanout(
        self, message: str, persona_names: list[str], model_name: str
    ) -> list[PersonaSignal]:
        names = [n for n in (persona_names or []) if n and n != self._editor_name]
        tasks = [self._run_one(name, message, model_name) for name in names]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        signals: list[PersonaSignal] = []
        for name, res in zip(names, results):
            if isinstance(res, Exception):
                signals.append(
                    PersonaSignal(
                        persona=name,
                        stance="中性",
                        confidence=0,
                        reasoning=f"分析失败: {res!s}",
                    )
                )
            else:
                signals.append(res)
        return signals

    async def _run_one(
        self, name: str, message: str, model_name: str
    ) -> PersonaSignal:
        async with self._sem:
            persona = await self._manager.get(name)
            spec = self._manager.has(name) and _spec_of(name)
            display = spec.display_name if spec else name
            result = await persona.chat(message, model_name=model_name)
            return parse_signal(name, display, result.content)

    # ── Phase 2: 主编聚合 ──────────────────────────────────────────

    async def _run_editor(
        self, message: str, signals: list[PersonaSignal], model_name: str
    ) -> str:
        editor = await self._editor()
        _set_signals(editor, signals)
        result = await editor.chat(message, model_name=model_name)
        return result.content

    async def _editor(self):
        if not self._manager.has(self._editor_name):
            raise ValueError(f"主编角色未注册: {self._editor_name!r}")
        return await self._manager.get(self._editor_name)


def _set_signals(editor, signals) -> None:
    """向主编注入信号（若主编支持 set_signals）。"""
    setter = getattr(editor, "set_signals", None)
    if callable(setter):
        setter(signals)


def _spec_of(name: str):
    """惰性查角色规格，避免顶层循环导入。"""
    from .registry import get_persona_spec

    return get_persona_spec(name)
