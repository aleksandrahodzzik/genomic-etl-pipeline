"""VCF 4.2 line parsing (sites only; sample/genotype columns are ignored).

A hand-written parser instead of pysam/htslib: it keeps the service a pure-Python wheel
install, and parsing is a small part of the work. The cost is that it supports only what the
pipeline needs: eight fixed columns and an optional per-allele AF in INFO.
"""

import math
from dataclasses import dataclass

from variant_etl.pipeline.records import RecordRejected, RejectReason


@dataclass(slots=True)
class VcfRecord:
    chrom: str
    pos: int
    ref: str  # upper-cased
    alts: list[str]  # upper-cased
    qual: float | None
    filter: str | None
    af: list[float] | None  # one value per ALT when INFO/AF is present and well formed


def is_header_line(line: str) -> bool:
    return line.startswith("#")


def _parse_af(info: str, alt_count: int) -> list[float] | None:
    if "AF=" not in info:
        return None
    for field in info.split(";"):
        if field.startswith("AF="):
            try:
                values = [float(v) for v in field[3:].split(",")]
            except ValueError:
                return None
            # Number=A: one value per ALT. Anything else cannot be attributed to alleles.
            if len(values) != alt_count or not all(math.isfinite(v) for v in values):
                return None
            return values
    return None


def parse_record(line: str) -> VcfRecord:
    """Parse one data line. Raises RecordRejected(MALFORMED_LINE | NON_NUMERIC_QUAL)."""
    fields = line.split("\t", 8)
    if len(fields) < 8:
        raise RecordRejected(RejectReason.MALFORMED_LINE)
    chrom, pos_text, _id, ref, alt_text, qual_text, filter_text, info = fields[:8]

    # str.isdigit() accepts non-ASCII digits; int() also accepts signs and underscores.
    if not chrom or not (pos_text.isascii() and pos_text.isdigit()) or not ref or not alt_text:
        raise RecordRejected(RejectReason.MALFORMED_LINE)
    alts = alt_text.split(",")
    if "" in alts:
        raise RecordRejected(RejectReason.MALFORMED_LINE)

    qual: float | None = None
    if qual_text != ".":
        try:
            qual = float(qual_text)
        except ValueError:
            raise RecordRejected(RejectReason.NON_NUMERIC_QUAL) from None
        if not math.isfinite(qual):  # float() happily parses "nan" and "inf"
            raise RecordRejected(RejectReason.NON_NUMERIC_QUAL)

    return VcfRecord(
        chrom=chrom,
        pos=int(pos_text),
        ref=ref.upper(),
        alts=[a.upper() for a in alts],
        qual=qual,
        filter=None if filter_text == "." else filter_text,
        af=_parse_af(info, len(alts)),
    )
