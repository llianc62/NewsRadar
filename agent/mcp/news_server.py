"""NewsRadar MCP Server — 基于 FastMCP 实现。

提供新闻查询能力，支持两种传输方式：
- stdio: 子进程管道（本地 Agent 调用）
- SSE:  HTTP 服务（外部 Agent / claude.ai 等调用）

使用方式:
    # stdio 模式（默认）
    python -m agent.mcp.news_server

    # SSE 模式（HTTP 服务）
    python -m agent.mcp.news_server --transport sse --port 8001

    # 外部 Agent 连接 SSE:
    client = MCPClient("newsradar")
    await client.connect_sse("http://localhost:8001/sse")
"""

from __future__ import annotations

import argparse
import sys

from mcp.server.fastmcp import FastMCP

from storage.postgres import PostgreSQL


# ======================================================================
# 全局状态 — 延迟初始化（FastMCP 启动后注入）
# ======================================================================
_db: PostgreSQL | None = None


def init_db(config_path: str = "config.yaml") -> None:
    """初始化数据库连接（FastMCP lifespan 中调用）。"""
    global _db
    from config.loader import load_config

    cfg = load_config(config_path)
    _db = PostgreSQL(cfg["postgresql"])
    _db.connect()


# ======================================================================
# FastMCP Server
# ======================================================================

mcp = FastMCP(
    "NewsRadar MCP Server",
    instructions=(
        "NewsRadar 新闻数据查询服务。可以搜索新闻、获取热门话题、"
        "查看新闻详情、分析情感倾向和获取来源统计。"
    ),
    host="0.0.0.0",
    port=8001,
    sse_path="/sse",
    message_path="/messages/",
)


@mcp.tool()
def search_news(query: str, limit: int = 10) -> list[dict]:
    """搜索新闻，支持关键词查询。

    Args:
        query: 搜索关键词，匹配新闻标题和摘要
        limit: 返回条数上限（默认 10，最大 50）
    """
    if _db is None or not _db.is_connected:
        raise RuntimeError("数据库未连接")

    limit = min(limit, 50)
    articles = _db.get_recent_news(search=query, limit=limit, offset=0)
    return [
        {
            "id": a.get("id"),
            "title": a.get("title", ""),
            "source": a.get("source_name", ""),
            "tier": a.get("tier"),
            "heat_score": a.get("heat_score"),
            "sentiment_score": a.get("sentiment_score"),
            "summary": a.get("summary", ""),
            "url": a.get("url", ""),
            "created_at": str(a.get("created_at", "")),
        }
        for a in articles
    ]


@mcp.tool()
def get_hot_topics(tier: str = "T1") -> list[dict]:
    """获取当前热门话题，按热度等级分类。

    Args:
        tier: 热度等级（T1=最高, T2=高, T3=中, T4=普通，默认 T1）
    """
    if _db is None or not _db.is_connected:
        raise RuntimeError("数据库未连接")

    tier_map = {"T1": 1, "T2": 2, "T3": 3, "T4": 4}
    tier_val = tier_map.get(tier, 1)

    articles = _db.get_recent_news(tier=tier_val, limit=20, offset=0)
    return [
        {
            "id": a.get("id"),
            "title": a.get("title", ""),
            "source": a.get("source_name", ""),
            "heat_score": a.get("heat_score"),
            "sentiment_score": a.get("sentiment_score"),
        }
        for a in articles
    ]


@mcp.tool()
def get_news_detail(news_id: str) -> dict | None:
    """获取单条新闻的完整内容。

    Args:
        news_id: 新闻 ID（数字字符串）
    """
    if _db is None or not _db.is_connected:
        raise RuntimeError("数据库未连接")

    article = _db.get_news_by_id(int(news_id))
    if article is None:
        return None

    return {
        "id": article.get("id"),
        "title": article.get("title", ""),
        "source": article.get("source_name", ""),
        "tier": article.get("tier"),
        "heat_score": article.get("heat_score"),
        "sentiment_score": article.get("sentiment_score"),
        "summary": article.get("summary", ""),
        "content": article.get("content", ""),
        "url": article.get("url", ""),
        "author": article.get("author", ""),
        "published_at": str(article.get("published_at", "")),
        "created_at": str(article.get("created_at", "")),
    }


@mcp.tool()
def analyze_sentiment(text: str) -> dict:
    """分析文本的情感倾向，返回 0-100 分数和标签。

    Args:
        text: 待分析的文本
    """
    positive_words = {"好", "优秀", "成功", "增长", "创新", "突破", "利好", "赞"}
    negative_words = {"差", "失败", "下跌", "危机", "问题", "风险", "利空", "崩"}
    pos_count = sum(1 for w in positive_words if w in text)
    neg_count = sum(1 for w in negative_words if w in text)
    total = pos_count + neg_count
    if total == 0:
        score = 50
    else:
        score = int(pos_count / total * 100)
    label = "正面" if score >= 67 else ("负面" if score <= 33 else "中性")
    return {"score": score, "label": label, "detail": f"正面词{pos_count}个，负面词{neg_count}个"}


@mcp.tool()
def get_source_stats(source_id: str = "") -> list[dict]:
    """获取新闻来源的统计数据。

    Args:
        source_id: 来源名称（可选，为空返回全部来源统计）
    """
    if _db is None or not _db.is_connected:
        raise RuntimeError("数据库未连接")

    all_stats = _db.get_stats()
    sources = all_stats.get("by_source", [])
    if source_id:
        sources = [s for s in sources if s.get("source_name") == source_id]
    return [
        {"source_name": s["source_name"], "count": s["cnt"]}
        for s in sources
    ]


# ======================================================================
# CLI 入口
# ======================================================================


def main() -> None:
    """入口：支持 stdio 和 SSE 两种传输模式。"""
    parser = argparse.ArgumentParser(description="NewsRadar MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse"],
        default="stdio",
        help="传输模式 (default: stdio)",
    )
    parser.add_argument("--port", type=int, default=8001, help="SSE 模式监听端口 (default: 8001)")
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="配置文件路径 (default: config.yaml)",
    )
    args = parser.parse_args()

    # 延迟初始化 DB（SSE 模式需要在 FastMCP 启动前初始化）
    init_db(args.config)

    if args.transport == "sse":
        # SSE 模式：启动 HTTP 服务
        print(f"[MCP Server] Starting SSE on http://0.0.0.0:{args.port}/sse")
        mcp.run(transport="sse")
    else:
        # stdio 模式：标准输入输出
        print("[MCP Server] Starting stdio mode", file=sys.stderr)
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
