# coding=utf-8
"""Knowledge base CLI - pgvector 知识库管理。

    python -m cli knowledge ingest <file|dir> --namespace buffett
    python -m cli knowledge search "价值投资" --namespace buffett
    python -m cli knowledge list --namespace buffett
    python -m cli knowledge clear --namespace buffett --force
"""

from pathlib import Path
from typing import Optional

import typer

from cli import app
from config import load_config

knowledge_app = typer.Typer(name="knowledge", help="Knowledge base (pgvector) management")
app.add_typer(knowledge_app, name="knowledge")

# 支持导入的文本文件后缀
_TEXT_SUFFIXES = (".md", ".txt", ".markdown")


# ═══════════════════════════════════════════════════════════════════
# Engine 构建
# ═══════════════════════════════════════════════════════════════════


def _build_engine(config: dict):
    """从配置构建 ``(KnowledgeEngine, KnowledgeStore, PostgreSQL)``。

    连接 PG 并幂等初始化 schema（确保 ``knowledge_chunks`` 表 + pgvector
    扩展存在）。调用方负责 ``db.close()``。
    """
    kcfg = config.get("knowledge", {})
    if not kcfg.get("embedding_api_key"):
        typer.echo(
            "[Knowledge] embedding_api_key 未配置"
            "（config/config.yaml 的 knowledge.embedding_api_key 或环境变量"
            " KNOWLEDGE_EMBEDDING_API_KEY）",
            err=True,
        )
        raise typer.Exit(code=1)

    from agent.knowledge import (
        EmbeddingClient,
        KnowledgeEngine,
        PgVectorKnowledgeStore,
    )
    from storage.postgres import PostgreSQL

    db = PostgreSQL(config["postgresql"])
    db.connect()
    db.init_schema()  # 幂等：建表 + pgvector 扩展

    embedding = EmbeddingClient(
        api_key=kcfg["embedding_api_key"],
        base_url=kcfg.get("embedding_base_url", ""),
        model=kcfg.get("embedding_model", "text-embedding-3-small"),
    )
    store = PgVectorKnowledgeStore(db)
    engine = KnowledgeEngine(
        store=store, embedding=embedding, top_k=kcfg.get("top_k", 5)
    )
    return engine, store, db


def _collect_docs(path: Path) -> list[dict]:
    """从文件或目录收集文档（递归 ``.md``/``.txt``）。

    返回 ``[{source, content, metadata}]``，跳过空文件。
    """
    if path.is_dir():
        files = sorted(
            p
            for p in path.rglob("*")
            if p.suffix.lower() in _TEXT_SUFFIXES and p.is_file()
        )
    elif path.is_file():
        files = [path]
    else:
        typer.echo(f"[Knowledge] 路径不存在: {path}", err=True)
        raise typer.Exit(code=1)

    docs: list[dict] = []
    for f in files:
        text = f.read_text(encoding="utf-8", errors="replace")
        if text.strip():
            docs.append(
                {
                    "source": str(f),
                    "content": text,
                    "metadata": {"type": "document", "file": str(f)},
                }
            )
    return docs


# ═══════════════════════════════════════════════════════════════════
# Commands
# ═══════════════════════════════════════════════════════════════════


@knowledge_app.command()
def ingest(
    path: Path = typer.Argument(..., help="文档文件或目录（递归 .md/.txt）"),
    namespace: str = typer.Option(..., "--namespace", "-n", help="命名空间"),
) -> None:
    """导入文档到知识库（切片 + embedding + 存 pgvector）。"""
    config = load_config("config/config.yaml")
    engine, _store, db = _build_engine(config)
    try:
        docs = _collect_docs(path)
        if not docs:
            typer.echo(f"[Knowledge] 未找到可导入的文档: {path}", err=True)
            raise typer.Exit(code=1)
        n = engine.ingest_documents(docs, namespace=namespace)
        typer.echo(
            f"[Knowledge] 导入完成: {len(docs)} 文档 -> {n} 切片 "
            f"(namespace={namespace})"
        )
    finally:
        db.close()


@knowledge_app.command()
def search(
    query: str = typer.Argument(..., help="查询语句"),
    namespace: str = typer.Option(..., "--namespace", "-n", help="命名空间"),
    top_k: int = typer.Option(5, "--top-k", "-k", help="返回片段数"),
) -> None:
    """语义检索测试。"""
    config = load_config("config/config.yaml")
    engine, _store, db = _build_engine(config)
    try:
        results = engine.retrieve(query, namespace=namespace, top_k=top_k)
        if not results:
            typer.echo(f"[Knowledge] 无结果 (namespace={namespace})")
            return
        for i, r in enumerate(results, 1):
            typer.echo(
                f"━━━ #{i} [{r['source']}] 相关度 {float(r['similarity']):.2f} ━━━"
            )
            typer.echo(r["content"])
            typer.echo("")
    finally:
        db.close()


@knowledge_app.command("list")
def list_chunks(
    namespace: str = typer.Option(
        "", "--namespace", "-n", help="命名空间（空=全部）"
    ),
) -> None:
    """查看切片数。"""
    config = load_config("config/config.yaml")
    _engine, store, db = _build_engine(config)
    try:
        n = store.count(namespace) if namespace else store.count()
        label = f"namespace={namespace}" if namespace else "全部命名空间"
        typer.echo(f"[Knowledge] {n} 切片 ({label})")
    finally:
        db.close()


@knowledge_app.command()
def clear(
    namespace: str = typer.Option(..., "--namespace", "-n", help="命名空间"),
    force: bool = typer.Option(False, "--force", "-f", help="跳过确认"),
) -> None:
    """清空命名空间。"""
    config = load_config("config/config.yaml")
    _engine, store, db = _build_engine(config)
    try:
        n = store.count(namespace)
        if n == 0:
            typer.echo(f"[Knowledge] namespace={namespace} 无切片，无需清理。")
            return
        if not force:
            ans = input(
                f"将删除 namespace={namespace} 的 {n} 个切片，继续？[yes/no]: "
            ).strip().lower()
            if ans != "yes":
                typer.echo("[Knowledge] 已取消。")
                return
        deleted = store.delete(namespace)
        typer.echo(f"[Knowledge] 删除 {deleted} 切片 (namespace={namespace})")
    finally:
        db.close()
