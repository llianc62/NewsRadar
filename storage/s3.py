# coding=utf-8
"""Public S3 client — reusable by SQLite storage and cloud sync.

Extracted from the old ``storage.py`` so that ``sync.py`` no longer
needs to call private ``_download_from_s3()`` methods.
"""

import os
import tempfile
from pathlib import Path
from typing import Optional

HAS_BOTO3 = False
try:
    import boto3
    from botocore.config import Config as BotoConfig
    from botocore.exceptions import ClientError

    HAS_BOTO3 = True
except ImportError:
    pass


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
        if not HAS_BOTO3:
            raise ImportError("S3 storage requires boto3: pip install boto3")

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
        print(
            f"[S3Client] Ready, bucket={bucket_name}, "
            f"signature={signature_version}"
        )

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
            print(f"[S3Client] head_object failed ({key}): {e}")
            return False
        except Exception as e:
            print(f"[S3Client] head_object failed ({key}): {e}")
            return False

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

            print(f"[S3Client] Downloaded: {key} -> {local_path}")
            return True
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchKey", "Not Found"):
                print(f"[S3Client] Object not found: {key}")
            else:
                print(f"[S3Client] Download failed ({key}): {e}")
            return False
        except Exception as e:
            print(f"[S3Client] Download failed ({key}): {e}")
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

    def upload_file(
        self,
        local_path: Path,
        key: str,
        content_type: str = "application/x-sqlite3",
    ) -> bool:
        """Upload a local file to S3.

        Reads the entire file into memory to provide a concrete
        ``ContentLength`` (avoids chunked transfer-encoding issues
        with Tencent COS / Aliyun OSS).
        """
        if not local_path.exists():
            print(f"[S3Client] File not found: {local_path}")
            return False

        try:
            local_size = local_path.stat().st_size

            with open(local_path, "rb") as f:
                file_content = f.read()

            self._client.put_object(
                Bucket=self.bucket_name,
                Key=key,
                Body=file_content,
                ContentLength=local_size,
                ContentType=content_type,
            )
            print(f"[S3Client] Uploaded: {key} ({local_size} bytes)")
            return True
        except Exception as e:
            print(f"[S3Client] Upload failed ({key}): {e}")
            return False

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
            print(f"[S3Client] Presigned URL failed ({key}): {e}")
            return None

    # ── Factory ─────────────────────────────────────────────────────

    @classmethod
    def from_config(cls, config: dict) -> Optional["S3Client"]:
        """Create an S3Client from a config dict, or None if not configured.

        Config dict keys: bucket_name, access_key_id, secret_access_key,
        endpoint_url, region.
        Also checks env vars as fallback.
        """
        bucket = config.get("bucket_name") or os.environ.get("S3_BUCKET_NAME")
        access_key = config.get("access_key_id") or os.environ.get("S3_ACCESS_KEY_ID")
        secret_key = config.get("secret_access_key") or os.environ.get("S3_SECRET_ACCESS_KEY")
        endpoint = config.get("endpoint_url") or os.environ.get("S3_ENDPOINT_URL")
        region = config.get("region", "")

        if not all([bucket, access_key, secret_key, endpoint]):
            return None

        return cls(
            endpoint_url=endpoint,
            bucket_name=bucket,
            access_key_id=access_key,
            secret_access_key=secret_key,
            region=region,
        )
