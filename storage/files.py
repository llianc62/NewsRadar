# coding=utf-8
"""Unified storage layer — local filesystem and S3-compatible backends.

Two-method interface::

    from storage.files import LocalStorage, S3Storage

    storage = LocalStorage("output")
    key = storage.save(image_bytes, "news/2026-06-09/img_00.jpg",
                       content_type="image/jpeg")
    url = storage.get(key)  # absolute path for local, presigned URL for S3
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path


# ═══════════════════════════════════════════════════════════════════
# Base
# ═══════════════════════════════════════════════════════════════════


class FileStorage(ABC):
    """Abstract storage backend — local filesystem or S3-compatible."""

    @abstractmethod
    def save(self, data: bytes, path: str,
             content_type: str = "") -> str:
        """Persist *data* at *path*, return the logical path.

        Both backends return *path* unchanged — a relative path for
        :class:`LocalStorage`, an S3 object key for :class:`S3Storage`.

        Args:
            data: Raw file bytes.
            path: Logical path (e.g. ``news/2026-06-09/img_00.jpg``).
            content_type: MIME type (used only by remote backends).
        """
        ...

    @abstractmethod
    def get(self, path: str, expires_in: int = 604800) -> str:
        """Return an access URL for the stored object at *path*.

        :class:`LocalStorage` returns the absolute filesystem path.
        :class:`S3Storage` returns a presigned GET URL (default 7-day
        expiry).

        Args:
            path: Logical path returned by :meth:`save`.
            expires_in: Presigned URL validity in seconds (S3 only).
        """
        ...


# ═══════════════════════════════════════════════════════════════════
# Local filesystem
# ═══════════════════════════════════════════════════════════════════


class LocalStorage(FileStorage):
    """Local filesystem storage — everything under a single root."""

    def __init__(self, data_dir: str = "output") -> None:
        self._data_dir = Path(data_dir).resolve()

    # ── properties ──────────────────────────────────────────────────

    @property
    def data_dir(self) -> Path:
        """Root output directory (read-only)."""
        return self._data_dir

    # ── FileStorage interface ────────────────────────────────────────

    def save(self, data: bytes, path: str,
             content_type: str = "") -> str:
        full = self._data_dir / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_bytes(data)
        return path

    def get(self, path: str, expires_in: int = 604800) -> str:
        return str(self._data_dir / path)


# ═══════════════════════════════════════════════════════════════════
# S3-compatible object storage
# ═══════════════════════════════════════════════════════════════════


class S3Storage(FileStorage):
    """S3-compatible object storage (MinIO, AWS S3, Tencent COS …).

    Wraps :class:`storage.s3.S3Client` and provides the same
    ``save`` / ``get`` interface as :class:`LocalStorage`.
    """

    def __init__(self, config: dict) -> None:
        from storage.s3 import S3Client

        s3 = S3Client.init_by_config(config)
        if s3 is None:
            raise ValueError(
                "S3Storage requires valid remote config "
                "(endpoint_url, bucket_name, access_key_id, secret_access_key)"
            )
        self._s3 = s3
        self._endpoint = config.get("endpoint_url", "")
        self._bucket = config.get("bucket_name", "")

    # ── FileStorage interface ────────────────────────────────────────

    def save(self, data: bytes, path: str,
             content_type: str = "") -> str:
        self._s3.upload(
            data, path,
            content_type=content_type or "application/octet-stream",
        )
        return path

    def get(self, path: str, expires_in: int = 604800) -> str:
        url = self._s3.presigned_get_url(path, expires_in=expires_in)
        return url or path
