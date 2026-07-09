from __future__ import annotations
from dataclasses import dataclass


@dataclass
class LlmConfig:
    protocol: str           # "anthropic" | "openai"
    model: str
    api_key: str
    base_url: str = ""
    temperature: float = 0.7
