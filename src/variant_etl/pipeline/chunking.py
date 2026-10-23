"""Cut a VCF into self-contained chunks, and read them back.

Each chunk is a valid standalone VCF (the original header + records). Two extra things are
recorded so chunks can be processed in any order on any worker:
  * `##variant_etl_first_line=N` (a legal meta line) says which line of the original file
    the chunk's first record line was, so rejected records keep their original line number;
  * chunk boundaries fall after exactly `chunk_size` non-blank records, so every chunk but
    the last has the same size and results do not depend on chunk placement.
"""

from collections.abc import Iterable, Iterator
from dataclasses import dataclass

FIRST_LINE_KEY = "##variant_etl_first_line="


class VcfFormatError(Exception):
    """The input is not a usable VCF. Permanent: retrying cannot fix it."""


@dataclass(frozen=True)
class Chunk:
    index: int
    first_line: int  # line number in the original file of the chunk's first body line
    record_count: int
    payload: bytes


def _check_header(header: list[str]) -> None:
    if not header or not header[-1].startswith("#CHROM"):
        raise VcfFormatError("missing '#CHROM' header line: not a VCF file")


def split_vcf_lines(lines: Iterable[str], chunk_size: int) -> Iterator[Chunk]:
    header: list[str] = []
    in_header = True
    body: list[str] = []
    count = 0
    first_line = 0
    index = 0

    def make() -> Chunk:
        meta = [*header[:-1], f"{FIRST_LINE_KEY}{first_line}", header[-1]]
        return Chunk(index, first_line, count, ("\n".join([*meta, *body]) + "\n").encode())

    for line_no, line in enumerate(lines, start=1):
        if in_header:
            if line.startswith("#"):
                header.append(line)
                continue
            _check_header(header)
            in_header = False
        if not body:
            first_line = line_no
        body.append(line)
        if line.strip():
            count += 1
        if count == chunk_size:
            yield make()
            index += 1
            body, count = [], 0

    if in_header:
        _check_header(header)  # empty file, or a header with no records
    if count > 0:
        yield make()


def read_chunk(lines: Iterable[str]) -> tuple[int, Iterator[str]]:
    """Return (first_line_number, remaining body lines) for a chunk made by split_vcf_lines."""
    iterator = iter(lines)
    first_line: int | None = None
    for line in iterator:
        if line.startswith(FIRST_LINE_KEY):
            first_line = int(line.removeprefix(FIRST_LINE_KEY))
        elif line.startswith("#CHROM"):
            break
    else:
        raise VcfFormatError("chunk has no '#CHROM' header line")
    if first_line is None:
        raise VcfFormatError("chunk is missing its first-line marker")
    return first_line, iterator
