"""S3-compatible (MinIO) object storage client with constant-memory streaming reads."""

import logging
from functools import lru_cache
from typing import BinaryIO, Iterator

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

from app.config import settings

logger = logging.getLogger(__name__)

STREAM_CHUNK_SIZE = 1024 * 1024  # 1 MiB


class S3StorageClient:
    """Thin boto3 wrapper around a single bucket."""

    def __init__(
        self,
        endpoint_url: str = settings.S3_ENDPOINT_URL,
        access_key: str = settings.S3_ACCESS_KEY,
        secret_key: str = settings.S3_SECRET_KEY,
        bucket_name: str = settings.S3_BUCKET_NAME,
        region_name: str = settings.S3_REGION,
    ) -> None:
        self.bucket_name = bucket_name
        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region_name,
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
                retries={"max_attempts": 5, "mode": "standard"},
                connect_timeout=5,
                read_timeout=60,
            ),
        )
        self.ensure_bucket()

    def ensure_bucket(self) -> None:
        """Create the bucket when it does not exist yet."""
        try:
            self.client.head_bucket(Bucket=self.bucket_name)
            return
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code not in {"404", "NoSuchBucket", "NotFound"}:
                raise
        try:
            self.client.create_bucket(Bucket=self.bucket_name)
            logger.info("Created bucket %s", self.bucket_name)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            # Another process (api/worker start-up race) may have created it first.
            if code not in {"BucketAlreadyOwnedByYou", "BucketAlreadyExists"}:
                raise

    def upload_fileobj(self, file_obj: BinaryIO, s3_key: str) -> None:
        """Upload a binary stream (multipart for large objects)."""
        self.client.upload_fileobj(file_obj, self.bucket_name, s3_key)

    def upload_bytes(self, data: bytes, s3_key: str) -> None:
        """Upload raw bytes."""
        self.client.put_object(Bucket=self.bucket_name, Key=s3_key, Body=data)

    def stream_lines(
        self, s3_key: str, chunk_size: int = STREAM_CHUNK_SIZE
    ) -> Iterator[str]:
        """Yield decoded text lines, reading the object body in `chunk_size` byte chunks.

        The object is never fully materialised in memory: only one chunk plus the
        current partial line is resident at any time.
        """
        response = self.client.get_object(Bucket=self.bucket_name, Key=s3_key)
        body = response["Body"]
        try:
            for raw_line in body.iter_lines(chunk_size=chunk_size):
                yield raw_line.decode("utf-8", errors="replace")
        finally:
            body.close()

    def ping(self) -> bool:
        """Connectivity probe used by the health endpoint."""
        self.client.head_bucket(Bucket=self.bucket_name)
        return True


@lru_cache
def get_storage() -> S3StorageClient:
    """Process-wide storage client (bucket creation happens once, on first use)."""
    return S3StorageClient()
