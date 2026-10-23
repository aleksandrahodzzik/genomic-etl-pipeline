import hashlib
import random
import subprocess
import sys
from pathlib import Path

import pytest

from variant_etl.pipeline.reference import FastaReference

ROOT = Path(__file__).resolve().parents[2]
SMALL = ["--contigs", "2", "--contig-length", "4000", "--variants", "200", "--error-rate", "0.05"]


def run_generator(out_dir: Path, seed: int) -> dict[str, str]:
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "generate_data.py"),
            *SMALL,
            "--seed",
            str(seed),
            "--out-dir",
            str(out_dir),
        ],
        check=True,
        capture_output=True,
    )
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(out_dir.iterdir())}


def test_same_seed_gives_byte_identical_output(tmp_path):
    assert run_generator(tmp_path / "a", 5) == run_generator(tmp_path / "b", 5)


def test_different_seed_gives_different_data(tmp_path):
    first, second = run_generator(tmp_path / "a", 1), run_generator(tmp_path / "b", 2)
    assert first["variants.vcf"] != second["variants.vcf"]
    assert first["ref.fa"] != second["ref.fa"]


def read_fasta(path: Path) -> dict[str, str]:
    contigs: dict[str, list[str]] = {}
    name = ""
    for line in path.read_text().splitlines():
        if line.startswith(">"):
            name = line[1:].split()[0]
            contigs[name] = []
        else:
            contigs[name].append(line)
    return {k: "".join(v) for k, v in contigs.items()}


@pytest.fixture
def reference(data_dir):
    ref = FastaReference(data_dir / "ref.fa")
    yield ref
    ref.close()


def test_fai_written_by_the_generator_is_usable_and_lengths_match(data_dir, reference):
    expected = read_fasta(data_dir / "ref.fa")
    assert reference.contigs == {name: len(seq) for name, seq in expected.items()}


def test_fetch_matches_plain_slicing_including_across_cache_blocks(
    data_dir, reference, monkeypatch
):
    monkeypatch.setattr(FastaReference, "BLOCK", 128)  # force many block boundaries
    expected = read_fasta(data_dir / "ref.fa")
    rng = random.Random(0)
    for _ in range(300):
        chrom = rng.choice(list(expected))
        start = rng.randrange(0, len(expected[chrom]))
        end = start + rng.randint(1, 700)
        assert reference.fetch(chrom, start, end) == expected[chrom][start:end].upper()


def test_fetch_is_clipped_at_contig_bounds(reference):
    length = reference.contig_length("chr1")
    assert reference.fetch("chr1", length - 3, length + 50) == reference.fetch(
        "chr1", length - 3, length
    )
    assert len(reference.fetch("chr1", length - 3, length + 50)) == 3
    assert reference.fetch("chr1", length, length + 5) == ""
    assert reference.fetch("chr1", -5, 2) == reference.fetch("chr1", 0, 2)


def test_contig_aliases(reference):
    assert reference.resolve("chr1") == "chr1"
    assert reference.resolve("1") == "chr1"
    assert reference.resolve("chrZ") is None
