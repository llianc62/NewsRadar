"""单元测试 - PersonaManager（懒构建 + 缓存 + 并发安全 + 依赖透传）。

用 monkeypatch 替换 ``agent.factory.create_persona``，避免真实 MCP 子进程连接。
"""
from __future__ import annotations

import asyncio

import pytest

from agent.persona import PersonaManager

_CONFIG = {"default": {"protocol": "openai", "model": "m", "api_key": "k"}}


class FakePersona:
    """假 PersonaAgent，记录构造名与 kwargs。"""

    def __init__(self, name, **kwargs):
        self.persona_name = name
        self.built_with = kwargs


# ═══════════════════════════════════════════════════════════════════
# 注册表查询
# ═══════════════════════════════════════════════════════════════════


def test_available_sorted_by_order():
    mgr = PersonaManager(_CONFIG, register_mcp=False)
    specs = mgr.available()
    orders = [s.order for s in specs]
    assert orders == sorted(orders)  # 按 order 升序
    names = [s.name for s in specs]
    assert names[0] == "buffett"     # order=10 最前
    assert "editor" in names         # editor 也在 available（前端按 category 过滤）
    assert len(names) == 10          # 4 投资人 + 5 专家 + 1 主编


def test_has():
    mgr = PersonaManager(_CONFIG, register_mcp=False)
    assert mgr.has("buffett") is True
    assert mgr.has("sentiment") is True
    assert mgr.has("nobody") is False


# ═══════════════════════════════════════════════════════════════════
# 懒构建 + 缓存
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_get_caches_built_instance(monkeypatch):
    calls = []

    async def fake_create(name, config, **kwargs):
        calls.append(name)
        return FakePersona(name, **kwargs)

    monkeypatch.setattr("agent.factory.create_persona", fake_create)

    mgr = PersonaManager(_CONFIG, register_mcp=False)
    p1 = await mgr.get("buffett")
    p2 = await mgr.get("buffett")
    assert p1 is p2
    assert len(calls) == 1  # 第二次命中缓存，不再构建


@pytest.mark.asyncio
async def test_get_passes_knowledge_and_analyzer(monkeypatch):
    captured = {}

    async def fake_create(name, config, **kwargs):
        captured.update(kwargs)
        return FakePersona(name, **kwargs)

    monkeypatch.setattr("agent.factory.create_persona", fake_create)

    knowledge = object()
    analyzer = object()
    mgr = PersonaManager(
        _CONFIG, knowledge=knowledge, analyzer=analyzer, register_mcp=False
    )
    await mgr.get("buffett")
    assert captured["knowledge"] is knowledge
    assert captured["analyzer"] is analyzer
    assert captured["register_mcp"] is False


@pytest.mark.asyncio
async def test_get_injects_short_term_memory_when_db_provided(monkeypatch):
    """有 db 时，get 给 persona 注入 ShortTermMemory（单角色聊天持久化对话 + title 更新）。

    Regression: persona 默认用 NullMemory，user 消息从不落库，会话 title 永远
    停留在"新会话"。注入 ShortTermMemory 后单角色路径会存消息并触发 title 更新；
    团队会诊因 orchestrator 不传 session_id（空），save 自动跳过，现状不变。
    """
    from agent.memory import ShortTermMemory

    captured = {}

    async def fake_create(name, config, **kwargs):
        captured.update(kwargs)
        return FakePersona(name, **kwargs)

    monkeypatch.setattr("agent.factory.create_persona", fake_create)

    fake_db = object()
    mgr = PersonaManager(_CONFIG, register_mcp=False, db=fake_db)
    await mgr.get("buffett")
    memory = captured.get("memory")
    assert isinstance(memory, ShortTermMemory)
    assert memory._db is fake_db


@pytest.mark.asyncio
async def test_get_no_memory_when_db_none(monkeypatch):
    """无 db 时 memory=None（persona 退化为 NullMemory，不持久化）。"""
    captured = {}

    async def fake_create(name, config, **kwargs):
        captured.update(kwargs)
        return FakePersona(name, **kwargs)

    monkeypatch.setattr("agent.factory.create_persona", fake_create)

    mgr = PersonaManager(_CONFIG, register_mcp=False, db=None)
    await mgr.get("buffett")
    assert captured.get("memory") is None


@pytest.mark.asyncio
async def test_get_unknown_raises(monkeypatch):
    mgr = PersonaManager(_CONFIG, register_mcp=False)
    with pytest.raises(ValueError, match="未知角色"):
        await mgr.get("nobody")


@pytest.mark.asyncio
async def test_concurrent_get_builds_once(monkeypatch):
    """并发首取同一角色，double-check 锁保证只构建一次。"""
    builds = 0

    async def fake_create(name, config, **kwargs):
        nonlocal builds
        await asyncio.sleep(0.05)  # 模拟构建耗时，放大竞态窗口
        builds += 1
        return FakePersona(name, **kwargs)

    monkeypatch.setattr("agent.factory.create_persona", fake_create)

    mgr = PersonaManager(_CONFIG, register_mcp=False)
    p1, p2 = await asyncio.gather(mgr.get("buffett"), mgr.get("buffett"))
    assert p1 is p2
    assert builds == 1


# ═══════════════════════════════════════════════════════════════════
# create_persona_manager 工厂（依赖装配 + 降级）
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_create_persona_manager_assembles_analyzer(monkeypatch):
    """analyzer 经 create_analyzer 注入到 manager。"""
    from agent.factory import create_persona_manager

    sentinel = object()
    monkeypatch.setattr("news.analyzer.create_analyzer", lambda cfg, db=None: sentinel)

    config = {"models": _CONFIG, "knowledge": {"enabled": False}}
    mgr = await create_persona_manager(config, db=None)
    assert isinstance(mgr, PersonaManager)
    assert mgr._analyzer is sentinel
    assert mgr._knowledge is None  # knowledge 未启用


@pytest.mark.asyncio
async def test_create_persona_manager_knowledge_enabled(monkeypatch):
    """knowledge.enabled + api_key + db 时构建 KnowledgeEngine 并注入。"""
    from agent.factory import create_persona_manager

    monkeypatch.setattr("news.analyzer.create_analyzer", lambda cfg, db=None: None)
    fake_engine = object()
    monkeypatch.setattr("agent.knowledge.EmbeddingClient", lambda **kw: object())
    monkeypatch.setattr("agent.knowledge.PgVectorKnowledgeStore", lambda db: object())
    monkeypatch.setattr("agent.knowledge.KnowledgeEngine", lambda **kw: fake_engine)

    fake_db = object()
    config = {
        "models": _CONFIG,
        "knowledge": {"enabled": True, "embedding_api_key": "k", "top_k": 7},
    }
    mgr = await create_persona_manager(config, db=fake_db)
    assert mgr._knowledge is fake_engine


@pytest.mark.asyncio
async def test_create_persona_manager_knowledge_disabled(monkeypatch):
    """knowledge 未启用 / 缺 db 时 knowledge=None（角色仍可对话）。"""
    from agent.factory import create_persona_manager

    monkeypatch.setattr("news.analyzer.create_analyzer", lambda cfg, db=None: None)
    config = {"models": _CONFIG, "knowledge": {"enabled": True, "embedding_api_key": "k"}}
    # 无 db -> knowledge 不构建
    mgr = await create_persona_manager(config, db=None)
    assert mgr._knowledge is None


@pytest.mark.asyncio
async def test_create_persona_manager_passes_db_to_manager(monkeypatch):
    """create_persona_manager 把 db 透传给 PersonaManager（供角色记忆持久化）。"""
    from agent.factory import create_persona_manager

    monkeypatch.setattr("news.analyzer.create_analyzer", lambda cfg, db=None: None)
    fake_db = object()
    config = {"models": _CONFIG, "knowledge": {"enabled": False}}
    mgr = await create_persona_manager(config, db=fake_db)
    assert isinstance(mgr, PersonaManager)
    assert mgr._db is fake_db
