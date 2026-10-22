"""Reference genome access (FASTA via pyfaidx) behind a small interface.

The pipeline only needs three things from a reference: resolve a contig name, know its
length, and fetch a slice. Keeping that as a Protocol lets unit tests use an in-memory
genome and keeps pyfaidx details out of validation and normalization.
"""

from collections import OrderedDict
from pathlib import Path
from typing import Protocol

from pyfaidx import Fasta


class Reference(Protocol):
    def resolve(self, name: str) -> str | None:
        """Canonical contig name for a name used in a VCF, or None if unknown."""

    def contig_length(self, name: str) -> int: ...

    def fetch(self, chrom: str, start: int, end: int) -> str:
        """Upper-case bases for the 0-based half-open interval [start, end); clipped to the
        contig (so it may return fewer bases than asked at the contig end)."""


def contig_aliases(name: str) -> list[str]:
    """Other spellings commonly used for the same contig (chr1/1, chrM/MT/M)."""
    bare = name.removeprefix("chr")
    aliases = [bare, f"chr{bare}"]
    if bare in {"M", "MT"}:
        aliases += ["M", "MT", "chrM", "chrMT"]
    return aliases


def build_alias_map(names: list[str]) -> dict[str, str]:
    mapping = {name: name for name in names}
    for name in names:
        for alias in contig_aliases(name):
            mapping.setdefault(alias, name)
    return mapping


class FastaReference:
    """pyfaidx-backed reference with a small block cache.

    Normalization asks for one base at a time while it walks left through repeats, and
    records inside a chunk are close together, so reading 64 KiB blocks and slicing them
    in Python is much cheaper than a seek+read per request.
    """

    BLOCK = 1 << 16
    MAX_BLOCKS = 256

    def __init__(self, path: str | Path) -> None:
        # pyfaidx writes <path>.fai next to the FASTA when it is missing.
        self._fasta = Fasta(str(path), as_raw=True, sequence_always_upper=True)
        self._lengths: dict[str, int] = {
            name: len(record) for name, record in self._fasta.records.items()
        }
        self._aliases = build_alias_map(list(self._lengths))
        self._blocks: OrderedDict[tuple[str, int], str] = OrderedDict()

    @property
    def contigs(self) -> dict[str, int]:
        return dict(self._lengths)

    def resolve(self, name: str) -> str | None:
        return self._aliases.get(name)

    def contig_length(self, name: str) -> int:
        return self._lengths[name]

    def _block(self, chrom: str, index: int) -> str:
        key = (chrom, index)
        block = self._blocks.get(key)
        if block is None:
            start = index * self.BLOCK
            block = str(self._fasta[chrom][start : start + self.BLOCK])
            self._blocks[key] = block
            if len(self._blocks) > self.MAX_BLOCKS:
                self._blocks.popitem(last=False)
        else:
            self._blocks.move_to_end(key)
        return block

    def fetch(self, chrom: str, start: int, end: int) -> str:
        start = max(start, 0)
        end = min(end, self._lengths[chrom])
        if start >= end:
            return ""
        first, last = start // self.BLOCK, (end - 1) // self.BLOCK
        if first == last:
            offset = first * self.BLOCK
            return self._block(chrom, first)[start - offset : end - offset]
        joined = "".join(self._block(chrom, i) for i in range(first, last + 1))
        offset = first * self.BLOCK
        return joined[start - offset : end - offset]

    def close(self) -> None:
        self._fasta.close()
