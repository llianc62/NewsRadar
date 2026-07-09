# Phase 1：上下文工程

> **父文档**: [index.md](index.md)  
> **产出**: 长对话不爆 token  
> **可验证**: 20 轮对话后观察 token

---

## 1. 上下文管理器

**文件**: `agent/context.py`

```python
@dataclass
class Turn:
    user: str
    assistant: str

class ContextManager:
    """阶梯式上下文管理器。

    三级策略（从简单到复杂，逐阶段启用）：
      Phase 1.0 — 滑动窗口截断（保留最近 N 轮）
      Phase 1.1 — LLM 摘要压缩（窗口满时压缩最早的一半）
      Phase 1.2 — 多级摘要（摘要累积到阈值后，再次压缩为"摘要的摘要"）
    """

    def __init__(self, window_size: int = 10, llm=None):
        self.window_size = window_size
        self.llm = llm                     # 压缩用 LLM（Phase 1.1 起）
        self.history: list[Turn] = []
        self.compressed_summary: str = ""  # 早期对话的压缩摘要
        self.summary_count: int = 0
        self._compression_strategy: str = "window"  # window | summary | multi

    def set_strategy(self, strategy: str) -> None:
        self._compression_strategy = strategy

    def add_turn(self, user: str, assistant: str) -> None:
        self.history.append(Turn(user, assistant))
        self._maybe_compress()

    def _maybe_compress(self) -> None:
        if len(self.history) < self.window_size:
            return

        compress_count = self.window_size // 2
        to_compress = self.history[:compress_count]
        self.history = self.history[compress_count:]

        if self._compression_strategy == "window":
            # Phase 1.0：直接丢弃最早轮次（不压缩，只是截断）
            pass
        elif self._compression_strategy == "summary" and self.llm:
            # Phase 1.1：LLM 压缩为摘要
            new_summary = self._compress_with_llm(to_compress)
            self.compressed_summary = new_summary
            self.summary_count += 1
        elif self._compression_strategy == "multi" and self.llm:
            # Phase 1.2：多级压缩
            new_summary = self._compress_with_llm(to_compress)
            if self.summary_count >= 3:
                # 已有多次摘要 → 将新摘要与旧摘要合并压缩
                combined = f"{self.compressed_summary}\n{new_summary}"
                self.compressed_summary = self._compress_with_llm([combined])
                self.summary_count = 1
            else:
                self.compressed_summary = new_summary
                self.summary_count += 1

    def _compress_with_llm(self, turns: list[Turn]) -> str:
        """调 LLM 将一段对话压缩为简洁摘要（保留关键事实和决策）。"""
        # Phase 1.1 实现：调 self.llm 做摘要
        # 后续可复用 agent/llm.py 的 build_llm()
        ...

    def build_context(self, current_message: str) -> str:
        """构建最终 prompt 字符串。"""
        parts = []
        if self.compressed_summary:
            parts.append(f"[对话摘要]\n{self.compressed_summary}\n")
        for t in self.history:
            parts.append(f"用户: {t.user}\n助手: {t.assistant}")
        parts.append(f"用户: {current_message}\n助手:")
        return "\n".join(parts)
```

---

## 2. 压缩策略演进

| 子阶段 | 策略 | 说明 |
|--------|------|------|
| Phase 1.0 | `window` | 滑动窗口，超出 window_size 的轮次直接丢弃。**这是 baseline，不做任何压缩** |
| Phase 1.1 | `summary` | 窗口满时调 LLM 压缩最早的一半轮次为自然语言摘要。信息被浓缩而非丢弃 |
| Phase 1.2 | `multi` | 摘要累积 3 次后，将多个摘要再次压缩为"摘要的摘要"，形成阶梯 |

---

## 3. Agent 类更新

```python
class Agent:
    def __init__(self, llm_cfg: LlmConfig, window_size: int = 10):
        self.llm = build_llm(llm_cfg)
        self.ctx = ContextManager(window_size=window_size, llm=self.llm)

    async def chat_stream(self, message: str) -> AsyncIterator[str]:
        context = self.ctx.build_context(message)
        full_reply = ""
        async for chunk in self.llm.astream(context):
            content = chunk.content
            if content:
                full_reply += content
                yield content
        self.ctx.add_turn(message, full_reply)
```

---

## 实现检查清单

- [ ] `agent/context.py` → `ContextManager`（三种策略）
- [ ] `agent/agent.py` → Agent 集成 ContextManager
- [ ] 验证 Phase 1.0：20 轮对话后检查 token 数被窗口限制
- [ ] 验证 Phase 1.1：观察 LLM 生成的摘要质量
- [ ] 验证 Phase 1.2：100+ 轮对话后 token 数可控