"""Embedding 客户端 - OpenAI 兼容 ``/v1/embeddings``。

代码库原有 ``ModelHub``/``BaseClient`` 只封装 chat，无 embedding。
DeepSeek 不提供 embedding 端点，故知识库配置独立 base_url/api_key/model
（见 ``config.yaml`` 的 ``knowledge`` 段）。
"""

from __future__ import annotations


class EmbeddingClient:
    """OpenAI 兼容 ``/v1/embeddings`` 客户端。

    包装同步 openai SDK（与 ``OpenAIClient.chat`` 同款阻塞调用）。
    model 在构造时确定（embedding 模型通常固定，不像 chat 可切换）。

    Args:
        api_key: API 密钥（必填，构造时校验）。
        base_url: 兼容服务地址；空串走 OpenAI 默认。
        model: embedding 模型名，默认 ``text-embedding-3-small``（1536 维）。
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "",
        model: str = "text-embedding-3-small",
    ):
        if not api_key:
            raise ValueError("EmbeddingClient 需要 api_key")
        from openai import OpenAI

        self.model = model
        self._client = OpenAI(api_key=api_key, base_url=base_url or None)

    def embed(self, texts: list[str], batch_size: int = 64) -> list[list[float]]:
        """批量 embedding。返回与 ``texts`` 等长的向量列表（顺序一致）。

        OpenAI 单次请求上限 2048 条；这里按 ``batch_size`` 分批避免超限，
        并对每批结果按 ``index`` 排序以防乱序。
        """
        if not texts:
            return []
        out: list[list[float]] = []
        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]
            resp = self._client.embeddings.create(model=self.model, input=batch)
            data = sorted(resp.data, key=lambda d: d.index)
            out.extend(d.embedding for d in data)
        return out
