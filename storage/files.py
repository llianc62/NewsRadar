# coding=utf-8
"""Unified storage layer — local filesystem and S3-compatible backends.

Provides a base class with two implementations so that callers can
instantiate the storage backend they need without reading config
details themselves::

    from storage import create_storage

    storage = create_storage(config)
    url = storage.save_file(image_bytes, "images/2026-06-09/img_00.jpg",
                            content_type="image/jpeg")
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from tempfile import mkdtemp, mktemp
from typing import Optional


# ═══════════════════════════════════════════════════════════════════
# Base
# ═══════════════════════════════════════════════════════════════════


class FileStorage(ABC):
    """Abstract storage backend — local filesystem or S3-compatible."""

    @abstractmethod
    def save_file(self, data: bytes, path: str,
                  content_type: str = "") -> str:
        """Persist *data* at *path* and return an access URL or file path.

        Args:
            data: Raw file bytes.
            path: Logical path (e.g. ``images/2026-06-09/img_00.jpg``).
            content_type: MIME type (used only by remote backends).

        Returns:
            Local absolute path (``LocalStorage``) or object URL
            (``S3Storage``).
        """
        ...

    @abstractmethod
    def get_path(self, *parts: str) -> Path:
        """Create and return a directory path suitable for staging files.

        For ``LocalStorage`` this is a persistent directory under
        ``data_dir``.  For ``S3Storage`` this is a temporary directory
        (callers should clean up).
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

    def save_file(self, data: bytes, path: str,
                  content_type: str = "") -> str:
        full = self._data_dir / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_bytes(data)
        return str(full)

    def get_path(self, *parts: str) -> Path:
        p = self._data_dir.joinpath(*parts)
        p.mkdir(parents=True, exist_ok=True)
        return p


# ═══════════════════════════════════════════════════════════════════
# S3-compatible object storage
# ═══════════════════════════════════════════════════════════════════


class S3Storage(FileStorage):
    """S3-compatible object storage (MinIO, AWS S3, Tencent COS …).

    Wraps :class:`storage.s3.S3Client` and provides the same
    ``save_file`` / ``get_path`` interface as :class:`LocalStorage`.
    """

    def __init__(self, remote_config: dict) -> None:
        from storage.s3 import S3Client

        rc = dict(remote_config)
        client = S3Client.from_config(rc)
        if client is None:
            raise ValueError(
                "S3Storage requires valid remote config "
                "(endpoint_url, bucket_name, access_key_id, secret_access_key)"
            )
        self._s3 = client
        self._endpoint = rc.get("endpoint_url", "")
        self._bucket = rc.get("bucket_name", "")

    # ── FileStorage interface ────────────────────────────────────────

    def save_file(self, data: bytes, path: str,
                  content_type: str = "") -> str:
        suffix = Path(path).suffix
        tmp = Path(mktemp(suffix=suffix))
        tmp.write_bytes(data)
        try:
            self._s3.upload_file(
                tmp, path,
                content_type=content_type or "application/octet-stream",
            )
        finally:
            tmp.unlink(missing_ok=True)
        # 返回 S3 对象 key（相对路径），不拼 URL
        # 浏览器端通过 web 代理 /media/{path} 访问，由服务端动态签名
        return path

    def get_path(self, *parts: str) -> Path:
        """Return a temporary staging directory.

        Callers should clean up when done.  S3 has no persistent local
        filesystem, so a temp directory is the best we can offer.
        """
        return Path(mkdtemp())


# ═══════════════════════════════════════════════════════════════════
# Factory
# ═══════════════════════════════════════════════════════════════════


def create_storage(config: dict) -> FileStorage:
    """Create the appropriate storage backend from *config*.

    Reads ``storage.backend``:

    * ``"remote"`` → :class:`S3Storage` (configured from
      ``storage.remote``).
    * otherwise → :class:`LocalStorage` (data dir from
      ``storage.local.data_dir``, default ``output``).
    """
    storage_cfg = config.get("storage", {})
    backend = storage_cfg.get("backend", "local")

    if backend == "remote":
        remote_cfg = storage_cfg.get("remote", {})
        return S3Storage(remote_cfg)

    data_dir = storage_cfg.get("local", {}).get("data_dir", "output")
    return LocalStorage(data_dir)
