# coding=utf-8
"""Unified storage: local SQLite + optional S3 sync."""

import os
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from models import NewsData, NewsItem

HAS_BOTO3 = False
try:
    import boto3
    from botocore.config import Config as BotoConfig
    from botocore.exceptions import ClientError

    HAS_BOTO3 = True
except ImportError:
    pass


class Storage:
    """Unified storage with local SQLite and optional S3 sync.

    Local mode: writes SQLite files to ``{data_dir}/news/{date}.db``.
    S3 mode: downloads the day's DB from S3 before writing, uploads after.

    Usage::

        s = Storage(data_dir="output", s3_config=None)
        s.save_news_data(news_data, source_tiers={"weibo": {"tier": 1, "priority": 10}})
        rows = s.get_unnotified("2026-06-04")
        s.mark_notified("2026-06-04")
        s.cleanup()
    """

    def __init__(
        self,
        data_dir: str = "output",
        timezone: str = "Asia/Shanghai",
        s3_config: Optional[Dict[str, str]] = None,
    ):
        """Initialize storage.

        Args:
            data_dir: Root directory for local DB files.
            timezone: Timezone string (used for display when needed).
            s3_config: Optional S3 config dict with keys:
                bucket_name, access_key_id, secret_access_key,
                endpoint_url, region.
                Falls back to env vars: S3_BUCKET_NAME, S3_ACCESS_KEY_ID,
                S3_SECRET_ACCESS_KEY, S3_ENDPOINT_URL.
        """
        self.data_dir = Path(data_dir)
        self.timezone = timezone

        # Connection cache: date_str -> sqlite3.Connection
        self._connections: Dict[str, sqlite3.Connection] = {}

        # Temp files tracked for cleanup()
        self._temp_files: List[Path] = []

        # ── S3 setup ─────────────────────────────────────────────────
        self.s3_enabled = False
        self.s3_client = None
        self.bucket_name = None

        if s3_config is None:
            s3_config = {}

        bucket = s3_config.get("bucket_name") or os.environ.get("S3_BUCKET_NAME")
        access_key = s3_config.get("access_key_id") or os.environ.get("S3_ACCESS_KEY_ID")
        secret_key = s3_config.get("secret_access_key") or os.environ.get("S3_SECRET_ACCESS_KEY")
        endpoint = s3_config.get("endpoint_url") or os.environ.get("S3_ENDPOINT_URL")
        region = s3_config.get("region", "")

        if bucket and access_key and secret_key and endpoint:
            if not HAS_BOTO3:
                raise ImportError(
                    "S3 storage requires boto3: pip install boto3"
                )

            self.s3_enabled = True
            self.bucket_name = bucket
            self.endpoint_url = endpoint

            # SigV2 for Tencent COS / Aliyun OSS to avoid chunked-encoding
            # issues; SigV4 for AWS S3, Cloudflare R2, MinIO, etc.
            use_sigv2 = (
                "myqcloud.com" in endpoint.lower()
                or "aliyuncs.com" in endpoint.lower()
            )
            signature_version = "s3" if use_sigv2 else "s3v4"

            s3_boto_config = BotoConfig(
                s3={"addressing_style": "virtual"},
                signature_version=signature_version,
            )

            client_kwargs: Dict[str, Any] = {
                "endpoint_url": endpoint,
                "aws_access_key_id": access_key,
                "aws_secret_access_key": secret_key,
                "config": s3_boto_config,
            }
            if region:
                client_kwargs["region_name"] = region

            self.s3_client = boto3.client("s3", **client_kwargs)
            print(
                f"[Storage] S3 enabled, bucket: {bucket}, "
                f"signature: {signature_version}"
            )

    # ── Path helpers ────────────────────────────────────────────────

    def _get_db_path(self, date: str) -> Path:
        """Return ``{data_dir}/news/{date}.db``, creating parent dirs."""
        path = self.data_dir / "news" / f"{date}.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _s3_key(self, date: str) -> str:
        """Return S3 object key: ``news/{date}.db``."""
        return f"news/{date}.db"

    # ── Connection management ───────────────────────────────────────

    def _get_connection(self, date: str) -> sqlite3.Connection:
        """Get or create a cached sqlite3 connection for *date*.

        Enables WAL mode and foreign keys.  Runs ``_init_tables()`` on
        every connection so that schema is guaranteed present.
        """
        if date not in self._connections:
            db_path = self._get_db_path(date)

            conn = sqlite3.connect(str(db_path))
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")

            self._init_tables(conn)
            self._connections[date] = conn

        return self._connections[date]

    def _init_tables(self, conn: sqlite3.Connection) -> None:
        """Read ``schema.sql`` from the same directory and execute it."""
        schema_path = Path(__file__).parent / "schema.sql"
        if not schema_path.exists():
            raise FileNotFoundError(f"Schema file not found: {schema_path}")

        with open(schema_path, "r", encoding="utf-8") as f:
            schema_sql = f.read()

        conn.executescript(schema_sql)
        conn.commit()

    # ── S3 operations ───────────────────────────────────────────────

    def _download_from_s3(self, date: str) -> Optional[Path]:
        """Download today's DB from S3 into a local temp file.

        Returns the temp-file Path, or *None* when the object does not
        exist or S3 is disabled.
        """
        if not self.s3_enabled or not self.s3_client:
            return None

        key = self._s3_key(date)

        # Check existence first
        try:
            self.s3_client.head_object(Bucket=self.bucket_name, Key=key)
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchKey", "Not Found"):
                print(f"[Storage] S3 object not found: {key}, will create new")
                return None
            print(f"[Storage] S3 check failed ({key}): {e}")
            return None
        except Exception as e:
            print(f"[Storage] S3 check failed ({key}): {e}")
            return None

        try:
            # Download to a temp file so we can handle any errors cleanly
            tmp_fd, tmp_path = tempfile.mkstemp(
                suffix=".db", prefix="s3_download_"
            )
            os.close(tmp_fd)
            tmp_path = Path(tmp_path)
            self._temp_files.append(tmp_path)

            response = self.s3_client.get_object(
                Bucket=self.bucket_name, Key=key
            )
            with open(tmp_path, "wb") as f:
                for chunk in response["Body"].iter_chunks(
                    chunk_size=1024 * 1024
                ):
                    f.write(chunk)

            print(f"[Storage] Downloaded from S3: {key}")
            return tmp_path
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchKey", "Not Found"):
                print(f"[Storage] S3 object not found: {key}, will create new")
                return None
            print(f"[Storage] S3 download failed ({key}): {e}")
            return None
        except Exception as e:
            print(f"[Storage] S3 download failed ({key}): {e}")
            return None

    def _upload_to_s3(self, local_path: Path, date: str) -> bool:
        """Upload a local DB file to S3.

        Reads the entire file into memory before uploading so that
        ``put_object`` can receive a concrete ``ContentLength`` (avoids
        chunked transfer-encoding issues with Tencent COS / Aliyun OSS).
        """
        if not self.s3_enabled or not self.s3_client:
            return False

        if not local_path.exists():
            print(f"[Storage] Local file not found, cannot upload: {local_path}")
            return False

        key = self._s3_key(date)

        try:
            local_size = local_path.stat().st_size

            with open(local_path, "rb") as f:
                file_content = f.read()

            self.s3_client.put_object(
                Bucket=self.bucket_name,
                Key=key,
                Body=file_content,
                ContentLength=local_size,
                ContentType="application/x-sqlite3",
            )
            print(f"[Storage] Uploaded to S3: {key} ({local_size} bytes)")
            return True
        except Exception as e:
            print(f"[Storage] S3 upload failed ({key}): {e}")
            return False

    # ── Core: save news data ────────────────────────────────────────

    def save_news_data(
        self,
        news_data: NewsData,
        source_tiers: Optional[Dict[str, Dict[str, int]]] = None,
    ) -> None:
        """Save *news_data* to SQLite, syncing with S3 when enabled.

        Dedup rules (matching the unique partial indexes in schema.sql):

        * **Hot-list** — matched on ``(url, source_id)`` when ``url`` is
          non-empty.  ``url != ''`` acts as the guard because the unique
          index filters on that condition.
        * **RSS** — matched on ``(guid, source_id)`` when ``guid`` is
          non-empty.
        * Items without a dedup key are always inserted as new.

        On **match**: title, rank, mobile_url, last_crawl_time,
        crawl_count (+1), priority, and tier are updated.  The
        ``notified`` column is **never** touched on update.

        On **insert**: ``notified`` defaults to 0.

        Args:
            news_data: A :class:`NewsData` produced by the crawler
                converters.
            source_tiers: Optional ``{source_id: {tier: int,
                priority: int}}`` mapping.  Sources not listed get
                tier=4, priority=0.
        """
        date = news_data.date

        # ── S3 pre-fetch ──────────────────────────────────────────
        if self.s3_enabled:
            downloaded = self._download_from_s3(date)
            if downloaded is not None:
                local_path = self._get_db_path(date)

                # Close any cached connection so we reopen against the
                # freshly-copied file
                if date in self._connections:
                    self._connections[date].close()
                    del self._connections[date]

                shutil.copy2(str(downloaded), str(local_path))

        conn = self._get_connection(date)
        cursor = conn.cursor()

        if source_tiers is None:
            source_tiers = {}

        new_total = 0
        updated_total = 0

        for source_id, news_list in news_data.items.items():
            tier_info = source_tiers.get(source_id, {})
            tier = tier_info.get("tier", 4)
            priority = tier_info.get("priority", 0)

            source_new = 0
            source_updated = 0

            for item in news_list:
                try:
                    existing = None

                    # ── Dedup lookup ──────────────────────────
                    if item.source_type == "hotlist" and item.url:
                        cursor.execute(
                            """SELECT id FROM news_items
                               WHERE url = ? AND source_id = ?
                                 AND source_type = 'hotlist'""",
                            (item.url, source_id),
                        )
                        existing = cursor.fetchone()
                    elif item.source_type == "rss" and item.guid:
                        cursor.execute(
                            """SELECT id FROM news_items
                               WHERE guid = ? AND source_id = ?
                                 AND source_type = 'rss'""",
                            (item.guid, source_id),
                        )
                        existing = cursor.fetchone()

                    if existing is not None:
                        # ── Update existing row ─────────────────
                        # IMPORTANT: do NOT touch `notified`!
                        cursor.execute(
                            """UPDATE news_items SET
                                title = ?,
                                rank = ?,
                                mobile_url = ?,
                                last_crawl_time = ?,
                                crawl_count = crawl_count + 1,
                                priority = ?,
                                tier = ?
                               WHERE id = ?""",
                            (
                                item.title,
                                item.rank,
                                item.mobile_url,
                                item.last_crawl_time,
                                priority,
                                tier,
                                existing[0],
                            ),
                        )
                        source_updated += 1
                    else:
                        # ── Insert new row ──────────────────────
                        cursor.execute(
                            """INSERT INTO news_items
                               (title, source_id, source_name, source_type,
                                tier, priority, url, mobile_url, rank,
                                guid, published_at, summary, author,
                                notified, first_crawl_time, last_crawl_time,
                                crawl_count)
                               VALUES (
                                ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                ?, ?, ?, ?,
                                0, ?, ?, 1
                               )""",
                            (
                                item.title,
                                source_id,
                                item.source_name,
                                item.source_type,
                                tier,
                                priority,
                                item.url,
                                item.mobile_url,
                                item.rank,
                                item.guid,
                                item.published_at,
                                item.summary,
                                item.author,
                                item.first_crawl_time,
                                item.last_crawl_time,
                            ),
                        )
                        source_new += 1

                except sqlite3.Error as e:
                    print(
                        f"[Storage] Failed to save item "
                        f"[{item.title[:30]}...]: {e}"
                    )

            if source_new > 0 or source_updated > 0:
                print(
                    f"[Storage] {source_id}: "
                    f"{source_new} new, {source_updated} updated"
                )
            new_total += source_new
            updated_total += source_updated

        conn.commit()
        print(
            f"[Storage] Saved: {new_total} new, {updated_total} updated "
            f"(date={date})"
        )

        # ── S3 upload ────────────────────────────────────────────
        if self.s3_enabled:
            self._upload_to_s3(self._get_db_path(date), date)

    # ── Query methods ───────────────────────────────────────────────

    def get_unnotified(self, date: str) -> List[sqlite3.Row]:
        """Return all unnotified rows, ordered by tier ASC, priority DESC."""
        conn = self._get_connection(date)
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            """SELECT * FROM news_items
               WHERE notified = 0
               ORDER BY tier ASC, priority DESC"""
        )
        return cursor.fetchall()

    def mark_notified(self, date: str) -> None:
        """Mark every unnotified news item as notified and commit."""
        conn = self._get_connection(date)
        conn.execute(
            "UPDATE news_items SET notified = 1 WHERE notified = 0"
        )
        conn.commit()
        print(f"[Storage] Marked items as notified (date={date})")

        # Sync to S3 when enabled
        if self.s3_enabled:
            self._upload_to_s3(self._get_db_path(date), date)

    # ── Cleanup ─────────────────────────────────────────────────────

    def cleanup(self) -> None:
        """Close all cached connections and remove tracked temp files."""
        for conn in self._connections.values():
            try:
                conn.close()
            except Exception:
                pass
        self._connections.clear()

        for tmp_file in self._temp_files:
            try:
                os.unlink(str(tmp_file))
            except OSError:
                pass
        self._temp_files.clear()

        print("[Storage] Cleanup complete")
