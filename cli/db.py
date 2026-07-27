# coding=utf-8
"""Database maintenance commands.

``python -m cli db clear --start 2026-07-02 --end 2026-07-04 --force``
"""

from datetime import datetime
from pathlib import Path
from typing import Optional

import typer

from cli import app
from config import load_config

db_app = typer.Typer(name="db", help="Database maintenance commands")
app.add_typer(db_app, name="db")


# ═══════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════


def _parse_datetime(value: str) -> str:
    """Validate and normalise a datetime string.

    Accepts ``YYYY-MM-DD`` or ``YYYY-MM-DD HH:MM:SS`` and returns the
    value unchanged.  Raises ``ValueError`` on invalid format.
    """
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            datetime.strptime(value, fmt)
            return value
        except ValueError:
            continue
    raise ValueError(f"Invalid datetime format: {value!r}")


def _build_where(
    delete_all: bool,
    start: Optional[str],
    end: Optional[str],
    col: str = "created_at",
) -> tuple[str, list]:
    """Build a SQL WHERE clause from time-range parameters.

    Returns ``(where_clause, params)``.  *where_clause* includes the
    leading ``WHERE`` keyword when non-empty.
    """
    if delete_all:
        return "", []

    conditions: list[str] = []
    params: list[str] = []

    if start:
        conditions.append(f"{col} >= %s")
        params.append(start)
    if end:
        conditions.append(f"{col} < %s")
        params.append(end)

    if not conditions:
        return "", []

    return f"WHERE {' AND '.join(conditions)}", params


def _build_sqlite_where(
    delete_all: bool,
    start: Optional[str],
    end: Optional[str],
    col: str = "created_at",
) -> tuple[str, list]:
    """Same as :func:`_build_where` but for SQLite (``?`` placeholders)."""
    if delete_all:
        return "", []

    conditions: list[str] = []
    params: list[str] = []

    if start:
        conditions.append(f"{col} >= ?")
        params.append(start)
    if end:
        conditions.append(f"{col} < ?")
        params.append(end)

    if not conditions:
        return "", []

    return f"WHERE {' AND '.join(conditions)}", params


def _confirm(count: int, backend: str, force: bool) -> bool:
    """Ask user to confirm deletion.  Returns True to proceed."""
    if force:
        return True
    print(f"Will delete {count} rows from {backend}.")
    ans = input("Continue? [yes/no]: ").strip().lower()
    return ans == "yes"


# ═══════════════════════════════════════════════════════════════════
# PostgreSQL
# ═══════════════════════════════════════════════════════════════════


def _clear_postgresql(
    config: dict,
    delete_all: bool,
    start: Optional[str],
    end: Optional[str],
    force: bool,
) -> None:
    """Delete rows from PostgreSQL ``news_articles`` + ``failed_tasks``."""
    from storage.postgres import PostgreSQL

    db = PostgreSQL(config["postgresql"])
    try:
        db.connect()

        where, params = _build_where(delete_all, start, end)

        # ── Count ──────────────────────────────────────────────────
        with db.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT COUNT(*) FROM news_articles {where}", params)
                article_count = cur.fetchone()[0]

                task_count = 0
                if delete_all:
                    cur.execute("SELECT COUNT(*) FROM failed_tasks")
                    task_count = cur.fetchone()[0]

        if article_count == 0 and task_count == 0:
            print("[PostgreSQL] No matching rows — nothing to delete.")
            return

        # ── Confirm ────────────────────────────────────────────────
        parts = [f"{article_count} articles"]
        if delete_all and task_count > 0:
            parts.append(f"{task_count} failed_tasks")
        print(f"[PostgreSQL] Matching: {', '.join(parts)}")
        total = article_count + task_count
        if not _confirm(total, "PostgreSQL", force):
            print("[PostgreSQL] Cancelled.")
            return

        # ── Delete ─────────────────────────────────────────────────
        with db.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM news_articles {where}", params)
                art_deleted = cur.rowcount

                task_deleted = 0
                if delete_all:
                    cur.execute("DELETE FROM failed_tasks")
                    task_deleted = cur.rowcount

        parts = [f"{art_deleted} articles"]
        if task_deleted > 0:
            parts.append(f"{task_deleted} failed_tasks")
        print(f"[PostgreSQL] Deleted: {', '.join(parts)}")

    except Exception as e:
        print(f"[PostgreSQL] Error: {e}")
        raise
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════════
# SQLite
# ═══════════════════════════════════════════════════════════════════


def _clear_sqlite(
    config: dict,
    delete_all: bool,
    start: Optional[str],
    end: Optional[str],
    force: bool,
) -> None:
    """Clear news data from SQLite files under ``output/db/``."""
    import sqlite3

    storage_cfg = config.get("storage", {})
    data_dir = Path(storage_cfg.get("local", {}).get("data_path", "output"))
    db_dir = data_dir / "db"

    if not db_dir.is_dir():
        print(f"[SQLite] Directory not found: {db_dir} — nothing to delete.")
        return

    db_files = sorted(db_dir.glob("*.db"))
    if not db_files:
        print("[SQLite] No .db files found — nothing to delete.")
        return

    if delete_all:
        # ── Full clear: delete all .db files ───────────────────────
        total_size = sum(f.stat().st_size for f in db_files)
        print(
            f"[SQLite] {len(db_files)} file(s), "
            f"{total_size / 1024:.1f} KB"
        )
        if not force:
            ans = input(
                f"Delete all {len(db_files)} file(s) ({total_size / 1024:.1f} KB) "
                f"from SQLite? [yes/no]: "
            ).strip().lower()
            if ans != "yes":
                print("[SQLite] Cancelled.")
                return

        deleted = 0
        for db_file in db_files:
            db_file.unlink()
            deleted += 1
        print(f"[SQLite] Deleted {deleted} file(s)")
        return

    # ── Time-range clear: scan each file ───────────────────────────
    where, params = _build_sqlite_where(delete_all, start, end)

    total_deleted = 0
    files_removed = 0

    # Count pass
    total_matching = 0
    for db_file in db_files:
        try:
            conn = sqlite3.connect(str(db_file))
            cur = conn.execute(
                f"SELECT COUNT(*) FROM news_items {where}", params
            )
            total_matching += cur.fetchone()[0]
            conn.close()
        except sqlite3.Error as e:
            print(f"[SQLite] Warning: cannot read {db_file.name}: {e}")

    if total_matching == 0:
        print("[SQLite] No matching rows — nothing to delete.")
        return

    print(f"[SQLite] Matching: {total_matching} rows across {len(db_files)} file(s)")
    if not _confirm(total_matching, "SQLite", force):
        print("[SQLite] Cancelled.")
        return

    # Delete pass
    for db_file in db_files:
        try:
            conn = sqlite3.connect(str(db_file))
            cur = conn.execute(
                f"DELETE FROM news_items {where}", params
            )
            deleted = cur.rowcount
            conn.commit()

            if deleted > 0:
                total_deleted += deleted

            # If file is now empty, remove it
            remaining = conn.execute("SELECT COUNT(*) FROM news_items").fetchone()[0]
            conn.close()

            if remaining == 0:
                db_file.unlink()
                files_removed += 1
                print(f"[SQLite] {db_file.name}: {deleted} deleted, file removed")
            else:
                print(f"[SQLite] {db_file.name}: {deleted} deleted, {remaining} remaining")

        except sqlite3.Error as e:
            print(f"[SQLite] Error processing {db_file.name}: {e}")

    print(
        f"[SQLite] Done: {total_deleted} rows deleted, "
        f"{files_removed} file(s) removed"
    )


# ═══════════════════════════════════════════════════════════════════
# CLI command
# ═══════════════════════════════════════════════════════════════════


@db_app.command()
def clear(
    backend: str = typer.Option(
        "all", "--backend", "-b",
        help="Target database: postgresql, sqlite, or all",
    ),
    delete_all: bool = typer.Option(
        False, "--all",
        help="Delete ALL rows (must be explicitly specified)",
    ),
    start: Optional[str] = typer.Option(
        None, "--start",
        help="Delete records created_at >= this datetime (YYYY-MM-DD or YYYY-MM-DD HH:MM:SS)",
    ),
    end: Optional[str] = typer.Option(
        None, "--end",
        help="Delete records created_at < this datetime (YYYY-MM-DD or YYYY-MM-DD HH:MM:SS)",
    ),
    force: bool = typer.Option(
        False, "--force", "-f",
        help="Skip confirmation prompt",
    ),
) -> None:
    """Clear news data from the database.

    Must specify either --all or --start/--end (or both).

    Examples:

        python -m cli db clear --all --force

        python -m cli db clear --end "2026-06-01 23:59:59"

        python -m cli db clear --backend postgresql --start 2026-05-01 --end 2026-06-02
    """
    # ── Validate ───────────────────────────────────────────────────
    if not delete_all and start is None and end is None:
        typer.echo(
            "Error: must specify --all or --start/--end.\n"
            "Try 'python -m cli db clear --help' for usage.",
            err=True,
        )
        raise typer.Exit(code=1)

    if delete_all and (start is not None or end is not None):
        typer.echo(
            "Error: --all and --start/--end are mutually exclusive.",
            err=True,
        )
        raise typer.Exit(code=1)

    # Validate datetime format
    for label, value in [("--start", start), ("--end", end)]:
        if value is not None:
            try:
                _parse_datetime(value)
            except ValueError as e:
                typer.echo(
                    f"Error: {label} must be YYYY-MM-DD or YYYY-MM-DD HH:MM:SS format, got: {value}",
                    err=True,
                )
                raise typer.Exit(code=1)

    # ── Load config ────────────────────────────────────────────────
    config = load_config("config/config.yaml")

    # ── Execute ────────────────────────────────────────────────────
    if backend in ("all", "postgresql"):
        try:
            _clear_postgresql(config, delete_all, start, end, force)
        except Exception as e:
            typer.echo(f"PostgreSQL clear failed: {e}", err=True)
            if backend != "all":
                raise typer.Exit(code=1)

    if backend in ("all", "sqlite"):
        _clear_sqlite(config, delete_all, start, end, force)

    print("[DB] Clear complete.")
