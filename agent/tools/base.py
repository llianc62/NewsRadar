"""Base tool abstractions — ToolDef, ToolCall, BaseTool, FunctionTool, @tool decorator."""

from __future__ import annotations

import asyncio
import inspect
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Union, get_args, get_origin, get_type_hints


@dataclass(frozen=True)
class ToolDef:
    """工具定义——用于生成 LLM 可识别的 tool schema。

    Attributes:
        name: 工具名（必须唯一）
        description: 描述（LLM 通过描述决定何时调此工具）
        input_schema: JSON Schema，描述工具接受的参数
    """
    name: str
    description: str
    input_schema: dict = field(default_factory=dict)
    level: int = 1


@dataclass
class ToolCallRecord:
    """一次工具调用的记录——存入 Context.tool_calls。"""
    name: str
    args: dict
    result: str = ""
    error: str = ""


class BaseTool(ABC):
    """所有工具的基类。"""

    @property
    @abstractmethod
    def category(self) -> str:
        """工具分类，如 "news"、"finance"、"general"、"mcp:<server>"。"""
        ...

    @property
    @abstractmethod
    def level(self) -> int:
        """工具危险等级 1-4。"""
        ...

    @abstractmethod
    def get_def(self) -> ToolDef:
        """返回工具定义（name + description + input_schema）。"""
        ...

    @abstractmethod
    async def execute(self, **kwargs: Any) -> str:
        """执行工具调用，返回文本结果。"""
        ...


class FunctionTool(BaseTool):
    """内置函数工具——包装一个纯 Python 函数。

    无需外部服务，零网络开销。适用于：
    - 计算器、时间、日期等无状态工具
    - 直接操作项目内部数据（DB 查询、文件读取）

    同步函数通过 asyncio.to_thread 桥接到异步调用。
    """

    def __init__(
        self,
        name: str,
        description: str,
        fn: Callable[..., Any],
        input_schema: dict,
        level: int = 1,
        category: str = "general",
    ):
        if not name.strip():
            raise ValueError("name must not be empty")
        if not description.strip():
            raise ValueError("description must not be empty")
        self._name = name
        self._description = description
        self._fn = fn
        self._schema = input_schema
        self._level = level
        self._category = category

    @property
    def category(self) -> str:
        return self._category

    @property
    def level(self) -> int:
        return self._level

    def get_def(self) -> ToolDef:
        return ToolDef(self._name, self._description, self._schema, level=self._level)

    async def execute(self, **kwargs: Any) -> str:
        try:
            if asyncio.iscoroutinefunction(self._fn):
                result = await self._fn(**kwargs)
            else:
                result = await asyncio.to_thread(self._fn, **kwargs)
            return str(result)
        except Exception as e:
            return f"Error executing tool '{self._name}': {e}"

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        """直接调用被包装的函数（绕过 asyncio 包装）。"""
        return self._fn(*args, **kwargs)


# ── @tool 装饰器 ──────────────────────────────────────────────────


_PYTHON_TYPE_TO_JSON = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    type(None): "null",
}


def _type_to_json_schema(tp: Any) -> dict:
    """将 Python 类型注解转换为 JSON Schema 片段。"""
    origin = get_origin(tp)

    # Optional[X] = Union[X, None]
    if origin is Union or (origin is not None and getattr(origin, "_name", None) == "Optional"):
        args = get_args(tp)
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            schema = _type_to_json_schema(non_none[0])
            schema["nullable"] = True
            return schema
        return {"anyOf": [_type_to_json_schema(a) for a in args]}

    # Literal["a", "b"]
    if origin is not None and getattr(origin, "_name", None) == "Literal":
        values = get_args(tp)
        json_type = "string"
        for v in values:
            if isinstance(v, int):
                json_type = "integer"
            elif isinstance(v, float):
                json_type = "number"
            elif isinstance(v, bool):
                json_type = "boolean"
            elif v is None:
                json_type = "null"
        return {"type": json_type, "enum": list(values)}

    # list[X], dict[str, X]
    if origin is list:
        args = get_args(tp)
        items = _type_to_json_schema(args[0]) if args else {}
        return {"type": "array", "items": items}
    if origin is dict:
        return {"type": "object"}
    if origin is tuple:
        return {"type": "array"}

    # 基础类型
    json_type = _PYTHON_TYPE_TO_JSON.get(tp)
    if json_type:
        return {"type": json_type}

    # fallback
    return {}


def _parse_docstring_params(docstring: str) -> dict[str, str]:
    """从 Google 风格 docstring 的 Args: 节提取参数描述。"""
    if not docstring:
        return {}
    params: dict[str, str] = {}
    # 匹配 Args: 节下面的参数行:  name: description
    in_args = False
    for line in docstring.split("\n"):
        stripped = line.strip()
        if stripped.startswith("Args:") or stripped.startswith("Parameters:"):
            in_args = True
            continue
        if in_args:
            # 遇到空行或另一个 section 标题 → 结束
            if not stripped or stripped.endswith(":") and not stripped.startswith(" "):
                in_args = False
                continue
            m = re.match(r"^(\w+):\s*(.+)$", stripped)
            if m:
                params[m.group(1)] = m.group(2).strip()
    return params


def tool(
    fn: Callable | None = None,
    *,
    name: str | None = None,
    description: str | None = None,
    level: int = 1,
    category: str = "general",
) -> FunctionTool | Callable[[Callable], FunctionTool]:
    """将函数转换为 FunctionTool，自动从类型注解生成 JSON Schema。

    用法:
        @tool
        def get_time() -> str:
            \"\"\"获取当前时间。\"\"\"
            ...

        @tool(description="自定义描述")
        def search(q: str, limit: int = 10) -> str:
            \"\"\"搜索。\"\"\"
            ...

        @tool(name="custom_name")
        def my_func(x: int) -> str: ...

        @tool(level=2)
        def search_news(q: str) -> str:
            \"\"\"搜索新闻。\"\"\"
            ...

        @tool(category="news")
        def get_hot_topics() -> str:
            \"\"\"获取热点话题。\"\"\"
            ...
    """
    if fn is not None:
        # 无参数调用: @tool
        return _make_tool(fn, name=name, description=description, level=level, category=category)

    # 带参数调用: @tool(...)
    def decorator(f: Callable) -> FunctionTool:
        return _make_tool(f, name=name, description=description, level=level, category=category)

    return decorator


def _make_tool(fn: Callable, *, name: str | None = None, description: str | None = None, level: int = 1, category: str = "general") -> FunctionTool:
    """将函数包装为 FunctionTool。"""
    tool_name = name or fn.__name__

    # 从 docstring 取 description
    doc = inspect.getdoc(fn) or ""
    if description:
        tool_desc = description
    elif doc:
        tool_desc = doc.split("\n\n")[0].strip()  # 第一段
    else:
        tool_desc = tool_name.replace("_", " ").strip()

    # 解析参数描述（从 Args: 节）
    doc_params = _parse_docstring_params(doc)

    # 自动生成 JSON Schema
    sig = inspect.signature(fn)
    hints = get_type_hints(fn) if hasattr(fn, "__annotations__") else {}

    properties: dict[str, dict] = {}
    required: list[str] = []

    for param_name, param in sig.parameters.items():
        if param_name == "return":
            continue
        if param_name.startswith("_"):
            continue

        # 类型
        param_type = hints.get(param_name, str)
        prop = _type_to_json_schema(param_type)

        # 参数描述
        if param_name in doc_params:
            prop["description"] = doc_params[param_name]

        # 默认值
        if param.default is not inspect.Parameter.empty:
            if "nullable" not in prop:
                prop["default"] = param.default if not isinstance(param.default, type) else None
        else:
            required.append(param_name)

        properties[param_name] = prop

    input_schema = {"type": "object", "properties": properties}
    if required:
        input_schema["required"] = required

    return FunctionTool(name=tool_name, description=tool_desc, fn=fn, input_schema=input_schema, level=level, category=category)
