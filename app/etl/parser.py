"""Constant-memory streaming parsers for VCF and FASTA text."""

import hashlib
from dataclasses import dataclass
from typing import Iterable, Iterator, List, Optional

DEFAULT_BATCH_SIZE = 100_000


@dataclass(slots=True)
class VcfRecord:
    """One *decomposed* VCF record (exactly one ALT allele), all fields still raw text."""

    line_number: int
    chrom: str
    pos: str
    variant_id: str
    ref: str
    alt: str
    qual: str
    filter: str
    info: str
    alt_index: int = 0  # index of this ALT within the original (multi-allelic) line
    parse_error: Optional[str] = None  # set for structurally malformed lines


@dataclass(slots=True)
class FastaContigRecord:
    """Aggregate statistics for one FASTA record, computed without keeping the sequence."""

    contig_id: str
    description: Optional[str]
    sequence_length: int
    gc_count: int
    n_count: int
    md5_checksum: str

    @property
    def gc_content_percent(self) -> float:
        """GC percentage over called (non-N) bases."""
        called = self.sequence_length - self.n_count
        if called <= 0:
            return 0.0
        return round(self.gc_count / called * 100.0, 4)


def _decompose_vcf_line(line: str, line_number: int) -> List[VcfRecord]:
    """Split one data line into one record per ALT allele (multi-allelic decomposition)."""
    fields = line.rstrip("\r\n").split("\t")
    if len(fields) < 8:
        # Keep malformed rows in the stream so they are counted and rejected downstream.
        padded = fields + [""] * (8 - len(fields))
        return [
            VcfRecord(
                line_number=line_number,
                chrom=padded[0],
                pos=padded[1],
                variant_id=padded[2],
                ref=padded[3],
                alt=padded[4],
                qual=padded[5],
                filter=padded[6],
                info=padded[7],
                parse_error=f"expected >= 8 tab-separated columns, found {len(fields)}",
            )
        ]

    chrom, pos, variant_id, ref, alt, qual, flt, info = fields[:8]
    alts = alt.split(",") if "," in alt else [alt]
    return [
        VcfRecord(
            line_number=line_number,
            chrom=chrom,
            pos=pos,
            variant_id=variant_id,
            ref=ref,
            alt=single_alt,
            qual=qual,
            filter=flt,
            info=info,
            alt_index=index,
        )
        for index, single_alt in enumerate(alts)
    ]


def parse_vcf_stream(
    lines: Iterable[str], batch_size: int = DEFAULT_BATCH_SIZE
) -> Iterator[List[VcfRecord]]:
    """Stream VCF text lines into batches of decomposed records.

    * `##` meta lines, the `#CHROM` header and blank lines are skipped.
    * Multi-allelic sites (`ALT=A,G`) are decomposed into one record per ALT.
    * Each yielded list holds at most `batch_size` records.
    """
    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")

    batch: List[VcfRecord] = []
    for line_number, line in enumerate(lines, start=1):
        if not line or line.startswith("#") or not line.strip():
            continue
        batch.extend(_decompose_vcf_line(line, line_number))
        while len(batch) >= batch_size:
            yield batch[:batch_size]
            batch = batch[batch_size:]
    if batch:
        yield batch


def parse_fasta_stream(lines: Iterable[str]) -> Iterator[FastaContigRecord]:
    """Stream FASTA text lines, yielding one statistics record per contig.

    Sequence data is folded into running counters and an incremental MD5, so memory
    use is independent of contig size (works for gigabase-scale chromosomes).
    """
    contig_id: Optional[str] = None
    description: Optional[str] = None
    length = 0
    gc_count = 0
    n_count = 0
    digest = hashlib.md5()  # noqa: S324 - content fingerprint, not security

    def finish() -> FastaContigRecord:
        return FastaContigRecord(
            contig_id=contig_id or "",
            description=description,
            sequence_length=length,
            gc_count=gc_count,
            n_count=n_count,
            md5_checksum=digest.hexdigest(),
        )

    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith(";"):
            continue
        if line.startswith(">"):
            if contig_id is not None:
                yield finish()
            header = line[1:].strip()
            parts = header.split(None, 1)
            contig_id = parts[0] if parts else ""
            description = parts[1] if len(parts) > 1 else None
            length = gc_count = n_count = 0
            digest = hashlib.md5()  # noqa: S324
            continue
        if contig_id is None:
            continue  # sequence data before the first header is ignored
        sequence = line.upper()
        length += len(sequence)
        gc_count += sequence.count("G") + sequence.count("C")
        n_count += sequence.count("N")
        digest.update(sequence.encode("ascii", errors="replace"))

    if contig_id is not None:
        yield finish()
