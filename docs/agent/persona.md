# 角色扮演 Agent + 多角色编排

> **父文档**: [index.md](index.md)
> **状态**: Phase B/C 已完成（2026-07-14）。仿 ai-hedge-fund `LLMAgent` + `analyst_signals` + portfolio_manager 聚合模式。
> **依赖**: [Phase 3 知识库](phase3-knowledge.md)（角色的专业知识来源）
> **可验证**:
> - Phase B：与单个角色对话，见人格声音 + 知识注入生效
> - Phase C：提问，N 个角色并行分析，主编综合答复

---

## 1. 设计思路

### 1.1 角色扮演的"配方"（来自 ai-hedge-fund）

一个角色 agent = ① **人格 system prompt**（哲学/声音/checklist）+ ② **可选硬编码领域逻辑**（确定性"专业知识"计算，LLM 只叙事）+ ③ **共享知识库**（所有角色读写同一份状态）。

铁律（ai-hedge-fund VISION）："The LLM never touches the trade"--LLM 形成 view + 叙事，确定性代码做决策。NewsRadar 借鉴：硬编码逻辑（JiebaAnalyzer 情感/关键词、热度异常检测）产出结构化事实，LLM 用角色声音叙事。

### 1.2 两条角色路线（用户确认：都要）

**新闻分析专家视角**（贴合 NewsRadar 场景）：
| 角色 | 视角 | 硬编码专业逻辑 |
|------|------|----------------|
| 宏观经济分析师 | 利率/GDP/央行/通胀政策 | search_news 关键词聚合 |
| 舆情/情感分析师 | 市场情绪极端/转向 | `JiebaAnalyzer.analyze_sentiment`（11k 词典） |
| 行业研究员 | 行业趋势/赛道 | `JiebaAnalyzer.analyze_keywords`（TF-IDF） |
| 事实核查员 | 来源可信度/交叉验证 | tier 加权可信度评分 |
| 黑天鹅风险视角 | 尾部风险/异常 | `heat_score>70 && sentiment<20` 异常检测 |

**投资人视角看新闻**（最贴近 ai-hedge-fund）：
| 角色 | 哲学 | 看新闻的 checklist |
|------|------|-------------------|
| 巴菲特 | 价值/护城河/长期持有 | 这条新闻对护城河/能力圈/持有十年的含义 |
| 格雷厄姆 | 安全边际/低估值 | 估值折扣/财务强度/防御性 |
| Taleb | 黑天鹅/反脆弱 | 尾部风险/不对称回报/脆弱性 |
| Cathie Wood | 颠覆式创新/成长 | 突破性技术/指数级增长/颠覆信号 |

**关键差异**：ai-hedge-fund 角色有结构化财务数据（EPS/BVPS/市值）；NewsRadar 角色只有新闻文本 + 派生分数（heat/sentiment/tags）。硬编码逻辑是文本分析与分数启发式，非财务公式。

---

## 2. PersonaAgent 基类

**新建 `agent/persona/base.py`**。`PersonaAgent` **继承 `DefaultAgent`**（已是正确形态：持有 brain/executor/memory/tools），不包装、不 peer。

### 2.1 DefaultAgent 小重构

在 `DefaultAgent`（`agent/agent.py`）抽出 `_make_ctx()` 方法，`chat()` 和 `chat_stream()` 共用。消除"override chat 不 override chat_stream 会丢知识注入"陷阱：

```python
class DefaultAgent:
    async def _make_ctx(self, user_input, session_id, model_name) -> Context:
        return Context(
            user_input=user_input, session_id=session_id,
            system_prompt=self.system_prompt,
            model_name=model_name or "default",
            running_mode=self._running_mode,
        )

    async def chat(self, user_input, session_id="", model_name="") -> AgentResult:
        ctx = await self._make_ctx(user_input, session_id, model_name)
        result_text = await self.executor.run(ctx=ctx, brain=self.brain, memory=self.memory, tools=self.tools)
        return AgentResult(...)
```

### 2.2 Context 加 persona_name

`agent/data.py` 的 `Context` 加 `persona_name: str = ""`（向后兼容，纯 dataclass 默认值）。`knowledge_context` 字段已存在（:20）。

### 2.3 PersonaAgent

```python
class PersonaAgent(DefaultAgent):
    """角色 agent：name + get_system_prompt() + 知识库检索。仿 ai-hedge-fund LLMAgent。"""

    def __init__(self, config, *, persona_name, knowledge=None, kb_namespace="",
                 system_prompt="", executor=None, memory=None, tools=None,
                 running_mode="normal"):
        super().__init__(config=config, executor=executor, memory=memory,
                         system_prompt=system_prompt, tools=tools, running_mode=running_mode)
        self.persona_name = persona_name
        self._knowledge = knowledge          # KnowledgeEngine | None
        self.kb_namespace = kb_namespace     # 检索的命名空间

    def get_system_prompt(self) -> str:
        """人格声音--每个子类定义。仿 LLMAgent.get_system_prompt()。"""
        return self.system_prompt

    async def _make_ctx(self, user_input, session_id, model_name) -> Context:
        ctx = await super()._make_ctx(user_input, session_id, model_name)
        ctx.persona_name = self.persona_name
        ctx.system_prompt = self.get_system_prompt()
        if self._knowledge and self.kb_namespace:
            ctx.knowledge_context = await self._to_thread(
                self._knowledge.retrieve_render, user_input, self.kb_namespace
            )
        return ctx
```

### 2.4 人格 prompt 结构（仿 v2 buffett.py）

每个角色子类只需定义 `name` + `get_system_prompt()`。prompt 含：身份声明 -> checklist -> 输出格式。例：

```python
class BuffettPersona(PersonaAgent):
    def __init__(self, config, **kw):
        super().__init__(config, persona_name="buffett",
                         kb_namespace="investing/buffett", **kw)
    def get_system_prompt(self) -> str:
        return """你是沃伦·巴菲特，以长期企业所有者而非交易者的视角评估新闻。

逐条 checklist：
1. 能力圈 - 这条新闻涉及的业务你能理解吗？
2. 护城河 - 是否涉及持久竞争优势、定价权？
...
输出 JSON：{"stance":"看多"|"看空"|"中性", "confidence":0-100, "reasoning":"..."}"""
```

---

## 3. 硬编码专业逻辑（仿 ben_graham 的 Graham Number）

ai-hedge-fund `ben_graham.py` 把 Graham Number 编码成 Python，LLM 只叙事。NewsRadar 对应物：

- **舆情角色**：import `JiebaAnalyzer`（`news/analyzer/jieba.py:150`，11k 词典）做硬编码情感，而非 MCP `analyze_sentiment` 玩具（8 词，`news_server.py:154`）。
- **行业角色**：`JiebaAnalyzer.analyze_keywords()`（TF-IDF/TextRank）。
- **黑天鹅角色**：`heat_score > 70 && sentiment_score < 20` 异常检测（阈值常量 `constants.py:64-66`：>=67 利好，<=33 利空）。

硬编码逻辑产出结构化事实（分数/标签），LLM 用角色声音解释。可叠加在 `get_system_prompt` 之外，作为 `_pre_analyze(user_input) -> dict` 钩子注入 user prompt（仿 ben_graham 的 `analysis_data`）。

> **注意**：MCP `analyze_sentiment` 已于 Phase D 修为路由真 `JiebaAnalyzer`（`news_server.py`，分析器不可用时兜底极简词典），与抓取管线同口径。

---

## 4. PersonaRegistry（仿 ANALYST_CONFIG）

**新建 `agent/persona/registry.py`**，仿 ai-hedge-fund `src/utils/analysts.py` 的 `ANALYST_CONFIG`：

```python
@dataclass
class PersonaSpec:
    name: str                    # "buffett"
    display_name: str            # "巴菲特"
    description: str             # "价值/护城河"
    category: str                # "expert" | "investor"
    kb_namespace: str            # "investing/buffett"
    factory: Callable            # -> PersonaAgent
    order: int

PERSONA_REGISTRY: dict[str, PersonaSpec] = {
    "buffett": PersonaSpec("buffett", "巴菲特", "价值/护城河/长期持有", "investor", ...),
    "graham": ...,
    "taleb": ...,
    "wood": ...,
    "macro": PersonaSpec("macro", "宏观经济", "...", "expert", ...),
    "sentiment": ...,
    "industry": ...,
    "factcheck": ...,
    "blackswan": ...,
}
```

前端右侧团队面板从此 registry 渲染列表（名称 + description + category 分组）。

---

## 5. PersonaOrchestrator（多角色并行 + 主编聚合）

**新建 `agent/persona/orchestrator.py`**，仿 ai-hedge-fund `start -> N 角色并行 -> portfolio_manager 聚合`：

```
用户消息
  │
  ├─ Phase 1（asyncio.gather 并行 fan-out）────────────┐
  │   每个选中角色独立跑 PersonaAgent.chat()：           │
  │     - 知识库检索（各自 namespace）                    │
  │     - 人格 system prompt                            │
  │     - ReAct 循环（可调 MCP news 工具）               │
  │     - 产出结构化 PersonaSignal                       │
  │   -> 写入 analyst_signals[persona_name]（共享 dict） │
  ├────────────────────────────────────────────────────┘
  │
  ├─ Phase 2（主编聚合）──────────────────────────────┐
  │   "主编"角色读 analyst_signals（所有角色观点）      │
  │     -> DirectExecutor 真流式 -> 逐 token 推 WebSocket│
  ├────────────────────────────────────────────────────┘
  ▼
客户端：主编综合答复（流式）+ 各角色 PersonaSignal 摘要
```

### 5.1 结构化输出 PersonaSignal

仿 ai-hedge-fund `BenGrahamSignal`（Pydantic）：

```python
class PersonaSignal(BaseModel):
    persona: str
    stance: Literal["看多", "看空", "中性"]
    confidence: float = Field(ge=0, le=100)
    reasoning: str
```

各角色 LLM 输出此结构（JSON 解析，仿 v2 `extract_json`）。主编读全部 signals 综合时序。

### 5.2 共享 analyst_signals

仿 ai-hedge-fund `state["data"]["analyst_signals"][agent_id]`，每角色写自己 key，无冲突。主编读累积 dict。

### 5.3 流式策略

**坑**：`ReActExecutor.run_stream`（`executor.py:273`）是假流式（整段 yield）。方案：
- Phase 1 角色用 ReAct **非流式**并行跑（静默，前端面板显示"思考中"状态）
- Phase 2 主编用 `DirectExecutor`（`executor.py:143` 真流式）逐 token 推

用户看到主编实时输出，角色分析在后台并发。

### 5.4 主编角色

固定一个"新闻主编"综合角色（type="editor"），config 可覆盖 prompt。读所有 PersonaSignal，综合成最终答复，标注引用了哪些角色的观点。

---

## 6. 前端：右侧团队面板（用户确认）

当前 `web/templates/pages/agent_chat.html` 是单列 chat，header 有 运行模式(Strict/Normal/Loose) + 模型(Quick/Deep) 分段控件。chat 只发 `{type:'chat', message, session_id, model, running_mode}`，`web/agent.py:160` 写死单 `agent_instance`。

### 改动

- **布局**：`app-main > chat-container` 单列 -> 双列（左 chat，右 团队面板）
- **右面板**：从 `PersonaRegistry` 渲染角色列表（名称 + description + category 分组：投资视角 / 专家视角）
  - **单选** = 单角色对话模式（Phase B 测试）：选中一个角色，chat 用该 PersonaAgent
  - **多选 + [团队会诊] 按钮** = orchestrator 模式（Phase C）：选中 N 个角色 + 主编
  - "默认助手"选项 = 原 DefaultAgent（向后兼容）
- **WebSocket 协议扩展**：chat 消息加 `persona: "buffett"`（单角色）或 `personas: ["buffett","sentiment"]`（团队会诊）
- **`web/agent.py:160`**：优先用 `app.state.persona_orchestrator` / persona registry，按消息里的 persona(s) 路由；降级单 agent_instance
- **Phase C 面板状态**：团队会诊时，面板内实时显示各角色状态（思考中/完成）+ 完成后展示其 PersonaSignal（立场/信心/理由摘要）

---

## 7. 接线

### main.py（`main.py:241`）

```python
agent = None
orchestrator = None
if self.config.get("models"):
    from agent.factory import create_agent, create_persona_orchestrator
    agent = await create_agent(self.config["models"], system_prompt="...", register_mcp=True)
    if self.config.get("personas"):
        knowledge = build_knowledge_engine(self.config)   # 若 knowledge.enabled
        orchestrator = await create_persona_orchestrator(
            self.config["models"], knowledge, persona_names=None  # None=全部
        )
app = create_app(..., agent_instance=agent, persona_orchestrator=orchestrator)
```

### web/app.py

`app.state.persona_orchestrator = orchestrator`（若非 None）。`PersonaRegistry` 也挂 `app.state.persona_registry` 供前端 `/api/agent/personas` 拉取。

### config（`config.py`）

加 `_load_personas_config()`：list of `{name, type, model, enabled, kb_namespace, custom_prompt}`。注册到 `config = {...}` dict。

---

## 8. 与 ai-hedge-fund 的对应

| ai-hedge-fund | NewsRadar |
|---|---|
| `LLMAgent` 基类（持有全部机制） | `PersonaAgent(DefaultAgent)`（继承复用 brain/executor/memory/tools） |
| persona = `name` + `get_system_prompt()` | 同 |
| `ben_graham.analyze_valuation_graham`（Graham Number 硬编码） | `JiebaAnalyzer.analyze_sentiment/keywords` + 热度异常（硬编码） |
| `analyst_signals` 共享 dict | `PersonaOrchestrator.analyst_signals` 共享 dict |
| `portfolio_manager`（LLM 聚合） | "主编"角色（LLM 聚合） |
| `BenGrahamSignal`（Pydantic） | `PersonaSignal`（Pydantic） |
| LangGraph `asyncio` 并行 fan-out | `asyncio.gather` 并行 fan-out（不引入 LangGraph） |
| `progress` Rich 表（各 agent 状态） | 前端右面板实时状态 |

---

## 9. 范围控制（不做）

- 不引入 LangGraph（asyncio.gather 够用，避免重依赖）
- 不做实时多角色流式（v1 主编流式 + 角色静默并行）
- 不做向量库迁移工具（pgvector 起步够用）

---

## 实现检查清单

### Phase B（单角色）
- [x] `agent/data.py`：`Context` 加 `persona_name`
- [x] `agent/agent.py`：抽 `_make_ctx()` 方法
- [x] `agent/executor.py`：两个 `_build_messages` 加 `## 知识库` 块
- [x] `agent/persona/base.py`：`PersonaAgent(DefaultAgent)`
- [x] 种子角色：`experts/sentiment.py` + `investors/buffett.py`
- [x] `agent/persona/registry.py`：`PersonaRegistry`
- [x] `agent/factory.py`：`create_persona()`
- [x] 前端：右面板雏形 + 单选路由 + WebSocket `persona` 字段
- [x] `tests/test_persona_agent.py`：MockClient 验证人格 + 知识注入

### Phase C（多角色编排）
- [x] `agent/persona/orchestrator.py`：fan-out + 主编聚合 + analyst_signals
- [x] `PersonaSignal` Pydantic 结构化输出
- [x] 补齐角色：experts(macro/industry/factcheck/blackswan) + investors(graham/taleb/wood) + 主编
- [x] `agent/factory.py`：`create_persona_orchestrator()`
- [x] `main.py` + `web/app.py`：挂 `app.state.persona_orchestrator`
- [x] 前端：多选 + 团队会诊 + 面板实时状态
- [x] `config.py`：`_load_personas_config()`
- [x] `tests/test_persona_orchestrator.py`：mock 多角色 fan-out + 聚合

> Phase B/C 完成（2026-07-14）：角色模块覆盖率 96-100%，全量单测通过。10 个角色（4 投资人 + 5 专家 + 主编），单选单角色直答、多选团队会诊（并行 fan-out 信号 + 主编 DirectExecutor 真流式聚合）。Phase D 顺带修复 MCP `analyze_sentiment` 路由真分析器。
