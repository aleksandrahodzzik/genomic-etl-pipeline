"""Plain data types shared by the pipeline stages."""

from enum import StrEnum
from typing import NamedTuple


class RejectReason(StrEnum):
    """Why a record was refused. Values are stored in the database and returned by the API."""

    MALFORMED_LINE = "malformed_line"
    NON_NUMERIC_QUAL = "non_numeric_qual"
    UNKNOWN_CONTIG = "unknown_contig"
    POS_OUT_OF_BOUNDS = "pos_out_of_bounds"
    UNSUPPORTED_SYMBOLIC = "unsupported_symbolic"
    INVALID_ALLELE_CHARS = "invalid_allele_chars"
    REF_MISMATCH = "ref_mismatch"


class VariantType(StrEnum):
    SNV = "SNV"
    MNV = "MNV"
    INS = "INS"
    DEL = "DEL"
    COMPLEX = "COMPLEX"


class RecordRejected(Exception):
    """Raised by parsing/validation/normalization; carries the machine-readable reason."""

    def __init__(self, reason: RejectReason) -> None:
        super().__init__(reason.value)
        self.reason = reason


class VariantRow(NamedTuple):
    """One normalized, biallelic variant. Field order is the COPY column order (after
    job_id and chunk_index, which the loader prepends)."""

    chrom: str
    pos: int
    ref: str
    alt: str
    qual: float | None
    filter: str | None
    variant_type: str
    is_transition: bool
    af: float | None
    was_normalized: bool


class RejectedLine(NamedTuple):
    line_no: int
    reason: str
    raw_line: str
