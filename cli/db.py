# coding=utf-8
"""Database maintenance commands.

``python -m cli db clear --all --force``
"""

from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import typer

from cli import app
from config.loader import load_config

db_app = typer.Typer(name="db", help="Database maintenance commands")
app.add_typer(db_app, name="db")


# ═══════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════


def _next_day(date_str: str) -> str:
    """Return *date_str* + 1 day as ISO date string."""
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    return (d + timedelta(days=1)).isoformat()


def _build_where(
    delete_all: bool,
    before: Optional[str],
    after: Optional[str],
    col: str = "published_at",
) -> tuple[str, list]:
    """Build a SQL WHERE clause from time-range parameters.

    Returns ``(where_clause, params)``.  *where_clause* includes the
    leading ``WHERE`` keyword when non-empty.
    """
    if delete_all:
        return "", []

    conditions: list[str] = []
    params: list[str] = []

    if before:
        conditions.append(f"{col} < %s")
        params.append(_next_day(before))
    if after:
        conditions.append(f"{col} >= %s")
        params.append(after)

    if not conditions:
        return "", []

    return f"WHERE {' AND '.join(conditions)}", params


def _build_sqlite_where(
    delete_all: bool,
    before: Optional[str],
    after: Optional[str],
    col: str = "published_at",
) -> tuple[str, list]:
    """Same as :func:`_build_where` but for SQLite (``?`` placeholders)."""
    if delete_all:
        return "", []

    conditions: list[str] = []
    params: list[str] = []

    if before:
        conditions.append(f"{col} < ?")
        params.append(_next_day(before))
    if after:
        conditions.append(f"{col} >= ?")
        params.append(after)

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
    before: Optional[str],
    after: Optional[str],
    force: bool,
) -> None:
    """Delete rows from PostgreSQL ``news_articles`` + ``news_images``."""
    from storage.postgres import Database

    db = Database(config["postgresql"])
    try:
        db.connect()

        where, params = _build_where(delete_all, before, after)

        # ── Count ──────────────────────────────────────────────────
        with db.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT COUNT(*) FROM news_articles {where}", params)
                article_count = cur.fetchone()[0]

                cur.execute(
                    f"""SELECT COUNT(*) FROM news_images
                        WHERE article_id IN (
                            SELECT id FROM news_articles {where}
                        )""",
                    params,
                )
                image_count = cur.fetchone()[0]

        if article_count == 0:
            print("[PostgreSQL] No matching rows — nothing to delete.")
            return

        # ── Confirm ────────────────────────────────────────────────
        print(
            f"[PostgreSQL] Matching: {article_count} articles, "
            f"{image_count} images"
        )
        if not _confirm(article_count, "PostgreSQL", force):
            print("[PostgreSQL] Cancelled.")
            return

        # ── Delete ─────────────────────────────────────────────────
        with db.get_conn() as conn:
            with conn.cursor() as cur:
                # Images first (though CASCADE handles it, be explicit)
                cur.execute(
                    f"""DELETE FROM news_images
                        WHERE article_id IN (
                            SELECT id FROM news_articles {where}
                        )""",
                    params,
                )
                img_deleted = cur.rowcount

                cur.execute(f"DELETE FROM news_articles {where}", params)
                art_deleted = cur.rowcount

        print(
            f"[PostgreSQL] Deleted: {art_deleted} articles, "
            f"{img_deleted} images"
        )

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
    before: Optional[str],
    after: Optional[str],
    force: bool,
) -> None:
    """Clear news data from SQLite files under ``output/news/``."""
    import sqlite3

    storage_cfg = config.get("storage", {})
    data_dir = Path(storage_cfg.get("local", {}).get("data_dir", "output"))
    news_dir = data_dir / "news"

    if not news_dir.is_dir():
        print(f"[SQLite] Directory not found: {news_dir} — nothing to delete.")
        return

    db_files = sorted(news_dir.glob("*.db"))
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
    where, params = _build_sqlite_where(delete_all, before, after)

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
    before: Optional[str] = typer.Option(
        None, "--before",
        help="Delete records with published_at before this date (YYYY-MM-DD, inclusive)",
    ),
    after: Optional[str] = typer.Option(
        None, "--after",
        help="Delete records with published_at after this date (YYYY-MM-DD, inclusive)",
    ),
    force: bool = typer.Option(
        False, "--force", "-f",
        help="Skip confirmation prompt",
    ),
) -> None:
    """Clear news data from the database.

    Must specify either --all or --before/--after.

    Examples:

        python -m cli db clear --all --force

        python -m cli db clear --before 2026-06-01

        python -m cli db clear --backend postgresql --before 2026-06-01 --after 2026-05-01
    """
    # ── Validate ───────────────────────────────────────────────────
    if not delete_all and before is None and after is None:
        typer.echo(
            "Error: must specify --all or --before/--after.\n"
            "Try 'python -m cli db clear --help' for usage.",
            err=True,
        )
        raise typer.Exit(code=1)

    if delete_all and (before is not None or after is not None):
        typer.echo(
            "Error: --all and --before/--after are mutually exclusive.",
            err=True,
        )
        raise typer.Exit(code=1)

    # Validate date format
    for label, value in [("--before", before), ("--after", after)]:
        if value is not None:
            try:
                datetime.strptime(value, "%Y-%m-%d")
            except ValueError:
                typer.echo(
                    f"Error: {label} must be YYYY-MM-DD format, got: {value}",
                    err=True,
                )
                raise typer.Exit(code=1)

    # ── Load config ────────────────────────────────────────────────
    config = load_config("config.yaml")

    # ── Execute ────────────────────────────────────────────────────
    if backend in ("all", "postgresql"):
        try:
            _clear_postgresql(config, delete_all, before, after, force)
        except Exception as e:
            typer.echo(f"PostgreSQL clear failed: {e}", err=True)
            if backend != "all":
                raise typer.Exit(code=1)

    if backend in ("all", "sqlite"):
        _clear_sqlite(config, delete_all, before, after, force)

    print("[DB] Clear complete.")
