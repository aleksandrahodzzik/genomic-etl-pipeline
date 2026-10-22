"""Variant normalization: chromosome naming, allele trimming, classification, INFO parsing."""

import uuid
from typing import Any, Dict, Optional, Tuple, Union

from app.etl.parser import VcfRecord

SNP = "SNP"
INSERTION = "INSERTION"
DELETION = "DELETION"
MNP = "MNP"
COMPLEX = "COMPLEX"

VARIANT_TYPES = (SNP, INSERTION, DELETION, MNP, COMPLEX)

TRANSITION_PAIRS = frozenset({("A", "G"), ("G", "A"), ("C", "T"), ("T", "C")})

MAX_AUTOSOME = 22


# --------------------------------------------------------------------------- #
# Chromosomes
# --------------------------------------------------------------------------- #
def normalize_chrom(raw: str) -> Optional[str]:
    """Return the canonical contig name (`chr1`..`chr22`, `chrX`, `chrY`, `chrM`).

    `1` -> `chr1`, `chr01` -> `chr1`, `MT` -> `chrM`, `x` -> `chrX`.
    Returns None for names that are not primary human chromosomes.
    """
    if raw is None:
        return None
    name = raw.strip()
    if name[:3].lower() == "chr":
        name = name[3:]
    name = name.upper()
    if name in {"M", "MT"}:
        return "chrM"
    if name in {"X", "Y"}:
        return f"chr{name}"
    if name.isdigit():
        number = int(name)
        if 1 <= number <= MAX_AUTOSOME:
            return f"chr{number}"
    return None


# --------------------------------------------------------------------------- #
# Allele trimming / left alignment
# --------------------------------------------------------------------------- #
def trim_alleles(pos: int, ref: str, alt: str) -> Tuple[int, str, str]:
    """Parsimonious allele trimming.

    1. Trim the shared suffix while both alleles are longer than one base.
    2. Trim the shared prefix while both alleles are longer than one base, moving
       `pos` right by one for every base removed from the left.

    >>> trim_alleles(100, "ATCG", "ATGG")
    (102, 'C', 'G')
    """
    while len(ref) > 1 and len(alt) > 1 and ref[-1] == alt[-1]:
        ref = ref[:-1]
        alt = alt[:-1]
    while len(ref) > 1 and len(alt) > 1 and ref[0] == alt[0]:
        ref = ref[1:]
        alt = alt[1:]
        pos += 1
    return pos, ref, alt


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #
def classify_variant(ref: str, alt: str) -> str:
    """Classify a trimmed (ref, alt) pair as SNP / INSERTION / DELETION / MNP / COMPLEX.

    Length rules decide the class. Length-changing events that are not a pure
    anchored indel (the shorter allele is neither a prefix nor a suffix of the longer
    one, e.g. `AT -> GCA`) are labelled COMPLEX instead of being mis-called as indels.
    """
    ref_len, alt_len = len(ref), len(alt)
    if ref_len == 1 and alt_len == 1:
        return SNP
    if ref_len == alt_len and ref_len > 1:
        return MNP
    if ref_len < alt_len:
        if ref and (alt.startswith(ref) or alt.endswith(ref)):
            return INSERTION
        return COMPLEX
    if ref_len > alt_len:
        if alt and (ref.startswith(alt) or ref.endswith(alt)):
            return DELETION
        return COMPLEX
    return COMPLEX


def is_transition(ref: str, alt: str, variant_type: str) -> bool:
    """True for purine<->purine (A<->G) or pyrimidine<->pyrimidine (C<->T) SNPs."""
    return variant_type == SNP and (ref, alt) in TRANSITION_PAIRS


# --------------------------------------------------------------------------- #
# INFO field
# --------------------------------------------------------------------------- #
def parse_info(info: str) -> Dict[str, Union[str, bool]]:
    """Parse `DP=112;AF=0.245;AC=2;DB` into a dict (flags map to True)."""
    parsed: Dict[str, Union[str, bool]] = {}
    if not info or info == ".":
        return parsed
    for token in info.split(";"):
        token = token.strip()
        if not token:
            continue
        key, sep, value = token.partition("=")
        parsed[key] = value if sep else True
    return parsed


def info_value_for_allele(
    info: Dict[str, Union[str, bool]], key: str, alt_index: int = 0
) -> Optional[str]:
    """Return the raw INFO value for the ALT allele at `alt_index`.

    Multi-allelic INFO values are comma separated (`AF=0.1,0.02`); a single value is
    treated as applying to every allele.
    """
    value = info.get(key)
    if value is None or value is True or value == ".":
        return None
    parts = str(value).split(",")
    if len(parts) == 1:
        return parts[0]
    if alt_index < len(parts):
        return parts[alt_index]
    return None


def parse_info_float(
    info: Dict[str, Union[str, bool]], key: str, alt_index: int = 0
) -> Optional[float]:
    """Float INFO value for an allele; None when absent. Raises ValueError if malformed."""
    raw = info_value_for_allele(info, key, alt_index)
    if raw is None or raw == "" or raw == ".":
        return None
    return float(raw)


def parse_info_int(
    info: Dict[str, Union[str, bool]], key: str, alt_index: int = 0
) -> Optional[int]:
    """Integer INFO value for an allele; None when absent or not an integer."""
    raw = info_value_for_allele(info, key, alt_index)
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except ValueError:
        try:
            return int(float(raw))
        except ValueError:
            return None


# --------------------------------------------------------------------------- #
# Record normalization
# --------------------------------------------------------------------------- #
def normalize_vcf_record(
    record: VcfRecord, job_id: Optional[uuid.UUID] = None
) -> Dict[str, Any]:
    """Convert a *validated* VCF record into a `genomic_variants` row dictionary."""
    chrom = normalize_chrom(record.chrom)
    if chrom is None:
        raise ValueError(f"unsupported chromosome: {record.chrom!r}")

    pos, ref, alt = trim_alleles(
        int(record.pos), record.ref.strip().upper(), record.alt.strip().upper()
    )
    variant_type = classify_variant(ref, alt)

    info = parse_info(record.info)
    try:
        allele_frequency = parse_info_float(info, "AF", record.alt_index)
    except ValueError:
        allele_frequency = None

    qual_text = record.qual.strip()
    quality: Optional[float] = None
    if qual_text and qual_text != ".":
        try:
            quality = float(qual_text)
        except ValueError:
            quality = None

    variant_id = record.variant_id.strip()
    filter_text = record.filter.strip()
    if not filter_text or filter_text == ".":
        filter_text = "UNFILTERED"

    return {
        "job_id": job_id,
        "chrom": chrom,
        "pos": pos,
        "variant_id": None if variant_id in {"", "."} else variant_id,
        "ref_allele": ref,
        "alt_allele": alt,
        "quality": quality,
        "filter_status": filter_text[:32],
        "variant_type": variant_type,
        "read_depth": parse_info_int(info, "DP", record.alt_index),
        "allele_frequency": allele_frequency,
        "is_transition": is_transition(ref, alt, variant_type),
    }
