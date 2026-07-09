# Agent 配置设计

> **父文档**: [index.md](index.md)

---

## 1. config.yaml 新增

```yaml
llm:
  deep:
    protocol: anthropic       # anthropic | openai
    model: claude-sonnet-5
    base_url: ""              # 可选，空则用 SDK 默认端点
    api_key: ""               # 用 LLM_DEEP_API_KEY env 覆盖
    temperature: 0.7
  quick:
    protocol: openai
    model: qwen-plus
    base_url: ""
    api_key: ""
    temperature: 0.3

agent:
  window_size: 10             # 上下文窗口大小
  memory_enabled: true        # 是否启用记忆
  compression_strategy: window  # window | summary | multi
  default_model: quick        # 默认模型（quick / deep）
```

**说明**：
- `llm.deep` — 高质量模型，用于用户对话
- `llm.quick` — 快速/低成本模型，用于系统底层操作（上下文压缩、记忆提取）和用户选择的"快速模式"

---

## 2. config/loader.py 扩展

沿用现有 `_load_*_config()` 模式新增：

```python
def _load_llm_instance(cfg: dict, env_prefix: str, required: bool) -> dict | None:
    """加载单个 LLM 实例配置。fail-fast 校验。"""
    if not cfg and not required:
        return None
    if not cfg:
        raise ValueError(f"LLM '{env_prefix.lower()}' 未配置")

    protocol = cfg.get("protocol", "openai")
    if protocol not in ("openai", "anthropic"):
        raise ValueError(f"不支持的 LLM 协议: {protocol!r}")

    api_key = _get_env_str(f"{env_prefix}_API_KEY") or cfg.get("api_key", "")
    if not api_key:
        raise ValueError(f"LLM api_key 未配置（设 {env_prefix}_API_KEY env 或 config.yaml）")

    return {
        "protocol": protocol,
        "model": _get_env_str(f"{env_prefix}_MODEL") or cfg.get("model", ""),
        "base_url": _get_env_str(f"{env_prefix}_BASE_URL") or cfg.get("base_url", ""),
        "api_key": api_key,
        "temperature": cfg.get("temperature", 0.7),
    }

def _load_agent_config(raw: dict) -> dict:
    agent = raw.get("agent", {})
    return {
        "window_size": agent.get("window_size", 10),
        "memory_enabled": agent.get("memory_enabled", True),
        "compression_strategy": agent.get("compression_strategy", "window"),
        "default_model": agent.get("default_model", "quick"),
    }
```

并在 `load_config()` 中调用：

```python
config = {
    ...
    "llm": {
        "deep": _load_llm_instance(raw.get("llm", {}).get("deep", {}), "LLM_DEEP", required=True),
        "quick": _load_llm_instance(raw.get("llm", {}).get("quick", {}), "LLM_QUICK", required=False),
    },
    "agent": _load_agent_config(raw),
}
```

---

## 3. pyproject.toml 新增依赖

```toml
dependencies = [
    ...
    "langchain-anthropic>=0.3",
    "langchain-openai>=0.3",
]
```

**注意**：不需要 `bcrypt`——本系统无用户系统，无需密码哈希。