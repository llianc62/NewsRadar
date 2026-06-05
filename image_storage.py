# coding=utf-8
"""MinIO image storage — S3-compatible client wrapper."""

from pathlib import Path
from typing import Optional

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError


class ImageStorage:
    """MinIO / S3-compatible image storage.

    Usage::

        store = ImageStorage(endpoint_url="http://localhost:9000",
                             bucket_name="newsradar-images",
                             access_key="minioadmin",
                             secret_key="minioadmin")
        store.ensure_bucket()
        url = store.upload_image(local_path, "2026-06/1/image_01.jpg")
    """

    def __init__(
        self,
        endpoint_url: str,
        bucket_name: str,
        access_key: str,
        secret_key: str,
        region: str = "",
    ):
        self.endpoint_url = endpoint_url
        self.bucket_name = bucket_name

        boto_config = BotoConfig(
            s3={"addressing_style": "path"},
            signature_version="s3v4",
        )

        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            config=boto_config,
            region_name=region or "us-east-1",
        )

    def ensure_bucket(self) -> None:
        """Create bucket if it doesn't exist."""
        try:
            self.client.head_bucket(Bucket=self.bucket_name)
            print(f"[ImageStorage] Bucket '{self.bucket_name}' exists")
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchBucket", "Not Found"):
                self.client.create_bucket(Bucket=self.bucket_name)
                print(f"[ImageStorage] Created bucket '{self.bucket_name}'")
            else:
                raise

    def upload_image(
        self,
        local_path: Path,
        object_key: str,
        content_type: Optional[str] = None,
    ) -> str:
        """Upload an image file and return the public URL.

        Args:
            local_path: Path to the local image file.
            object_key: S3 object key (e.g. '2026-06/1/image_01.jpg').
            content_type: MIME type. Auto-detected from extension if None.

        Returns:
            Full URL to the uploaded image.
        """
        if content_type is None:
            content_type = self._guess_content_type(local_path)

        file_size = local_path.stat().st_size

        self.client.upload_file(
            str(local_path),
            self.bucket_name,
            object_key,
            ExtraArgs={"ContentType": content_type},
        )

        url = f"{self.endpoint_url}/{self.bucket_name}/{object_key}"
        print(f"[ImageStorage] Uploaded: {object_key} ({file_size} bytes)")
        return url

    def delete_image(self, object_key: str) -> bool:
        """Delete an image. Returns True on success."""
        try:
            self.client.delete_object(
                Bucket=self.bucket_name, Key=object_key
            )
            print(f"[ImageStorage] Deleted: {object_key}")
            return True
        except ClientError as e:
            print(f"[ImageStorage] Delete failed: {object_key}: {e}")
            return False

    @staticmethod
    def _guess_content_type(path: Path) -> str:
        ext = path.suffix.lower()
        mapping = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".gif": "image/gif",
            ".webp": "image/webp",
            ".svg": "image/svg+xml",
        }
        return mapping.get(ext, "application/octet-stream")
