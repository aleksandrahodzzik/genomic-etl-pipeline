"""Record validation. Returns `(is_valid, reason)` instead of raising, so bad rows are counted."""

import math
from typing import NamedTuple, Optional

from app.etl.normalizer import (
    info_value_for_allele,
    normalize_chrom,
    parse_info,
)
from app.etl.parser import VcfRecord

VALID_BASES = frozenset({"A", "C", "G", "T", "N"})
MAX_ALLELE_LENGTH = 512
MAX_VARIANT_ID_LENGTH = 64
MAX_POSITION = 2_147_483_647  # PostgreSQL INTEGER upper bound


class ValidationResult(NamedTuple):
    is_valid: bool
    reason: Optional[str]


OK = ValidationResult(True, None)


def _reject(reason: str) -> ValidationResult:
    return ValidationResult(False, reason)


def _invalid_allele(allele: str) -> bool:
    return not allele or any(base not in VALID_BASES for base in allele)


def validate_variant(
    chrom: str,
    pos: int,
    ref: str,
    alt: str,
    quality: Optional[float] = None,
    allele_frequency: Optional[float] = None,
) -> ValidationResult:
    """Validate already-typed variant fields."""
    if normalize_chrom(chrom) is None:
        return _reject(f"invalid chromosome '{chrom}'")
    if pos is None or pos <= 0:
        return _reject(f"position must be > 0 (got {pos})")
    if pos > MAX_POSITION:
        return _reject(f"position {pos} exceeds supported range")

    ref_u = (ref or "").strip().upper()
    alt_u = (alt or "").strip().upper()
    if not ref_u:
        return _reject("empty REF allele")
    if not alt_u or alt_u == ".":
        return _reject("empty ALT allele")
    if _invalid_allele(ref_u):
        return _reject(f"invalid characters in REF allele '{ref}'")
    if _invalid_allele(alt_u):
        return _reject(f"invalid or symbolic ALT allele '{alt}'")
    if len(ref_u) > MAX_ALLELE_LENGTH or len(alt_u) > MAX_ALLELE_LENGTH:
        return _reject(f"allele longer than {MAX_ALLELE_LENGTH} bases")
    if ref_u == alt_u:
        return _reject("REF and ALT alleles are identical")

    if quality is not None and (not math.isfinite(quality) or quality < 0):
        return _reject(f"QUAL must be a finite number >= 0 (got {quality})")
    if allele_frequency is not None and not 0.0 <= allele_frequency <= 1.0:
        return _reject(f"allele frequency {allele_frequency} outside [0, 1]")
    return OK


def validate_vcf_record(record: VcfRecord) -> ValidationResult:
    """Validate a raw (all-text) decomposed VCF record."""
    if record.parse_error:
        return _reject(record.parse_error)

    try:
        pos = int(record.pos)
    except ValueError:
        return _reject(f"non-integer POS '{record.pos}'")

    qual_text = record.qual.strip()
    quality: Optional[float] = None
    if qual_text and qual_text != ".":
        try:
            quality = float(qual_text)
        except ValueError:
            return _reject(f"non-numeric QUAL '{record.qual}'")

    info = parse_info(record.info)
    allele_frequency: Optional[float] = None
    raw_af = info_value_for_allele(info, "AF", record.alt_index)
    if raw_af not in (None, "", "."):
        try:
            allele_frequency = float(raw_af)  # type: ignore[arg-type]
        except ValueError:
            return _reject(f"non-numeric AF '{raw_af}'")

    if len(record.variant_id.strip()) > MAX_VARIANT_ID_LENGTH:
        return _reject(f"variant ID longer than {MAX_VARIANT_ID_LENGTH} characters")

    return validate_variant(
        record.chrom, pos, record.ref, record.alt, quality, allele_frequency
    )
