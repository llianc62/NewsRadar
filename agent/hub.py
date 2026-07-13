from __future__ import annotations

from .llm import AnthropicClient, BaseClient, OpenAIClient

_PROVIDER_MAP: dict[str, type[BaseClient]] = {
    "openai": OpenAIClient,
    "anthropic": AnthropicClient,
}


class ModelHub:
    """LLM Client 管理器。

    职责:
    - 管理模型配置（原始 dict 格式）
    - 惰性创建 BaseClient 实例
    - 按名称（别名）返回 Client

    不负责:
    - ❌ 封装 chat() / chat_stream() 调用
    - ❌ 模型选择逻辑（交给 Executor）

    使用方式:
        hub = ModelHub(config={
            "default": {"protocol": "openai", "model": "gpt-4o", "api_key": "..."},
            "cheap":   {"protocol": "openai", "model": "gpt-4o-mini", "api_key": "..."},
        })

        client = hub.get_default()
        resp = await client.chat(model="gpt-4o", messages=[...])
    """

    def __init__(self, config: dict):
        self._config = config
        self._clients: dict[str, BaseClient] = {}

    def get_default(self) -> BaseClient:
        """获取默认模型 Client（name='default' 的配置）。"""
        return self.get("default")

    def get(self, name: str) -> BaseClient:
        """按配置 name 获取或创建 Client。"""
        if name not in self._clients:
            cfg = self._config[name]
            client_cls = _PROVIDER_MAP.get(cfg["protocol"])
            if not client_cls:
                raise ValueError(
                    f"Unsupported protocol: {cfg['protocol']!r} "
                    f"(supported: {list(_PROVIDER_MAP)})"
                )
            self._clients[name] = client_cls(
                api_key=cfg["api_key"],
                base_url=cfg.get("base_url", ""),
            )
        return self._clients[name]

    def get_model_version(self, name: str) -> str:
        """按配置 name 返回实际的模型版本。"""
        return self._config[name]["model"]
