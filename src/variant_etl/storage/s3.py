"""Object storage over the plain S3 API (boto3).

Nothing here is MinIO-specific: pointing S3_ENDPOINT_URL at AWS S3, Garage or SeaweedFS
works the same way. Path-style addressing is forced because most self-hosted S3 servers
are reached by IP or a single hostname, not by `<bucket>.<host>`.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from variant_etl.config import Settings, get_settings

NOT_FOUND_CODES = {"404", "NoSuchKey", "NotFound"}
TRANSIENT_CLIENT_CODES = {"RequestTimeout", "SlowDown", "Throttling", "ThrottlingException"}


class ObjectNotFound(Exception):
    def __init__(self, key: str) -> None:
        super().__init__(f"object not found: {key}")
        self.key = key


@dataclass(frozen=True)
class ObjectInfo:
    key: str
    etag: str
    size: int


def is_transient_client_error(exc: ClientError) -> bool:
    """Server-side or throttling failures are worth retrying; 4xx (missing key, denied) are not."""
    status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
    code = exc.response.get("Error", {}).get("Code", "")
    return status >= 500 or code in TRANSIENT_CLIENT_CODES


class ObjectStorage:
    def __init__(self, settings: Settings) -> None:
        self.bucket = settings.s3_bucket
        self._expires = settings.presign_expires_seconds
        config = Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            retries={"max_attempts": 3, "mode": "standard"},
        )

        def client(endpoint: str):
            return boto3.client(
                "s3",
                endpoint_url=endpoint,
                aws_access_key_id=settings.s3_access_key,
                aws_secret_access_key=settings.s3_secret_key,
                region_name=settings.s3_region,
                config=config,
            )

        self._client = client(settings.s3_endpoint_url)
        # Presigning is a local computation (no request is made), but the host is part of
        # the signature, so the URL must be signed for the address the *caller* will use.
        self._signer = client(settings.s3_public_endpoint_url)

    def ensure_bucket(self) -> None:
        try:
            self._client.head_bucket(Bucket=self.bucket)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") not in NOT_FOUND_CODES:
                raise
            self._client.create_bucket(Bucket=self.bucket)

    def presign_put(self, key: str) -> str:
        return str(
            self._signer.generate_presigned_url(
                "put_object",
                Params={"Bucket": self.bucket, "Key": key},
                ExpiresIn=self._expires,
            )
        )

    def head(self, key: str) -> ObjectInfo:
        try:
            response = self._client.head_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in NOT_FOUND_CODES:
                raise ObjectNotFound(key) from exc
            raise
        return ObjectInfo(key, response["ETag"].strip('"'), int(response["ContentLength"]))

    def iter_blocks(self, key: str, block_size: int = 1 << 20) -> Iterator[bytes]:
        """Stream an object in blocks; memory use does not depend on object size."""
        try:
            body = self._client.get_object(Bucket=self.bucket, Key=key)["Body"]
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in NOT_FOUND_CODES:
                raise ObjectNotFound(key) from exc
            raise
        try:
            yield from body.iter_chunks(block_size)
        finally:
            body.close()

    def put_bytes(self, key: str, data: bytes) -> None:
        self._client.put_object(Bucket=self.bucket, Key=key, Body=data)

    def upload_file(self, path: Path, key: str) -> None:
        self._client.upload_file(str(path), self.bucket, key)

    def delete_prefix(self, prefix: str) -> int:
        """Delete every object under a prefix (used to clean up chunk objects)."""
        deleted = 0
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            objects = [{"Key": item["Key"]} for item in page.get("Contents", [])]
            if objects:
                self._client.delete_objects(Bucket=self.bucket, Delete={"Objects": objects})
                deleted += len(objects)
        return deleted


@lru_cache
def get_storage() -> ObjectStorage:
    return ObjectStorage(get_settings())
