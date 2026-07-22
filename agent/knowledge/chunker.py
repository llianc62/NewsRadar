"""文本切片器 - 按段落 + 长度切分，保留段落边界，带重叠。

零依赖实现（不引入 langchain text splitter）。char 级别度量，
``max_chars≈1200`` 对应 OpenAI embedding 的 ~512 token（混合中英文经验值）。
"""

from __future__ import annotations

import re

# 段落边界：空行（含空白）
_PARA_SPLIT = re.compile(r"\n\s*\n")


def chunk_text(
    text: str,
    max_chars: int = 1200,
    overlap: int = 200,
) -> list[str]:
    """将长文本切成 ~``max_chars`` 的片段，带 ``overlap`` 重叠。

    策略:
        1. 按空行切成段落；
        2. 贪心地把段落拼进当前片段，直到超过 ``max_chars``；
        3. 超长段落（单段 > ``max_chars``）按 ``max_chars`` 硬切；
        4. 新片段以 ``overlap`` 字符的尾部作为前缀，跨片段上下文连续。

    Args:
        text: 原始文本。
        max_chars: 单片段字符上限。
        overlap: 相邻片段重叠字符数（``0`` 关闭重叠）。

    Returns:
        非空片段列表；输入为空返回 ``[]``。
    """
    if not text or not text.strip():
        return []
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    if overlap < 0 or overlap >= max_chars:
        raise ValueError("overlap must be in [0, max_chars)")

    paragraphs = [p.strip() for p in _PARA_SPLIT.split(text) if p.strip()]
    chunks: list[str] = []
    current = ""

    for para in paragraphs:
        # 超长段落：硬切成 max_chars 片段，带 overlap
        if len(para) > max_chars:
            if current:
                chunks.append(current.strip())
                current = ""
            step = max_chars - overlap
            for i in range(0, len(para), step):
                piece = para[i : i + max_chars].strip()
                if piece:
                    chunks.append(piece)
            continue

        # 拼接会超限 -> 落盘当前片段，带 overlap 开新片段
        if current and len(current) + len(para) + 1 > max_chars:
            chunks.append(current.strip())
            current = (current[-overlap:] + "\n" + para) if overlap else para
        else:
            current = f"{current}\n{para}" if current else para

    if current:
        chunks.append(current.strip())

    return [c for c in chunks if c]
