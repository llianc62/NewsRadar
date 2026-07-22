"""单元测试 - PersonaOrchestrator（fan-out + 主编聚合 + 信号解析 + 降级）。

用 FakePersona / FakeManager 隔离真实 LLM 与 MCP，验证：
- Phase 1 并行 fan-out -> 各角色 PersonaSignal
- Phase 2 主编读取 signals 聚合
- chat_stream 事件序列：signals -> token...
- 单角色失败降级为“分析失败”信号，不阻塞其余角色
- parse_signal 的 JSON 解析 / 回退 / confidence 钳制
"""
from __future__ import annotations

import types

import pytest

from agent.persona.orchestrator import PersonaOrchestrator, parse_signal
from agent.persona.signal import PersonaSignal


# ═══════════════════════════════════════════════════════════════════
# Fakes
# ═══════════════════════════════════════════════════════════════════


class FakePersona:
    """假 PersonaAgent：chat 返回固定回复，chat_stream 逐 token。"""

    def __init__(self, name, reply="", tokens=None):
        self.persona_name = name
        self._reply = reply
        self._tokens = tokens or []
        self.running_mode = "strict"
        self.executor = types.SimpleNamespace(_approval_callback=None)
        self.received_signals = None

    async def chat(self, message, model_name=""):
        return types.SimpleNamespace(content=self._reply)

    async def chat_stream(self, message, model_name=""):
        for t in self._tokens:
            yield t

    def set_signals(self, signals):
        self.received_signals = list(signals or [])


class FakeManager:
    """假 PersonaManager：按名返回 FakePersona。"""

    def __init__(self, personas):
        self._personas = personas
        self._running_mode = "strict"
        self._approval_callback = None

    def has(self, name):
        return name in self._personas

    def available(self):
        return []

    async def get(self, name):
        return self._personas[name]

    def set_running_config(self, running_mode, approval_callback=None):
        self._running_mode = running_mode
        self._approval_callback = approval_callback


def _build_orchestrator(personas, *, editor_name="editor"):
    manager = FakeManager(personas)
    return PersonaOrchestrator(manager, editor_name=editor_name, max_concurrent=2)


# ═══════════════════════════════════════════════════════════════════
# parse_signal
# ═══════════════════════════════════════════════════════════════════


def test_parse_signal_success():
    content = '护城河深厚。\n{"stance":"看多","confidence":80,"reasoning":"护城河深"}'
    sig = parse_signal("buffett", "巴菲特", content)
    assert sig.persona == "buffett"
    assert sig.display_name == "巴菲特"
    assert sig.stance == "看多"
    assert sig.confidence == 80
    assert sig.reasoning == "护城河深"
    assert sig.raw == content


def test_parse_signal_fallback_no_json():
    sig = parse_signal("macro", "宏观分析师", "纯文本回复，无 JSON")
    assert sig.stance == ""
    assert sig.confidence == 0
    assert sig.raw == "纯文本回复，无 JSON"


def test_parse_signal_clamps_confidence():
    content = '{"stance":"看多","confidence":250,"reasoning":"x"}'
    sig = parse_signal("wood", "伍德", content)
    assert sig.confidence == 100  # 钳制到上限


def test_parse_signal_bad_confidence_defaults_zero():
    content = '{"stance":"看空","confidence":"abc","reasoning":"x"}'
    sig = parse_signal("taleb", "塔勒布", content)
    assert sig.confidence == 0


# ═══════════════════════════════════════════════════════════════════
# chat() - fan-out + 主编聚合
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_chat_fanout_and_editor_aggregate():
    personas = {
        "buffett": FakePersona(
            "buffett", '价值看多。\n{"stance":"看多","confidence":80,"reasoning":"护城河"}'
        ),
        "sentiment": FakePersona(
            "sentiment", '情绪乐观。\n{"stance":"看多","confidence":70,"reasoning":"情绪暖"}'
        ),
        "editor": FakePersona("editor", reply="综合答复", tokens=["综", "合", "答复"]),
    }
    orch = _build_orchestrator(personas)
    result = await orch.chat("某公司财报大增", ["buffett", "sentiment"])

    assert result.reply == "综合答复"
    assert len(result.signals) == 2
    stances = {s.persona: s.stance for s in result.signals}
    assert stances == {"buffett": "看多", "sentiment": "看多"}
    # 主编收到信号
    assert personas["editor"].received_signals is not None
    assert len(personas["editor"].received_signals) == 2


@pytest.mark.asyncio
async def test_chat_excludes_editor_from_fanout():
    """persona_names 含 editor 时，editor 不参与 Phase 1 fan-out。"""
    personas = {
        "buffett": FakePersona(
            "buffett", '{"stance":"看多","confidence":60,"reasoning":"x"}'
        ),
        "editor": FakePersona("editor", tokens=["主编"]),
    }
    orch = _build_orchestrator(personas)
    result = await orch.chat("hi", ["buffett", "editor"])
    # 只有 buffett 一个信号，editor 不在 signals 里
    assert [s.persona for s in result.signals] == ["buffett"]


@pytest.mark.asyncio
async def test_chat_persona_failure_degrades():
    """某角色 chat 抛异常 -> 降级为“分析失败”信号，其余角色 + 主编照常。"""
    class BoomPersona(FakePersona):
        async def chat(self, message, model_name=""):
            raise RuntimeError("LLM 挂了")

    personas = {
        "buffett": BoomPersona("buffett"),
        "sentiment": FakePersona(
            "sentiment", '{"stance":"看空","confidence":40,"reasoning":"恐慌"}'
        ),
        "editor": FakePersona("editor", reply="答", tokens=["答"]),
    }
    orch = _build_orchestrator(personas)
    result = await orch.chat("暴跌", ["buffett", "sentiment"])

    assert result.reply == "答"
    assert len(result.signals) == 2
    failed = next(s for s in result.signals if s.persona == "buffett")
    assert "分析失败" in failed.reasoning
    ok = next(s for s in result.signals if s.persona == "sentiment")
    assert ok.stance == "看空"


# ═══════════════════════════════════════════════════════════════════
# chat_stream() - 事件序列
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_chat_stream_yields_signals_then_tokens():
    personas = {
        "buffett": FakePersona(
            "buffett", '{"stance":"看多","confidence":80,"reasoning":"护城河"}'
        ),
        "sentiment": FakePersona(
            "sentiment", '{"stance":"中性","confidence":50,"reasoning":"平稳"}'
        ),
        "editor": FakePersona("editor", tokens=["主", "编"]),
    }
    orch = _build_orchestrator(personas)
    events = []
    async for ev in orch.chat_stream("某新闻", ["buffett", "sentiment"]):
        events.append(ev)

    # 第一件是 signals，之后是 token 事件
    assert events[0]["type"] == "signals"
    assert len(events[0]["signals"]) == 2
    token_events = [e for e in events if e["type"] == "token"]
    assert [e["content"] for e in token_events] == ["主", "编"]


@pytest.mark.asyncio
async def test_chat_stream_signals_contain_serializable_dicts():
    personas = {
        "buffett": FakePersona(
            "buffett", '{"stance":"看多","confidence":80,"reasoning":"x"}'
        ),
        "editor": FakePersona("editor", tokens=["t"]),
    }
    orch = _build_orchestrator(personas)
    async for ev in orch.chat_stream("hi", ["buffett"]):
        if ev["type"] == "signals":
            sig = ev["signals"][0]
            assert sig["persona"] == "buffett"
            assert sig["stance"] == "看多"
            assert set(sig.keys()) >= {"persona", "stance", "confidence", "reasoning"}
            break


# ═══════════════════════════════════════════════════════════════════
# 错误路径
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_editor_missing_raises():
    personas = {
        "buffett": FakePersona(
            "buffett", '{"stance":"看多","confidence":80,"reasoning":"x"}'
        ),
        # 无 editor
    }
    orch = _build_orchestrator(personas)
    with pytest.raises(ValueError, match="主编角色未注册"):
        await orch.chat("hi", ["buffett"])


@pytest.mark.asyncio
async def test_chat_empty_persona_names_still_runs_editor():
    """无角色选中时，主编仍聚合空信号列表产出答复（不报错）。"""
    personas = {"editor": FakePersona("editor", reply="无观点可综合", tokens=["无观点可综合"])}
    orch = _build_orchestrator(personas)
    result = await orch.chat("hi", [])
    assert result.reply == "无观点可综合"
    assert result.signals == []


# ═══════════════════════════════════════════════════════════════════
# 端到端：真实 PersonaAgent + MockClient（验 create_persona -> orchestrator 全链路）
# ═══════════════════════════════════════════════════════════════════


class _StreamingMockClient:
    """MockClient：persona.chat 返回 JSON 摘要回复，editor.chat_stream 逐 token。"""

    def __init__(self, persona_name):
        self.persona_name = persona_name
        self.api_key = "test"
        self.base_url = ""

    async def chat(self, messages, tools=None, **kwargs):
        from langchain_core.messages import AIMessage
        if self.persona_name == "editor":
            # 主编做综合叙事（非流式 chat 路径）
            return AIMessage(content="主编综合答复")
        # 角色产出分析 + 末尾 JSON 摘要
        return AIMessage(content=(
            f"{self.persona_name} 的分析。\n"
            f'{{"stance":"看多","confidence":75,"reasoning":"测试理由"}}'
        ))

    async def chat_stream(self, messages, **kwargs):
        from langchain_core.messages import AIMessageChunk
        for t in ["主", "编", "答"]:
            yield AIMessageChunk(content=t)


class _MockingManager:
    """包真 PersonaManager：get() 后注入 _StreamingMockClient。"""

    def __init__(self, models_config):
        from agent.persona import PersonaManager
        self._inner = PersonaManager(models_config, register_mcp=False)

    def has(self, name):
        return self._inner.has(name)

    def available(self):
        return self._inner.available()

    async def get(self, name):
        persona = await self._inner.get(name)
        persona.brain._clients["default"] = _StreamingMockClient(name)
        return persona

    def set_running_config(self, running_mode, approval_callback=None):
        self._inner.set_running_config(running_mode, approval_callback)


_MODELS = {"default": {"protocol": "openai", "model": "test-model", "api_key": "k"}}


@pytest.mark.asyncio
async def test_end_to_end_real_personas_through_orchestrator():
    """真实 PersonaAgent（buffett+sentiment）+ MockClient 跑通编排全链路。"""
    manager = _MockingManager(_MODELS)
    orch = PersonaOrchestrator(manager, max_concurrent=2)

    result = await orch.chat("某公司财报大增", ["buffett", "sentiment"])

    # 两个角色都解析出 JSON 信号
    assert len(result.signals) == 2
    for sig in result.signals:
        assert sig.stance == "看多"
        assert sig.confidence == 75
        assert sig.reasoning == "测试理由"
    # 主编综合答复（非流式 chat 路径）
    assert result.reply == "主编综合答复"


@pytest.mark.asyncio
async def test_end_to_end_stream_events():
    """真实角色下 chat_stream 事件序列：signals -> token..."""
    manager = _MockingManager(_MODELS)
    orch = PersonaOrchestrator(manager, max_concurrent=2)

    events = []
    async for ev in orch.chat_stream("某新闻", ["buffett"]):
        events.append(ev)

    assert events[0]["type"] == "signals"
    assert events[0]["signals"][0]["stance"] == "看多"
    tokens = "".join(e["content"] for e in events if e["type"] == "token")
    assert tokens == "主编答"
