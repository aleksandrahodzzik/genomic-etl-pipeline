"""Transform + high-throughput bulk loading into PostgreSQL.

PostgreSQL uses `psycopg2.extras.execute_values` (multi-row `INSERT ... VALUES %s`
statements of `page_size` rows). Other dialects (SQLite in unit tests) fall back to
chunked SQLAlchemy Core `insert()` executemany. A deliberately naive row-by-row ORM
loader is also provided so the benchmark can measure the improvement.
"""

import uuid
from dataclasses import dataclass, field
from operator import itemgetter
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from psycopg2.extras import execute_values, register_uuid
from sqlalchemy import insert
from sqlalchemy.orm import Session

from app.config import settings
from app.etl.normalizer import normalize_vcf_record
from app.etl.parser import FastaContigRecord, VcfRecord
from app.etl.validator import validate_vcf_record
from app.models import FastaContig, GenomicVariant

register_uuid()  # lets psycopg2 adapt uuid.UUID parameters

VARIANT_COLUMNS: Tuple[str, ...] = (
    "job_id",
    "chrom",
    "pos",
    "variant_id",
    "ref_allele",
    "alt_allele",
    "quality",
    "filter_status",
    "variant_type",
    "read_depth",
    "allele_frequency",
    "is_transition",
)
_variant_getter = itemgetter(*VARIANT_COLUMNS)

MAX_REJECTION_SAMPLES = 10


@dataclass
class TransformResult:
    """Outcome of validating + normalizing one parsed batch."""

    rows: List[Dict[str, Any]] = field(default_factory=list)
    total: int = 0
    rejected: int = 0
    rejection_samples: List[Tuple[int, str]] = field(default_factory=list)

    @property
    def valid(self) -> int:
        return len(self.rows)


def transform_records(
    records: Iterable[VcfRecord], job_id: Optional[uuid.UUID]
) -> TransformResult:
    """Validate and normalize a batch of parsed VCF records."""
    result = TransformResult()
    for record in records:
        result.total += 1
        verdict = validate_vcf_record(record)
        if not verdict.is_valid:
            result.rejected += 1
            if len(result.rejection_samples) < MAX_REJECTION_SAMPLES:
                result.rejection_samples.append(
                    (record.line_number, verdict.reason or "unknown")
                )
            continue
        result.rows.append(normalize_vcf_record(record, job_id))
    return result


def _is_postgres(session: Session) -> bool:
    return session.get_bind().dialect.name == "postgresql"


def bulk_insert_variants(
    session: Session,
    rows: Sequence[Dict[str, Any]],
    chunk_size: Optional[int] = None,
    page_size: Optional[int] = None,
) -> int:
    """Bulk insert normalized variant dictionaries. The caller owns commit/rollback.

    Rows are sent in `chunk_size` round trips (default `DB_CHUNK_SIZE`); on PostgreSQL
    each chunk is expanded into multi-row INSERTs of `page_size` rows (default 10,000).
    """
    if not rows:
        return 0
    chunk_size = chunk_size or settings.DB_CHUNK_SIZE
    page_size = page_size or settings.DB_PAGE_SIZE

    if _is_postgres(session):
        columns = ", ".join(VARIANT_COLUMNS)
        sql = f"INSERT INTO genomic_variants ({columns}) VALUES %s"
        raw_connection = session.connection().connection
        with raw_connection.cursor() as cursor:
            for start in range(0, len(rows), chunk_size):
                chunk = rows[start : start + chunk_size]
                execute_values(
                    cursor,
                    sql,
                    [_variant_getter(row) for row in chunk],
                    page_size=page_size,
                )
    else:
        table = GenomicVariant.__table__
        for start in range(0, len(rows), chunk_size):
            session.execute(insert(table), list(rows[start : start + chunk_size]))
    return len(rows)


def bulk_insert_contigs(
    session: Session, job_id: uuid.UUID, contigs: Sequence[FastaContigRecord]
) -> int:
    """Insert FASTA contig statistics (small volume, plain Core executemany)."""
    if not contigs:
        return 0
    payload = [
        {
            "job_id": job_id,
            "contig_id": contig.contig_id[:128],
            "description": contig.description[:512] if contig.description else None,
            "sequence_length": contig.sequence_length,
            "gc_content_percent": contig.gc_content_percent,
            "n_base_count": contig.n_count,
            "md5_checksum": contig.md5_checksum,
        }
        for contig in contigs
    ]
    session.execute(insert(FastaContig.__table__), payload)
    return len(payload)


def naive_insert_variants(session: Session, rows: Sequence[Dict[str, Any]]) -> int:
    """Baseline loader: one ORM object + one commit per row. Benchmark use only."""
    for row in rows:
        session.add(GenomicVariant(**row))
        session.commit()
    return len(rows)
