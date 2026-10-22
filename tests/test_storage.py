"""S3StorageClient behaviour against a fake boto3 client (no MinIO required)."""

import io

import pytest
from botocore.exceptions import ClientError

from app.storage import S3StorageClient


def client_error(code: str, operation: str = "HeadBucket") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, operation)


class FakeBody:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.closed = False
        self.chunk_sizes = []

    def iter_lines(self, chunk_size: int = 1024, keepends: bool = False):
        self.chunk_sizes.append(chunk_size)
        yield from self.payload.splitlines()

    def close(self) -> None:
        self.closed = True


class FakeBoto:
    def __init__(self, head_error=None, create_error=None, body: bytes = b"") -> None:
        self.head_error = head_error
        self.create_error = create_error
        self.body = FakeBody(body)
        self.created = []
        self.put = []
        self.uploaded = []
        self.fetched = []

    def head_bucket(self, Bucket):
        if self.head_error is not None:
            raise self.head_error

    def create_bucket(self, Bucket):
        if self.create_error is not None:
            raise self.create_error
        self.created.append(Bucket)

    def put_object(self, Bucket, Key, Body):
        self.put.append((Bucket, Key, Body))

    def upload_fileobj(self, fileobj, bucket, key):
        self.uploaded.append((fileobj.read(), bucket, key))

    def get_object(self, Bucket, Key):
        self.fetched.append((Bucket, Key))
        return {"Body": self.body}


@pytest.fixture()
def make_client(monkeypatch):
    def factory(**kwargs) -> tuple:
        fake = FakeBoto(**kwargs)
        monkeypatch.setattr("app.storage.boto3.client", lambda *a, **k: fake)
        return S3StorageClient(bucket_name="test-bucket"), fake

    return factory


def test_existing_bucket_is_not_recreated(make_client):
    _, fake = make_client()
    assert fake.created == []


@pytest.mark.parametrize("code", ["404", "NoSuchBucket", "NotFound"])
def test_missing_bucket_is_created_on_init(make_client, code):
    _, fake = make_client(head_error=client_error(code))
    assert fake.created == ["test-bucket"]


def test_unexpected_head_error_is_propagated(make_client):
    with pytest.raises(ClientError):
        make_client(head_error=client_error("403"))


def test_bucket_creation_race_is_tolerated(make_client):
    # another process (api/worker start-up race) created the bucket first
    make_client(
        head_error=client_error("404"),
        create_error=client_error("BucketAlreadyOwnedByYou", "CreateBucket"),
    )


def test_bucket_creation_failure_is_propagated(make_client):
    with pytest.raises(ClientError):
        make_client(
            head_error=client_error("404"),
            create_error=client_error("AccessDenied", "CreateBucket"),
        )


def test_upload_bytes_and_fileobj(make_client):
    client, fake = make_client()
    client.upload_bytes(b"abc", "k/one")
    client.upload_fileobj(io.BytesIO(b"streamed"), "k/two")
    assert fake.put == [("test-bucket", "k/one", b"abc")]
    assert fake.uploaded == [(b"streamed", "test-bucket", "k/two")]


def test_stream_lines_decodes_streams_in_1mib_chunks_and_closes(make_client):
    client, fake = make_client(body="line é1\nline2\r\nline3".encode("utf-8"))
    lines = list(client.stream_lines("k/file.vcf"))
    assert lines == ["line é1", "line2", "line3"]
    assert fake.fetched == [("test-bucket", "k/file.vcf")]
    assert fake.body.chunk_sizes == [1024 * 1024]
    assert fake.body.closed is True


def test_stream_lines_closes_body_when_consumer_stops_early(make_client):
    client, fake = make_client(body=b"a\nb\nc\n")
    stream = client.stream_lines("k")
    assert next(stream) == "a"
    stream.close()
    assert fake.body.closed is True


def test_stream_lines_replaces_undecodable_bytes(make_client):
    client, _ = make_client(body=b"ok\n\xff\xfe bad\n")
    lines = list(client.stream_lines("k"))
    assert lines[0] == "ok"
    assert "bad" in lines[1]


def test_custom_chunk_size_is_forwarded(make_client):
    client, fake = make_client(body=b"x\n")
    list(client.stream_lines("k", chunk_size=4096))
    assert fake.body.chunk_sizes == [4096]


def test_ping_checks_bucket(make_client):
    client, fake = make_client()
    assert client.ping() is True
    fake.head_error = client_error("500")
    with pytest.raises(ClientError):
        client.ping()
