# coding=utf-8
"""Agent CLI — agent 定义管理。

    python -m cli agent create --input agent/agents/default.md
"""

import re
from pathlib import Path

import typer
import yaml

from cli import app
from config.loader import load_config

agent_app = typer.Typer(name="agent", help="Agent definition management")
app.add_typer(agent_app, name="agent")


# ═══════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def _parse_md(path: Path) -> tuple[dict, str]:
    """解析 markdown 文件，返回 (frontmatter_dict, system_prompt_body)。"""
    text = path.read_text(encoding="utf-8")
    m = _FRONTMATTER_RE.match(text)
    if not m:
        raise typer.BadParameter(
            f"文件 {path} 缺少 YAML frontmatter（需以 --- 包裹）"
        )
    frontmatter = yaml.safe_load(m.group(1))
    if not isinstance(frontmatter, dict):
        raise typer.BadParameter(f"文件 {path} 的 frontmatter 格式错误")
    system_prompt = text[m.end():].strip()
    if not system_prompt:
        raise typer.BadParameter(f"文件 {path} 的 system_prompt 为空")
    return frontmatter, system_prompt


# ═══════════════════════════════════════════════════════════════════
# Commands
# ═══════════════════════════════════════════════════════════════════


@agent_app.command("create")
def create(
    input: Path = typer.Option(
        ..., "--input", "-i", help="Agent 定义 markdown 文件路径",
    ),
    force: bool = typer.Option(
        False, "--force", "-f", help="已存在时跳过确认，直接覆盖"
    ),
) -> None:
    """从 markdown 文件导入 agent 定义到数据库。

    文件格式：
    ---
    name: "默认助手"
    description: "通用助手"
    tools: ["search_news"]
    ---

    system_prompt 内容...
    """
    from agent.data import AgentDefinition
    from storage.postgres import PostgreSQL

    if not input.is_file():
        raise typer.BadParameter(f"文件 {input} 不存在")
    frontmatter, system_prompt = _parse_md(input)

    name = frontmatter.get("name")
    if not name:
        raise typer.BadParameter("frontmatter 中缺少 name 字段")

    tools = frontmatter.get("tools", [])
    if not isinstance(tools, list):
        raise typer.BadParameter("tools 必须是列表")

    # 构建 AgentDefinition
    defn = AgentDefinition(
        id=frontmatter.get("id", ""),
        name=name,
        description=frontmatter.get("description", ""),
        system_prompt=system_prompt,
        tools=tools,
        knowledge_id=None,
        metadata={
            "icon": frontmatter.get("icon", "sparkles"),
            "category": frontmatter.get("category", "general"),
        },
    )

    # 连接数据库
    config = load_config("config.yaml")
    postgres_cfg = config.get("postgresql")
    if not postgres_cfg:
        typer.echo("错误: 配置文件中缺少 postgresql 配置", err=True)
        raise typer.Exit(code=1)

    db = PostgreSQL(postgres_cfg)
    try:
        db.connect()

        # 检查是否已存在
        existing = None
        if defn.id:
            existing = db.get_agent_definition(defn.id)

        if existing:
            if not force:
                confirm = input(
                    f"Agent '{name}' (id={defn.id}) 已存在，是否覆盖？[yes/no]: "
                )
                if confirm.strip().lower() != "yes":
                    typer.echo("已取消")
                    raise typer.Exit(code=0)

            db.update_agent_definition(defn)
            typer.echo(f"✅ Agent '{name}' (id={defn.id}) 已更新")
        else:
            new_id = db.create_agent_definition(defn)
            typer.echo(f"✅ Agent '{name}' 已创建，id={new_id}")
    finally:
        db.close()


@agent_app.command("list")
def list_() -> None:
    """列出所有 agent 定义。"""
    from storage.postgres import PostgreSQL

    config = load_config("config.yaml")
    db = PostgreSQL(config.get("postgresql", {}))
    try:
        db.connect()
        agents = db.list_agent_definitions()
        if not agents:
            typer.echo("(空)")
            return
        for a in agents:
            typer.echo(f"  {a.id:36s}  {a.name}")
    finally:
        db.close()


@agent_app.command("delete")
def delete(
    id: str = typer.Argument(..., help="Agent ID 或 'default'"),
    force: bool = typer.Option(False, "--force", "-f", help="跳过确认"),
) -> None:
    """按 ID 删除 agent 定义。"""
    from storage.postgres import PostgreSQL

    config = load_config("config.yaml")
    db = PostgreSQL(config.get("postgresql", {}))
    try:
        db.connect()
        defn = db.get_agent_definition(id)
        if not defn:
            typer.echo(f"Agent '{id}' 不存在", err=True)
            raise typer.Exit(code=1)

        if not force:
            confirm = input(f"删除 Agent '{defn.name}' (id={id})？[yes/no]: ")
            if confirm.strip().lower() != "yes":
                typer.echo("已取消")
                raise typer.Exit(code=0)

        db.delete_agent_definition(id)
        typer.echo(f"✅ Agent '{defn.name}' (id={id}) 已删除")
    finally:
        db.close()