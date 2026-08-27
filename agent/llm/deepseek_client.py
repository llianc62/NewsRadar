"""DeepSeek Client（基于 ``langchain-deepseek`` 官方包）。

使用官方 ``ChatDeepSeek`` 作为基类，获得 DeepSeek 专属的响应处理（含
``reasoning_content`` 响应侧提取）+ 原生 function calling。再叠加一点补丁：

- **``reasoning_content`` 请求侧回传**：``ChatDeepSeek`` 只在响应侧把
  ``reasoning_content`` 提取进 ``AIMessage.additional_kwargs``（``_create_chat_result``），
  但请求侧**不**回传。思考模式下若上轮有工具调用，``reasoning_content`` 必须原样
  回传否则 HTTP 400。故 override ``_get_request_payload`` 从 ``additional_kwargs``
  注入回 outgoing message。

思考模式经 ``extra_body={"thinking":{"type":"enabled"}}`` 显式开启
（参考 https://api-docs.deepseek.com/zh-cn/guides/thinking_mode）。

注：``deepseek-v4-flash`` 的官方 model profile 声明 ``tool_calling: True``，
走 ``bind_tools`` 直接返回结构化 ``tool_calls``，无需文本兜底解析。
"""

from __future__ import annotations

from typing import Any

from langchain_deepseek import ChatDeepSeek

from .base_client import BaseClient


class DeepSeekClient(BaseClient, ChatDeepSeek):
    """DeepSeek client：``ChatDeepSeek`` + reasoning 回传补丁。"""

    def __init__(
        self,
        api_key: str = "",
        base_url: str = "",
        model: str = "",
        thinking: str = "enabled",
        **kwargs: Any,
    ) -> None:
        extra_body = kwargs.pop("extra_body", None) or {}
        if thinking:
            extra_body.setdefault("thinking", {"type": thinking})
        kwargs["extra_body"] = extra_body
        # 自定义 base_url 时 ChatDeepSeek 默认不回传流式 usage,显式开启
        kwargs.setdefault("stream_usage", True)
        super().__init__(
            api_key=api_key or None,
            base_url=base_url or None,
            model=model,
            **kwargs,
        )

    def _get_request_payload(self, input_: Any, *, stop: list[str] | None = None, **kwargs: Any) -> dict:
        """请求侧回传 ``reasoning_content``（ChatDeepSeek 不做，需自己注入）。

        ChatDeepSeek 的 ``_create_chat_result`` 已在响应侧把 ``reasoning_content``
        提取进 ``AIMessage.additional_kwargs``；此处成对地在请求侧把它注入回
        outgoing message，满足思考模式多轮回传要求。
        """
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        try:
            from langchain_core.messages import AIMessage, convert_to_messages

            input_msgs = (
                input_ if isinstance(input_, list) else convert_to_messages(input_)
            )
        except Exception:
            return payload

        for msg_dict, msg in zip(payload.get("messages", []), input_msgs):
            if isinstance(msg, AIMessage):
                rc = (msg.additional_kwargs or {}).get("reasoning_content")
                if rc is not None:
                    msg_dict["reasoning_content"] = rc
        return payload
