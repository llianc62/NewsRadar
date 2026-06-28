# coding=utf-8
"""S3-compatible object storage client.

Supports MinIO, AWS S3, Tencent COS, Aliyun OSS, Cloudflare R2.
Provides SigV2/SigV4 auto-detection, an ``init_by_config`` factory,
and presigned URL generation.
"""

import logging
import os
import tempfile
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError


class S3Client:
    """S3-compatible object storage client.

    Auto-detects SigV2 vs SigV4 based on endpoint domain:

    * Tencent COS / Aliyun OSS → SigV2 (avoids chunked-encoding issues)
    * AWS S3 / Cloudflare R2 / MinIO → SigV4

    Usage::

        client = S3Client(
            endpoint_url="https://s3.example.com",
            bucket_name="my-bucket",
            access_key="AKIA...",
            secret_key="...",
        )
        client.download_file("db/2026-06-06.db", Path("/tmp/out.db"))
        client.upload_file(Path("/tmp/out.db"), "db/2026-06-06.db")
    """

    def __init__(
        self,
        endpoint_url: str,
        bucket_name: str,
        access_key_id: str,
        secret_access_key: str,
        region: str = "",
    ):

        # Normalize endpoint URL — boto3 requires a scheme
        endpoint_url = endpoint_url.strip()
        if not endpoint_url.startswith(("http://", "https://")):
            endpoint_url = f"https://{endpoint_url}"

        self.endpoint_url = endpoint_url
        self.bucket_name = bucket_name

        # Auto-detect signature version
        endpoint_lower = endpoint_url.lower()
        use_sigv2 = (
            "myqcloud.com" in endpoint_lower
            or "aliyuncs.com" in endpoint_lower
        )
        signature_version = "s3" if use_sigv2 else "s3v4"

        boto_config = BotoConfig(
            s3={"addressing_style": "path"},
            signature_version=signature_version,
        )

        client_kwargs = {
            "endpoint_url": endpoint_url,
            "aws_access_key_id": access_key_id,
            "aws_secret_access_key": secret_access_key,
            "config": boto_config,
        }
        if region:
            client_kwargs["region_name"] = region

        self._client = boto3.client("s3", **client_kwargs)
        logger.info(
            "S3Client ready, bucket=%s, signature=%s",
            bucket_name, signature_version,
        )
        self._ensure_bucket()

    # ── Bucket lifecycle ───────────────────────────────────────────────

    def _ensure_bucket(self) -> None:
        """Create the bucket if it doesn't already exist.

        Raises:
            botocore.exceptions.ClientError: If bucket access or creation
                fails for reasons other than "bucket does not exist yet".
        """
        try:
            self._client.head_bucket(Bucket=self.bucket_name)
            logger.info("Bucket '%s' already exists", self.bucket_name)
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchBucket", "Not Found"):
                try:
                    self._client.create_bucket(Bucket=self.bucket_name)
                    logger.info("Bucket '%s' created", self.bucket_name)
                except Exception:
                    logger.exception(
                        "Failed to create bucket '%s'", self.bucket_name
                    )
                    raise
            else:
                logger.error(
                    "head_bucket failed for '%s': %s", self.bucket_name, e
                )
                raise

    # ── Existence check ─────────────────────────────────────────────

    def object_exists(self, key: str) -> bool:
        """Return True if *key* exists in the bucket."""
        try:
            self._client.head_object(Bucket=self.bucket_name, Key=key)
            return True
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchKey", "Not Found"):
                return False
            logger.warning("head_object failed (%s): %s", key, e)
            return False

    # ── List objects ──────────────────────────────────────────────────

    def list_objects(
        self,
        prefix: str = "",
        max_keys: int = 1000,
    ) -> List[str]:
        """List object keys in the bucket, optionally filtered by *prefix*.

        Uses ``list_objects_v2`` paginator to handle buckets with more
        than 1000 objects.  Returns an empty list on failure.
        """
        try:
            keys: List[str] = []
            paginator = self._client.get_paginator("list_objects_v2")
            pages = paginator.paginate(
                Bucket=self.bucket_name,
                Prefix=prefix,
                PaginationConfig={"MaxItems": max_keys, "PageSize": 1000},
            )
            for page in pages:
                for obj in page.get("Contents", []):
                    keys.append(obj["Key"])
            return keys
        except Exception as e:
            logger.error("list_objects failed (prefix=%r): %s", prefix, e)
            return []

    # ── Download ────────────────────────────────────────────────────

    def download_file(self, key: str, local_path: Path) -> bool:
        """Download an object to a local file. Returns True on success."""
        try:
            response = self._client.get_object(
                Bucket=self.bucket_name, Key=key
            )
            with open(local_path, "wb") as f:
                for chunk in response["Body"].iter_chunks(
                    chunk_size=1024 * 1024
                ):
                    f.write(chunk)

            logger.info("Downloaded: %s -> %s", key, local_path)
            return True
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchKey", "Not Found"):
                logger.info("Object not found: %s", key)
            else:
                logger.error("Download failed (%s): %s", key, e)
            return False
        except Exception as e:
            logger.error("Download failed (%s): %s", key, e)
            return False

    def download_to_temp(self, key: str) -> Optional[Path]:
        """Download an object to a temp file. Returns the path or None."""
        tmp_fd, tmp_path = tempfile.mkstemp(
            suffix=".db", prefix="s3_download_"
        )
        os.close(tmp_fd)
        tmp_path = Path(tmp_path)

        if self.download_file(key, tmp_path):
            return tmp_path

        # Clean up on failure
        try:
            os.unlink(str(tmp_path))
        except OSError:
            pass
        return None

    # ── Upload ──────────────────────────────────────────────────────

    def upload(
        self,
        data: bytes,
        key: str,
        content_type: str = "application/octet-stream",
    ) -> bool:
        """Upload raw bytes directly to S3 (no temp file needed).

        Args:
            data: Raw bytes to upload.
            key: Object key in the bucket.
            content_type: MIME type for the object.

        Returns:
            True on success.
        """
        try:
            self._client.put_object(
                Bucket=self.bucket_name,
                Key=key,
                Body=data,
                ContentLength=len(data),
                ContentType=content_type,
            )
            logger.info("Uploaded: %s (%d bytes)", key, len(data))
            return True
        except Exception as e:
            logger.error("Upload failed (%s): %s", key, e)
            return False

    def upload_file(
        self,
        local_path: Path,
        key: str,
        content_type: str,
    ) -> bool:
        """Upload a local file to S3.

        Delegates to :meth:`upload` after reading the file into memory
        (avoids chunked transfer-encoding issues with Tencent COS /
        Aliyun OSS).
        """
        if not local_path.exists():
            logger.warning("File not found: %s", local_path)
            return False

        with open(local_path, "rb") as f:
            data = f.read()

        return self.upload(data, key, content_type)

    # ── Presigned URLs ──────────────────────────────────────────────

    def presigned_get_url(
        self, key: str, expires_in: int = 604800
    ) -> Optional[str]:
        """Generate a presigned GET URL for *key*.

        Args:
            key: Object key in the bucket.
            expires_in: URL validity in seconds (default 7 days).

        Returns:
            Presigned URL string, or None on failure.
        """
        try:
            return self._client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.bucket_name, "Key": key},
                ExpiresIn=expires_in,
            )
        except Exception as e:
            logger.error("Presigned URL failed (%s): %s", key, e)
            return None

    # ── Factory ─────────────────────────────────────────────────────

    @classmethod
    def init_by_config(cls, config: dict) -> Optional["S3Client"]:
        """Create an S3Client from a config dict.

        Config dict keys: bucket_name, access_key_id, secret_access_key,
        endpoint_url, region.

        Returns ``None`` when S3 is *not* configured (all required keys
        are empty or missing). This is the legitimate "S3 disabled" case.

        Raises ``ValueError`` when S3 is *partially* configured — some
        required keys are present but not all. This is a configuration
        error and should fail fast.
        """
        required = ["endpoint_url", "bucket_name", "access_key_id", "secret_access_key"]
        vals = {k: config.get(k, "") for k in required}

        missing = [k for k, v in vals.items() if not v]
        if len(missing) == len(required):
            return None
        if missing:
            raise ValueError(f"S3 partially configured. Missing: {', '.join(missing)}")

        return cls(**vals, region=config.get("region", ""))
