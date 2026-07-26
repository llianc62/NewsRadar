from __future__ import annotations

from .llm import AnthropicClient, DeepSeekClient, OpenAIClient


def _is_deepseek(cfg: dict) -> bool:
    """是否走 DeepSeek 思考模式 client。"""
    base_url = (cfg.get("base_url") or "").lower()
    model = (cfg.get("model") or "").lower()
    return "deepseek.com" in base_url or model.startswith("deepseek")


def _build_client(cfg: dict):
    """按 protocol + 厂商构建 LangChain LLM client。

    返回 ``ChatOpenAI`` / ``ChatAnthropic`` / ``ChatDeepSeek`` 实例，
    直接实现 ``LLMClient`` 协议（``chat()`` / ``chat_stream()`` 返回 ``AIMessage``）。
    """
    protocol = cfg.get("protocol", "openai")
    common = {
        "api_key": cfg.get("api_key", ""),
        "base_url": cfg.get("base_url", ""),
        "model": cfg.get("model", ""),
    }
    if protocol == "anthropic":
        return AnthropicClient(**common)
    if protocol == "openai":
        if _is_deepseek(cfg):
            return DeepSeekClient(**common)
        return OpenAIClient(**common)
    raise ValueError(
        f"Unsupported protocol: {protocol!r} (supported: openai, anthropic)"
    )


class ModelHub:
    """LLM Client 管理器。

    职责:
    - 管理模型配置（原始 dict 格式）
    - 惰性创建 LangChain LLM Client 实例（OpenAIClient / AnthropicClient / DeepSeekClient）
    - 按名称（别名）返回 Client

    不负责:
    - ❌ 封装 chat() / chat_stream() 调用（由 Executor 直接调 client.chat() / client.chat_stream()）
    - ❌ 模型选择逻辑（交给 Executor）

    使用方式:
        hub = ModelHub(config={
            "default": {"protocol": "openai", "model": "gpt-4o", "api_key": "..."},
            "quick":   {"protocol": "openai", "model": "deepseek-v4-flash", "api_key": "...",
                        "base_url": "https://api.deepseek.com"},
        })

        client = hub.get_default()          # ChatOpenAI / ChatAnthropic 实例
        result = await client.chat(messages=[...])  # 返回 AIMessage
    """

    def __init__(self, config: dict):
        self._config = config
        self._clients: dict = {}

    def get_default(self):
        """获取默认模型 Client（name='default' 的配置）。"""
        return self.get("default")

    def get(self, name: str):
        """按配置 name 获取或创建 Client。

        返回实现 ``LLMClient`` 协议的 LangChain 实例
        （``ChatOpenAI`` / ``ChatAnthropic`` / ``ChatDeepSeek``）。
        """
        if name not in self._clients:
            cfg = self._config[name]
            self._clients[name] = _build_client(cfg)
        return self._clients[name]

    def get_model_version(self, name: str) -> str:
        """按配置 name 返回实际的模型版本。"""
        return self._config[name]["model"]