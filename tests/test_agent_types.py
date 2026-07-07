"""Tests for agent/types.py."""
import pytest
from agent.types import LlmConfig, Turn


class TestLlmConfig:
    def test_creates_with_required_fields(self):
        cfg = LlmConfig(protocol="openai", model="gpt-4", api_key="sk-xxx")
        assert cfg.protocol == "openai"
        assert cfg.model == "gpt-4"
        assert cfg.api_key == "sk-xxx"

    def test_defaults(self):
        cfg = LlmConfig(protocol="anthropic", model="claude", api_key="sk-xxx")
        assert cfg.base_url == ""
        assert cfg.temperature == 0.7

    def test_custom_temperature(self):
        cfg = LlmConfig(protocol="openai", model="gpt-4", api_key="sk-xxx", temperature=0.3)
        assert cfg.temperature == 0.3


class TestTurn:
    def test_creates_turn(self):
        t = Turn(user="hello", assistant="hi there")
        assert t.user == "hello"
        assert t.assistant == "hi there"

    def test_turn_equality(self):
        t1 = Turn(user="a", assistant="b")
        t2 = Turn(user="a", assistant="b")
        assert t1 == t2
