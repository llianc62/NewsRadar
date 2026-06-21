# coding=utf-8
"""Crawl command — cloud CI entry point.

Fetches news from all configured sources, downloads article body,
and persists to SQLite.  This is the fixed cloud-CI workflow; for
manual testing use ``grab-one``.
"""

from cli import app
from config.loader import load_config
from news.crawler import Crawler, OutputStyle


@app.command()
def crawl():
    """Fetch news from all sources → download content → save to SQLite.

    Cloud CI fixed workflow — no flags needed.
    """
    from pathlib import Path

    from storage.s3 import S3Client
    from utils import format_date_today

    config = load_config("config.yaml")
    tz = config["app"]["timezone"]
    date = format_date_today(tz)
    data_dir = config["storage"]["local"].get("data_dir", "output")
    db_path = Path(data_dir) / "db" / f"{date}.db"

    # ── Download existing daily DB from S3 ───────────────────────
    # GitHub Actions runs are ephemeral — we must pull the previous
    # snapshot so today's data accumulates across multiple CI runs.
    s3 = S3Client.init_by_config(config["storage"]["cloud"])
    if not s3:
        raise ValueError(
            "crawl requires S3 storage. "
            "Configure storage.cloud in config.yaml or set CLOUD_S3_* env vars."
        )
    if s3.object_exists(f"db/{date}.db"):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        if s3.download_file(f"db/{date}.db", db_path):
            print("[Crawl] Restored existing DB from S3, will merge")
        else:
            print("[Crawl] Failed to restore DB from S3, starting fresh")

    # ── Crawl daily news ───────────────────────
    crawler = Crawler(config)
    crawler.fetch_all(OutputStyle.SQLITE)
    crawler.close()

    # ── Upload SQLite to S3 ──────────────────────────────────────
    if not db_path.exists():
        print(f"[Crawl] DB file not found: {db_path}")
        return

    if s3.upload_file(db_path, f"db/{date}.db", content_type="application/x-sqlite3"):
        print(f"[Crawl] Uploaded {db_path} -> db/{date}.db")
    else:
        print(f"[Crawl] Upload failed for db/{date}.db")
