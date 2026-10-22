"""Validation rules applied to a parsed record, in a fixed order (first failure wins):

    malformed_line, non_numeric_qual      (parse_record)
    unknown_contig                        contig not in the reference
    pos_out_of_bounds                     POS < 1 or the REF span runs past the contig end
    unsupported_symbolic                  ALT is <SYMBOLIC>, a breakend, '*' or '.'
    invalid_allele_chars                  REF/ALT contains anything outside A, C, G, T, N
    ref_mismatch                          REF differs from the reference (case-insensitive)

A record is accepted or rejected as a whole: if one ALT of a multi-allelic site is bad the
whole line goes to `rejected_records`, so one input line never ends up half in the database.
"""

from variant_etl.pipeline.records import RecordRejected, RejectReason
from variant_etl.pipeline.reference import Reference
from variant_etl.pipeline.vcf import VcfRecord

ALLOWED_BASES = frozenset("ACGTN")


def _is_symbolic(alt: str) -> bool:
    return alt in {"*", "."} or "<" in alt or ">" in alt or "[" in alt or "]" in alt


def validate_record(record: VcfRecord, reference: Reference) -> str:
    """Return the reference's canonical contig name, or raise RecordRejected."""
    chrom = reference.resolve(record.chrom)
    if chrom is None:
        raise RecordRejected(RejectReason.UNKNOWN_CONTIG)

    if record.pos < 1 or record.pos + len(record.ref) - 1 > reference.contig_length(chrom):
        raise RecordRejected(RejectReason.POS_OUT_OF_BOUNDS)

    if any(_is_symbolic(alt) for alt in record.alts):
        raise RecordRejected(RejectReason.UNSUPPORTED_SYMBOLIC)

    if not ALLOWED_BASES.issuperset(record.ref) or any(
        not ALLOWED_BASES.issuperset(alt) for alt in record.alts
    ):
        raise RecordRejected(RejectReason.INVALID_ALLELE_CHARS)

    expected = reference.fetch(chrom, record.pos - 1, record.pos - 1 + len(record.ref))
    if expected != record.ref:
        raise RecordRejected(RejectReason.REF_MISMATCH)

    # ALT == REF is not a variant, and normalization would trim it to two empty alleles.
    if record.ref in record.alts:
        raise RecordRejected(RejectReason.MALFORMED_LINE)

    return chrom
