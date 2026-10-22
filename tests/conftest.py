from pathlib import Path

import pytest

from variant_etl.pipeline.reference import Reference, build_alias_map

DATA_DIR = Path(__file__).parent / "data"


class InMemoryReference:
    """Tiny reference for hand-written unit-test cases (same interface as FastaReference)."""

    def __init__(self, contigs: dict[str, str]) -> None:
        self._contigs = {name: seq.upper() for name, seq in contigs.items()}
        self._aliases = build_alias_map(list(self._contigs))

    def resolve(self, name: str) -> str | None:
        return self._aliases.get(name)

    def contig_length(self, name: str) -> int:
        return len(self._contigs[name])

    def fetch(self, chrom: str, start: int, end: int) -> str:
        return self._contigs[chrom][max(start, 0) : end]


@pytest.fixture
def data_dir() -> Path:
    return DATA_DIR


@pytest.fixture
def repeat_reference() -> Reference:
    """chr1 positions (1-based):
    G1 G2 T3 A4 C5 A6 C7 A8 C9 T10 T11 C12 A13 A14 A15 A16 G17 T18
    i.e. a dinucleotide repeat (AC)x3 at 4-9 and a homopolymer A-run at 13-16.
    chr2 is a short contig for bounds and start-of-contig cases.
    """
    return InMemoryReference({"chr1": "GGTACACACTTCAAAAGT", "chr2": "AAATGC"})
