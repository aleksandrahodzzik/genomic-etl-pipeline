"""initial schema: jobs, job_chunks, variants, rejected_records

Index trade-off during bulk load: both variants indexes exist while chunks are inserted, so
every COPY also maintains two B-trees. That costs write throughput (measured in
docs/BENCHMARKS.md) but keeps partial results queryable while a job is still running and
keeps the pipeline simple. For a one-off, offline load of a very large cohort the alternative
is to create the indexes after the load (CREATE INDEX CONCURRENTLY), which builds each B-tree
in one sorted pass instead of one insertion per row.

Revision ID: 0001
Revises:
Create Date: 2026-10-22
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("vcf_key", sa.Text(), nullable=False),
        sa.Column("reference_key", sa.Text(), nullable=False),
        sa.Column("checksum", sa.String(length=64), nullable=False),
        sa.Column("total", sa.Integer(), nullable=False),
        sa.Column("accepted", sa.Integer(), nullable=False),
        sa.Column("rejected", sa.Integer(), nullable=False),
        sa.Column("normalized", sa.Integer(), nullable=False),
        sa.Column("chunk_count", sa.Integer(), nullable=True),
        sa.Column("contigs", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("checksum"),
    )
    op.create_table(
        "job_chunks",
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("total", sa.Integer(), nullable=False),
        sa.Column("accepted", sa.Integer(), nullable=False),
        sa.Column("rejected", sa.Integer(), nullable=False),
        sa.Column("normalized", sa.Integer(), nullable=False),
        sa.Column(
            "reject_reasons",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("job_id", "chunk_index"),
    )
    op.create_table(
        "variants",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("chrom", sa.Text(), nullable=False),
        sa.Column("pos", sa.Integer(), nullable=False),
        sa.Column("ref", sa.Text(), nullable=False),
        sa.Column("alt", sa.Text(), nullable=False),
        sa.Column("qual", sa.Float(), nullable=True),
        sa.Column("filter", sa.Text(), nullable=True),
        sa.Column("variant_type", sa.String(length=8), nullable=False),
        sa.Column("is_transition", sa.Boolean(), nullable=False),
        sa.Column("af", sa.Float(), nullable=True),
        sa.Column("was_normalized", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_variants_job_chrom_pos", "variants", ["job_id", "chrom", "pos"])
    op.create_index("ix_variants_job_type", "variants", ["job_id", "variant_type"])
    op.create_table(
        "rejected_records",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("line_no", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(length=32), nullable=False),
        sa.Column("raw_line", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_rejected_job_chunk", "rejected_records", ["job_id", "chunk_index"])


def downgrade() -> None:
    op.drop_index("ix_rejected_job_chunk", table_name="rejected_records")
    op.drop_table("rejected_records")
    op.drop_index("ix_variants_job_type", table_name="variants")
    op.drop_index("ix_variants_job_chrom_pos", table_name="variants")
    op.drop_table("variants")
    op.drop_table("job_chunks")
    op.drop_table("jobs")
