"""PostgreSQL branch of the bulk loader, exercised with a fake DBAPI cursor."""

import uuid

import pytest

from app.etl import loader
from app.etl.loader import VARIANT_COLUMNS, bulk_insert_variants


class FakeCursor:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeRawConnection:
    def __init__(self) -> None:
        self.cursor_obj = FakeCursor()

    def cursor(self):
        return self.cursor_obj


class FakeConnection:
    def __init__(self, raw) -> None:
        self.connection = raw


class FakeDialect:
    name = "postgresql"


class FakeBind:
    dialect = FakeDialect()


class FakePgSession:
    def __init__(self) -> None:
        self.raw = FakeRawConnection()

    def get_bind(self):
        return FakeBind()

    def connection(self):
        return FakeConnection(self.raw)


def make_rows(count: int, job_id=None):
    job_id = job_id or uuid.uuid4()
    return [
        {
            "job_id": job_id,
            "chrom": "chr1",
            "pos": i + 1,
            "variant_id": None,
            "ref_allele": "A",
            "alt_allele": "G",
            "quality": 50.0,
            "filter_status": "PASS",
            "variant_type": "SNP",
            "read_depth": 10,
            "allele_frequency": 0.1,
            "is_transition": True,
        }
        for i in range(count)
    ]


@pytest.fixture()
def captured(monkeypatch):
    calls = []

    def fake_execute_values(cursor, sql, argslist, template=None, page_size=100, fetch=False):
        calls.append({"cursor": cursor, "sql": sql, "args": list(argslist), "page_size": page_size})

    monkeypatch.setattr(loader, "execute_values", fake_execute_values)
    return calls


def test_postgres_uses_execute_values_with_page_size(captured):
    session = FakePgSession()
    rows = make_rows(10)
    assert bulk_insert_variants(session, rows, chunk_size=100, page_size=10_000) == 10
    (call,) = captured
    assert call["page_size"] == 10_000
    assert call["cursor"] is session.raw.cursor_obj
    assert call["sql"] == (
        f"INSERT INTO genomic_variants ({', '.join(VARIANT_COLUMNS)}) VALUES %s"
    )
    assert len(call["args"]) == 10
    assert call["args"][0] == tuple(rows[0][c] for c in VARIANT_COLUMNS)


def test_postgres_splits_batch_into_db_chunks(captured):
    rows = make_rows(25)
    bulk_insert_variants(FakePgSession(), rows, chunk_size=10, page_size=5)
    assert [len(c["args"]) for c in captured] == [10, 10, 5]
    flattened = [args[2] for call in captured for args in call["args"]]  # pos column
    assert flattened == [row["pos"] for row in rows]  # order preserved, nothing lost


def test_postgres_defaults_come_from_settings(captured, monkeypatch):
    monkeypatch.setattr(loader.settings, "DB_CHUNK_SIZE", 4)
    monkeypatch.setattr(loader.settings, "DB_PAGE_SIZE", 2)
    bulk_insert_variants(FakePgSession(), make_rows(9))
    assert [len(c["args"]) for c in captured] == [4, 4, 1]
    assert {c["page_size"] for c in captured} == {2}


def test_postgres_empty_batch_issues_no_statements(captured):
    assert bulk_insert_variants(FakePgSession(), []) == 0
    assert captured == []
