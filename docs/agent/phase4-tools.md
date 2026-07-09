# Phase 4：工具调用 / MCP

> **父文档**: [index.md](index.md)  
> **产出**: 外部工具调用  
> **可验证**: 自主决定调工具

---

## 1. 内置工具

先实现几个简单工具用于验证：

| 工具 | 说明 |
|------|------|
| `get_current_time` | 返回当前时间 |
| `search_news` | 搜索 NewsRadar 数据库中的新闻 |
| `get_stock_price` | 获取股票价格（占位） |
| `search_knowledge` | 知识库检索（Phase 3 → 4 迁移） |

---

## 2. LangChain tool 绑定

利用 `langchain-anthropic`/`langchain-openai` 的 `bind_tools()` 原生支持：

```python
from langchain_core.tools import tool

tools = [get_current_time, search_news, get_stock_price]
llm_with_tools = self.llm.bind_tools(tools)
```

---

## 3. ReAct 循环

```python
async def chat_stream(self, message: str) -> AsyncIterator[str]:
    context = self.ctx.build_context(message)
    reply = await self.llm.bind_tools(tools).ainvoke(context)

    while reply.tool_calls:
        for tc in reply.tool_calls:
            tool_result = tools_by_name[tc["name"]].invoke(tc["args"])
            context += f"\n工具返回: {tool_result}"
        reply = await self.llm.bind_tools(tools).ainvoke(context)

    yield reply.content
```

---

## 4. MCP（预留）

MCP 是 Phase 4 的后续扩展点，用于将外部服务包装为标准工具接口。当前不在 Scope 内。

---

## 实现检查清单

- [ ] 内置工具：`get_current_time`, `search_news`, `get_stock_price`, `search_knowledge`
- [ ] `agent/agent.py` → Agent 集成 `bind_tools()` + ReAct 循环
- [ ] WS 协议：`tool_call` 消息推送工具调用可见性
- [ ] 验证：Agent 自主决定调工具并返回结果