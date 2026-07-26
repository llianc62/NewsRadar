"""单元测试 - 角色扮演 agent（PersonaAgent + registry + factory）。

用 MockClient（不调真实 API）+ FakeKnowledge + FakeAnalyzer 验证：
- 人格 system prompt 注入
- 知识库检索 -> ## 知识库 块
- 硬编码专业分析 -> ## 专业分析 块
- create_persona 工厂 + PersonaRegistry
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from agent.factory import create_persona
from agent.model_hub import ModelHub
from agent.data import Context
from agent.persona import (
    PERSONA_REGISTRY,
    PersonaAgent,
    get_persona_spec,
    list_personas,
)
from agent.persona.experts.sentiment import SentimentPersona
from agent.persona.investors.buffett import BuffettPersona
from agent.persona.editor import EditorPersona
from agent.persona.experts.blackswan import BlackswanPersona
from agent.persona.experts.factcheck import FactcheckPersona
from agent.persona.experts.industry import IndustryPersona
from agent.persona.experts.macro import MacroPersona
from agent.persona.investors.graham import GrahamPersona
from agent.persona.investors.taleb import TalebPersona
from agent.persona.investors.wood import WoodPersona
from agent.persona.signal import PersonaSignal


# ═══════════════════════════════════════════════════════════════════
# Fakes
# ═══════════════════════════════════════════════════════════════════


class MockClient:
    """Mock LLM Client，记录 messages，不调真实 API。

    实现 ``LLMClient`` 协议，返回 ``AIMessage``。
    """

    def __init__(self, api_key: str = "test", base_url: str = ""):
        self.api_key = api_key
        self.base_url = base_url
        self.last_messages: list[dict] = []

    async def chat(self, messages, tools=None, **kwargs):
        self.last_messages = messages
        from langchain_core.messages import AIMessage
        return AIMessage(content="mock response for gpt-4o")

    async def chat_stream(self, messages, **kwargs):
        self.last_messages = messages
        from langchain_core.messages import AIMessageChunk
        for token in ["mock ", "response"]:
            yield AIMessageChunk(content=token)


class FakeKnowledge:
    """假 KnowledgeEngine，记录调用、返回固定文本。

    模拟 ``KnowledgeEngine.search(ctx)`` 接口：按 ``_namespace`` 检索，
    非空结果追加 ``MemoryBlock(title="知识库")`` 到 ``ctx.memories``。
    """

    def __init__(self, text: str = "mock knowledge snippet", namespace: str = ""):
        self.text = text
        self.last_query: str = ""
        self.last_ns: str = ""
        self._namespace = namespace

    def retrieve_render(self, query: str, namespace: str, top_k: int | None = None) -> str:
        self.last_query = query
        self.last_ns = namespace
        return self.text

    async def search(self, ctx) -> None:
        if not self._namespace or not ctx.user_input:
            return
        text = self.retrieve_render(ctx.user_input, self._namespace)
        if text:
            from agent.data import MemoryBlock
            ctx.memories.append(MemoryBlock(
                title="知识库", source="knowledge", content=text, order=20,
            ))


class FakeAnalyzer:
    """假 JiebaAnalyzer，analyze_sentiment 原地写固定 sentiment_score。"""

    def __init__(self, score: int = 72):
        self._score = score

    def analyze_sentiment(self, items: list) -> None:
        for item in items:
            item["sentiment_score"] = self._score


_CONFIG = {"default": {"protocol": "openai", "model": "test-model", "api_key": "test"}}


def _inject_mock(persona: PersonaAgent) -> MockClient:
    """把 MockClient 注入 persona 的 ModelHub，返回该 mock。"""
    mock = MockClient()
    persona.brain._clients["default"] = mock
    return mock


def _system_texts(messages: list[dict]) -> list[str]:
    """提取所有 system 消息的 content。"""
    return [m["content"] for m in messages if m["role"] == "system"]


# ═══════════════════════════════════════════════════════════════════
# PersonaAgent 基类
# ═══════════════════════════════════════════════════════════════════


class TestPersonaAgentBase:
    def test_empty_persona_name_raises(self):
        with pytest.raises(ValueError, match="persona_name"):
            PersonaAgent(_CONFIG, persona_name="")

    def test_get_system_prompt_default(self):
        """基类 get_system_prompt 返回构造时传入的 system_prompt。"""
        persona = PersonaAgent(
            _CONFIG, persona_name="x", system_prompt="hello",
        )
        assert persona.get_system_prompt() == "hello"

    def test_requires_analyzer_default_false(self):
        assert PersonaAgent.requires_analyzer is False


# ═══════════════════════════════════════════════════════════════════
# BuffettPersona（纯人格 prompt，无硬编码逻辑）
# ═══════════════════════════════════════════════════════════════════


class TestBuffettPersona:
    def test_prompt_has_voice_and_checklist(self):
        prompt = BuffettPersona(_CONFIG).get_system_prompt()
        assert "巴菲特" in prompt
        assert "护城河" in prompt
        assert "JSON" in prompt

    @pytest.mark.asyncio
    async def test_chat_injects_persona_prompt(self):
        persona = BuffettPersona(_CONFIG)
        mock = _inject_mock(persona)
        result = await persona.chat("看这条新闻：某公司发布财报")
        assert result.content == "mock response for gpt-4o"
        # 第一条 system 消息 = 巴菲特人格 prompt
        systems = _system_texts(mock.last_messages)
        assert any("巴菲特" in s for s in systems)
        # 无知识库/分析时不注入这两个块
        assert not any(s.startswith("## 知识库\n") for s in systems)
        assert not any(s.startswith("## 专业分析\n") for s in systems)

    @pytest.mark.asyncio
    async def test_make_ctx_sets_system_prompt(self):
        persona = BuffettPersona(_CONFIG)
        ctx = await persona._make_ctx("hi", "sess", "")
        assert ctx.system_prompt == persona.get_system_prompt()
        # Context 不再有 persona_name/analysis_context/knowledge_context 字段
        assert not hasattr(ctx, "persona_name")
        assert not hasattr(ctx, "analysis_context")
        assert not hasattr(ctx, "knowledge_context")


# ═══════════════════════════════════════════════════════════════════
# SentimentPersona（硬编码专业逻辑）
# ═══════════════════════════════════════════════════════════════════


class TestSentimentPersona:
    def test_requires_analyzer_true(self):
        assert SentimentPersona.requires_analyzer is True

    def test_pre_analyze_positive(self):
        persona = SentimentPersona(_CONFIG, analyzer=FakeAnalyzer(score=72))
        result = persona._pre_analyze("市场大涨，情绪乐观")
        assert result["sentiment_score"] == 72
        assert result["label"] == "利好"

    def test_pre_analyze_negative(self):
        persona = SentimentPersona(_CONFIG, analyzer=FakeAnalyzer(score=20))
        result = persona._pre_analyze("暴跌恐慌")
        assert result["label"] == "利空"

    def test_pre_analyze_neutral(self):
        persona = SentimentPersona(_CONFIG, analyzer=FakeAnalyzer(score=50))
        assert persona._pre_analyze("震荡整理")["label"] == "中性"

    def test_pre_analyze_no_analyzer_returns_none(self):
        persona = SentimentPersona(_CONFIG, analyzer=None)
        assert persona._pre_analyze("任何内容") is None

    def test_pre_analyze_analyzer_raises_returns_none(self):
        class BoomAnalyzer:
            def analyze_sentiment(self, items):
                raise RuntimeError("boom")

        persona = SentimentPersona(_CONFIG, analyzer=BoomAnalyzer())
        assert persona._pre_analyze("大涨") is None

    def test_pre_analyze_empty_input_returns_none(self):
        persona = SentimentPersona(_CONFIG, analyzer=FakeAnalyzer())
        assert persona._pre_analyze("   ") is None

    @pytest.mark.asyncio
    async def test_chat_injects_analysis_block(self):
        persona = SentimentPersona(
            _CONFIG, analyzer=FakeAnalyzer(score=72),
        )
        mock = _inject_mock(persona)
        await persona.chat("市场情绪极度乐观")
        systems = _system_texts(mock.last_messages)
        # ## 专业分析 块含硬编码产出（用 startswith 排除 prompt 里的字面提及）
        analysis_block = [s for s in systems if s.startswith("## 专业分析\n")]
        assert len(analysis_block) == 1
        assert "sentiment_score: 72" in analysis_block[0]
        assert "利好" in analysis_block[0]

    @pytest.mark.asyncio
    async def test_chat_without_analyzer_no_analysis_block(self):
        persona = SentimentPersona(
            _CONFIG, analyzer=None,
        )
        mock = _inject_mock(persona)
        await persona.chat("hi")
        systems = _system_texts(mock.last_messages)
        assert not any(s.startswith("## 专业分析\n") for s in systems)


# ═══════════════════════════════════════════════════════════════════
# 知识库注入
# ═══════════════════════════════════════════════════════════════════


class TestKnowledgeInjection:
    @pytest.mark.asyncio
    async def test_chat_injects_knowledge_block(self):
        fake_kb = FakeKnowledge(text="知识片段正文", namespace="investing/buffett")
        persona = PersonaAgent(
            _CONFIG, persona_name="buffett", knowledge=fake_kb,
            kb_namespace="investing/buffett",
        )
        mock = _inject_mock(persona)
        await persona.chat("查询")
        systems = _system_texts(mock.last_messages)
        kb_block = [s for s in systems if s.startswith("## 知识库\n")]
        assert len(kb_block) == 1
        assert "知识片段正文" in kb_block[0]
        assert fake_kb.last_query == "查询"
        assert fake_kb.last_ns == "investing/buffett"

    @pytest.mark.asyncio
    async def test_chat_empty_knowledge_skipped(self):
        """知识引擎返回空串时不注入 ## 知识库 块。"""
        fake_kb = FakeKnowledge(text="", namespace="investing/buffett")
        persona = PersonaAgent(
            _CONFIG, persona_name="buffett", knowledge=fake_kb,
            kb_namespace="investing/buffett",
        )
        mock = _inject_mock(persona)
        await persona.chat("查询")
        systems = _system_texts(mock.last_messages)
        assert not any(s.startswith("## 知识库\n") for s in systems)

    @pytest.mark.asyncio
    async def test_no_knowledge_engine_no_block(self):
        persona = PersonaAgent(
            _CONFIG, persona_name="buffett",
        )
        mock = _inject_mock(persona)
        await persona.chat("查询")
        systems = _system_texts(mock.last_messages)
        assert not any(s.startswith("## 知识库\n") for s in systems)


# ═══════════════════════════════════════════════════════════════════
# Factory
# ═══════════════════════════════════════════════════════════════════


class TestCreatePersona:
    @pytest.mark.asyncio
    async def test_create_buffett(self):
        persona = await create_persona(
            "buffett", _CONFIG, register_mcp=False,
        )
        assert isinstance(persona, BuffettPersona)
        assert persona.persona_name == "buffett"
        assert persona.kb_namespace == "investing/buffett"
        assert "巴菲特" in persona.get_system_prompt()
        assert persona.brain.get_model_version("default") == "test-model"

    @pytest.mark.asyncio
    async def test_create_sentiment_with_analyzer(self):
        persona = await create_persona(
            "sentiment", _CONFIG, register_mcp=False,
            analyzer=FakeAnalyzer(score=72),
        )
        assert isinstance(persona, SentimentPersona)
        # analyzer 注入后 _pre_analyze 生效
        assert persona._pre_analyze("大涨")["label"] == "利好"

    @pytest.mark.asyncio
    async def test_create_unknown_raises(self):
        with pytest.raises(ValueError, match="未知角色"):
            await create_persona("nobody", _CONFIG, register_mcp=False)

    @pytest.mark.asyncio
    async def test_created_persona_can_chat(self):
        persona = await create_persona(
            "buffett", _CONFIG, register_mcp=False,
        )
        mock = _inject_mock(persona)
        result = await persona.chat("你好")
        assert result.content == "mock response for gpt-4o"
        assert any("巴菲特" in s for s in _system_texts(mock.last_messages))

    @pytest.mark.asyncio
    async def test_editor_uses_direct_executor(self):
        """主编 prefer_direct_executor -> DirectExecutor（真流式），普通角色默认 ReAct。"""
        from agent.executor import DirectExecutor, ReActExecutor

        editor = await create_persona("editor", _CONFIG, register_mcp=False)
        assert isinstance(editor.executor, DirectExecutor)
        assert editor.tools is None  # 纯叙事角色不挂工具

        buffett = await create_persona("buffett", _CONFIG, register_mcp=False)
        assert isinstance(buffett.executor, ReActExecutor)
        assert buffett.tools is not None  # 普通角色挂内置工具


# ═══════════════════════════════════════════════════════════════════
# Registry
# ═══════════════════════════════════════════════════════════════════


class TestRegistry:
    def test_list_personas_sorted_by_order(self):
        specs = list_personas()
        assert [s.order for s in specs] == sorted(s.order for s in specs)
        names = [s.name for s in specs]
        assert "buffett" in names
        assert "sentiment" in names

    def test_get_persona_spec(self):
        spec = get_persona_spec("buffett")
        assert spec is not None
        assert spec.display_name == "巴菲特"
        assert spec.category == "investor"

    def test_get_unknown_returns_none(self):
        assert get_persona_spec("nobody") is None

    def test_registry_has_seed_personas(self):
        assert set(PERSONA_REGISTRY) >= {"buffett", "sentiment"}
        # 覆盖两个 category
        cats = {s.category for s in list_personas()}
        assert cats >= {"investor", "expert"}


# ═══════════════════════════════════════════════════════════════════
# Phase C 角色（macro/industry/factcheck/blackswan + graham/taleb/wood + editor）
# ═══════════════════════════════════════════════════════════════════


class FakeKeywordAnalyzer:
    """假 analyzer，analyze_keywords 原地写固定 tags。"""

    def __init__(self, tags):
        self._tags = tags

    def analyze_keywords(self, items, topk=5):
        for item in items:
            item["tags"] = list(self._tags)


class TestPhase3Personas:
    """各新角色的人格 prompt + analyzer 注入 + _pre_analyze 硬编码逻辑。"""

    @pytest.mark.parametrize("cls,name,voice", [
        (MacroPersona, "macro", "宏观"),
        (FactcheckPersona, "factcheck", "核查"),
        (GrahamPersona, "graham", "安全边际"),
        (TalebPersona, "taleb", "反脆弱"),
        (WoodPersona, "wood", "创新"),
    ])
    def test_pure_prompt_personas(self, cls, name, voice):
        persona = cls(_CONFIG)
        assert persona.persona_name == name
        assert persona.kb_namespace  # 均挂知识库命名空间
        prompt = persona.get_system_prompt()
        assert voice in prompt
        assert "JSON" in prompt  # 统一以 JSON 摘要收口
        assert persona._pre_analyze("hi") is None  # 纯 prompt 无硬编码逻辑

    def test_industry_pre_analyze_with_keywords(self):
        persona = IndustryPersona(
            _CONFIG, analyzer=FakeKeywordAnalyzer(["新能源", "锂电"]),
        )
        result = persona._pre_analyze("宁德时代发布新电池")
        assert result is not None
        assert "新能源" in result["关键词"]
        assert result["text_sample"]

    def test_industry_pre_analyze_no_analyzer(self):
        persona = IndustryPersona(_CONFIG, analyzer=None)
        assert persona._pre_analyze("任何内容") is None

    def test_industry_requires_analyzer(self):
        assert IndustryPersona.requires_analyzer is True

    def test_blackswan_extreme_panic(self):
        persona = BlackswanPersona(
            _CONFIG, analyzer=FakeAnalyzer(score=10),
        )
        result = persona._pre_analyze("市场崩盘恐慌")
        assert result["情绪区间"] == "极端恐慌"
        assert result["极端异常"] == "是"

    def test_blackswan_normal_range(self):
        persona = BlackswanPersona(
            _CONFIG, analyzer=FakeAnalyzer(score=50),
        )
        result = persona._pre_analyze("平稳震荡")
        assert result["极端异常"] == "否"
        assert result["情绪区间"] == "正常区间"

    def test_blackswan_requires_analyzer(self):
        assert BlackswanPersona.requires_analyzer is True

    def test_editor_pre_analyze_with_signals(self):
        persona = EditorPersona(_CONFIG)
        persona.set_signals([
            PersonaSignal(persona="buffett", display_name="巴菲特",
                          stance="看多", confidence=80, reasoning="护城河"),
        ])
        result = persona._pre_analyze("hi")
        assert result is not None
        assert "巴菲特" in result["各角色信号"]
        assert "看多" in result["各角色信号"]

    def test_editor_pre_analyze_no_signals(self):
        persona = EditorPersona(_CONFIG)
        assert persona._pre_analyze("hi") is None

    def test_editor_kb_namespace_empty(self):
        persona = EditorPersona(_CONFIG)
        assert persona.kb_namespace == ""
        assert persona.persona_name == "editor"


class TestRegistryComplete:
    def test_registry_has_all_personas(self):
        expected = {
            "buffett", "graham", "taleb", "wood",  # investors
            "macro", "sentiment", "industry", "factcheck", "blackswan",  # experts
            "editor",
        }
        assert expected <= set(PERSONA_REGISTRY)

    def test_editor_excluded_from_selectable_via_category(self):
        """editor 的 category='editor'，前端据此排除出可选面板。"""
        spec = get_persona_spec("editor")
        assert spec is not None
        assert spec.category == "editor"

    def test_requires_analyzer_flags(self):
        assert PERSONA_REGISTRY["sentiment"].cls.requires_analyzer is True
        assert PERSONA_REGISTRY["industry"].cls.requires_analyzer is True
        assert PERSONA_REGISTRY["blackswan"].cls.requires_analyzer is True
        # 纯 prompt 角色
        for name in ("buffett", "macro", "factcheck", "editor"):
            assert PERSONA_REGISTRY[name].cls.requires_analyzer is False
