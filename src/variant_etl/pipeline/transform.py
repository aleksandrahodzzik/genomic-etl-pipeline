"""Raw VCF text -> normalized variant rows, for one line and for one chunk of lines."""

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field

from variant_etl.pipeline.normalize import classify, is_transition, normalize_allele
from variant_etl.pipeline.records import (
    RecordRejected,
    RejectedLine,
    VariantRow,
)
from variant_etl.pipeline.reference import Reference
from variant_etl.pipeline.validate import validate_record
from variant_etl.pipeline.vcf import parse_record


def process_line(line: str, reference: Reference) -> list[VariantRow]:
    """Parse, validate, split and normalize one data line. Raises RecordRejected.

    `was_normalized` is true when the stored row is not the allele as written: it was
    trimmed or left-aligned, or it came out of splitting a multi-allelic site. That is the
    set of rows `bcftools norm -m -any -f` would have touched.
    """
    record = parse_record(line)
    chrom = validate_record(record, reference)
    multi_allelic = len(record.alts) > 1

    rows: list[VariantRow] = []
    for index, alt in enumerate(record.alts):
        pos, ref, new_alt = normalize_allele(reference, chrom, record.pos, record.ref, alt)
        changed = multi_allelic or (pos, ref, new_alt) != (record.pos, record.ref, alt)
        rows.append(
            VariantRow(
                chrom=chrom,
                pos=pos,
                ref=ref,
                alt=new_alt,
                qual=record.qual,
                filter=record.filter,
                variant_type=classify(ref, new_alt).value,
                is_transition=is_transition(ref, new_alt),
                af=record.af[index] if record.af else None,
                was_normalized=changed,
            )
        )
    return rows


@dataclass
class ChunkResult:
    """Outcome of processing one chunk. `total` and `accepted`/`rejected` count VCF records
    (lines); `rows` can be longer than `accepted` because multi-allelic sites are split."""

    rows: list[VariantRow] = field(default_factory=list)
    rejected_samples: list[RejectedLine] = field(default_factory=list)
    reject_reasons: Counter[str] = field(default_factory=Counter)
    total: int = 0
    accepted: int = 0
    rejected: int = 0
    normalized: int = 0  # rows with was_normalized


def process_lines(
    lines: Iterable[str],
    first_line_no: int,
    reference: Reference,
    max_rejected_samples: int,
) -> ChunkResult:
    """Process the data lines of a chunk. Blank lines are skipped but still counted for
    line numbers, so `line_no` in rejected_records matches the original file."""
    result = ChunkResult()
    for line_no, line in enumerate(lines, start=first_line_no):
        if not line.strip():
            continue
        result.total += 1
        try:
            rows = process_line(line, reference)
        except RecordRejected as exc:
            result.rejected += 1
            result.reject_reasons[exc.reason.value] += 1
            if len(result.rejected_samples) < max_rejected_samples:
                result.rejected_samples.append(RejectedLine(line_no, exc.reason.value, line))
            continue
        result.accepted += 1
        result.normalized += sum(row.was_normalized for row in rows)
        result.rows.extend(rows)
    return result
