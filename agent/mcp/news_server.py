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
    client = await MCPClient.connect_sse("http://localhost:8001/sse")
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime

from mcp.server.fastmcp import FastMCP

from news.constants import SENTIMENT_NEGATIVE_THRESHOLD, SENTIMENT_POSITIVE_THRESHOLD
from storage.postgres import PostgreSQL
from utils import format_date_today


# ======================================================================
# 全局状态 — 延迟初始化（FastMCP 启动后注入）
# ======================================================================
_db: PostgreSQL | None = None
_analyzer = None  # news.analyzer.Analyzer | None，复用抓取管线的 jieba 分析器


def _sentiment_label(score: int) -> str:
    """0-100 情感分 -> 标签，与 constants.py 阈值口径一致。"""
    if score >= SENTIMENT_POSITIVE_THRESHOLD:
        return "正面"
    if score <= SENTIMENT_NEGATIVE_THRESHOLD:
        return "负面"
    return "中性"


def init_db(config_path: str = "config/config.yaml") -> None:
    """初始化数据库连接（FastMCP lifespan 中调用）。"""
    global _db, _analyzer
    from config import load_config
    from news.analyzer import create_analyzer

    cfg = load_config(config_path)
    _db = PostgreSQL(cfg["postgresql"])
    _db.connect()
    # 复用抓取管线的 JiebaAnalyzer，保证 MCP 工具与入库情感分同口径
    try:
        _analyzer = create_analyzer(cfg, db=_db)
    except Exception as exc:  # 分析器初始化失败不应阻断 MCP 服务
        print(f"[MCP Server] analyzer init failed, fall back to mini dict: {exc}", file=sys.stderr)
        _analyzer = None


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
    """搜索新闻，支持关键词查询。搜索全量历史数据，无时间限制。

    Args:
        query: 搜索关键词，匹配新闻标题和摘要
        limit: 返回条数上限（默认 10，最大 50）
    """
    if _db is None or not _db.is_connected:
        raise RuntimeError("数据库未连接")

    limit = min(limit, 50)
    articles = _db.search_news(query=query, limit=limit, offset=0)
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
def get_hot_topics(tier: str = "T1", sort_by: str = "heat", date: str = "",
                   limit: int = 30, offset: int = 0) -> dict:
    """获取指定日期的热点新闻，按热度或情绪排序，支持分页。

    返回 {"items": [...], "total": N, "next_offset": M}：items 是本批新闻，
    total 是当日该等级匹配总数，next_offset 是下一批起始偏移（没有更多时为 null）。

    Args:
        tier: 热度等级（T1=最高, T2=高, T3=中, T4=普通, all=全部等级，默认 T1）
        sort_by: 排序方式（heat=热度, sentiment=情绪利好在前, time=抓取时间，默认 heat）
        date: 日期（YYYY-MM-DD，默认今天）
        limit: 返回条数上限（默认 30，最大 50）
        offset: 起始偏移量（翻页用，默认 0）
    """
    if _db is None or not _db.is_connected:
        raise RuntimeError("数据库未连接")

    tier_map = {"T1": 1, "T2": 2, "T3": 3, "T4": 4, "all": None}
    try:
        tier_val = tier_map[tier]
    except KeyError:
        raise ValueError(f"Unsupported tier: {tier!r} (supported: {', '.join(tier_map)})")

    order_map = {"heat": "heat", "sentiment": "sentiment", "time": "created"}
    try:
        order_by = order_map[sort_by]
    except KeyError:
        raise ValueError(f"Unsupported sort_by: {sort_by!r} (supported: {', '.join(order_map)})")

    if not date:
        date = format_date_today()
    else:
        try:
            datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            raise ValueError(f"Invalid date: {date!r} (expected YYYY-MM-DD)")

    limit = max(1, min(limit, 50))
    offset = max(0, offset)
    articles = _db.get_recent_news(
        tier=tier_val, limit=limit, offset=offset,
        date_from=date, date_to=date,
        order_by=order_by,
    )
    # RealDictCursor 对 NULL 列返回 None（.get 默认值不生效），统一兜底为空值
    items = [
        {
            "id": a.get("id"),
            "title": a.get("title") or "",
            "summary": a.get("summary") or "",
            "source": a.get("source_name") or "",
            "tier": a.get("tier"),
            "tags": a.get("tags") or [],
            "heat_score": a.get("heat_score"),
            "sentiment_score": a.get("sentiment_score"),
            "url": a.get("url") or "",
            "published_at": str(a.get("published_at") or ""),
        }
        for a in articles
    ]
    # 分页元数据：total 用与 get_recent_news 相同的过滤口径统计，
    # next_offset 由代码计算，agent 只照抄、不自己算，避免漂移。
    total = _db.get_news_count(
        tier=tier_val, date_from=date, date_to=date,
    )
    next_offset = offset + limit if offset + limit < total else None
    # total/next_offset 前置：即使 items 超长被展平层截断，分页元数据也不丢
    return {"total": total, "next_offset": next_offset, "items": items}


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
    if not text or not text.strip():
        return {"score": 50, "label": "中性", "detail": "空文本"}

    # 优先复用抓取管线的 JiebaAnalyzer（jieba 分词 + 4 词典 + tanh 映射）
    if _analyzer is not None:
        item = {"title": "", "content": text, "sentiment_score": 50}
        try:
            _analyzer.analyze_sentiment([item])
        except Exception:
            pass
        score = int(item.get("sentiment_score", 50))
        return {
            "score": score,
            "label": _sentiment_label(score),
            "detail": "基于 jieba 词典 + tanh 映射（与抓取管线同口径）",
        }

    # 兜底：分析器未启用时的极简词典
    positive_words = {"好", "优秀", "成功", "增长", "创新", "突破", "利好", "赞"}
    negative_words = {"差", "失败", "下跌", "危机", "问题", "风险", "利空", "崩"}
    pos_count = sum(1 for w in positive_words if w in text)
    neg_count = sum(1 for w in negative_words if w in text)
    total = pos_count + neg_count
    score = int(pos_count / total * 100) if total else 50
    return {
        "score": score,
        "label": _sentiment_label(score),
        "detail": f"正面词{pos_count}个，负面词{neg_count}个（兜底词典）",
    }


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
        default="config/config.yaml",
        help="配置文件路径 (default: config/config.yaml)",
    )
    args = parser.parse_args()

    # 延迟初始化 DB（SSE 模式需要在 FastMCP 启动前初始化）
    init_db(args.config)

    if args.transport == "sse":
        # 用 CLI 参数覆盖 FastMCP 构造时的默认值
        mcp.settings.port = args.port
        print(f"[MCP Server] Starting SSE on http://0.0.0.0:{args.port}/sse")
        mcp.run(transport="sse")
    else:
        # stdio 模式：标准输入输出
        print("[MCP Server] Starting stdio mode", file=sys.stderr)
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
