"""The parts of scripts/benchmark_clinvar.py that are easy to get subtly wrong: the chromosome
subset, the two-member gzip it writes, and splicing a run marker into that gzip so the
pipeline still reads the file as the same VCF. No network and no stack needed."""

import gzip
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import benchmark_clinvar  # noqa: E402
from variant_etl.pipeline.chunking import split_vcf_lines  # noqa: E402
from variant_etl.pipeline.textio import iter_lines, maybe_gunzip  # noqa: E402

HEADER = [
    "##fileformat=VCFv4.1",
    "##source=ClinVar",
    "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO",
]
RECORDS = [
    "1\t942451\t836156\tT\t.\t.\t.\tCLNHGVS=NC_000001.11:g.942451%3D;CLNVC=Variation",
    "1\t943000\t1\tA\tG\t.\t.\tCLNVC=single_nucleotide_variant",
    "2\t100\t2\tC\tT\t.\t.\tCLNVC=single_nucleotide_variant",
    "5\t200\t3\tG\tA\t.\t.\tCLNVC=single_nucleotide_variant",
    "17\t43044300\t4\tAT\tA\t.\t.\tCLNVC=Deletion",
    "NT_187633.1\t5\t5\tG\tC\t.\t.\tCLNVC=single_nucleotide_variant",
]


def write_release(path: Path) -> None:
    path.write_bytes(gzip.compress(("\n".join(HEADER + RECORDS) + "\n").encode()))


def pipeline_lines(data: bytes) -> list[str]:
    """What the pipeline's split step reads from an uploaded file."""
    return list(iter_lines(maybe_gunzip([data[i : i + 7] for i in range(0, len(data), 7)])))


def test_subset_keeps_the_header_and_the_chosen_chromosomes(tmp_path):
    release = tmp_path / "clinvar.vcf.gz"
    write_release(release)
    stats = benchmark_clinvar.subset_vcf(release, {"1", "17"}, tmp_path / "subset.vcf.gz")

    assert stats["records_in_release"] == 6
    assert stats["records_kept"] == 3
    assert stats["records_by_chromosome"]["1"] == 2
    kept = gzip.decompress((tmp_path / "subset.vcf.gz").read_bytes()).decode().splitlines()
    assert kept == [*HEADER, RECORDS[0], RECORDS[1], RECORDS[4]]


def test_subset_is_two_gzip_members_with_the_fileformat_line_first(tmp_path):
    release = tmp_path / "clinvar.vcf.gz"
    write_release(release)
    benchmark_clinvar.subset_vcf(release, {"1"}, tmp_path / "subset.vcf.gz")

    data = (tmp_path / "subset.vcf.gz").read_bytes()
    first = zlib.decompressobj(wbits=31)
    assert first.decompress(data) == b"##fileformat=VCFv4.1\n"
    assert first.eof and first.unused_data


def test_run_marker_goes_after_fileformat_and_the_records_are_unchanged(tmp_path):
    release = tmp_path / "clinvar.vcf.gz"
    write_release(release)
    benchmark_clinvar.subset_vcf(release, {"1", "2", "17"}, tmp_path / "subset.vcf.gz")
    dataset = object.__new__(benchmark_clinvar.Dataset)  # no stack: only the bytes matter
    dataset.vcf = (tmp_path / "subset.vcf.gz").read_bytes()

    one, two = dataset.marked_vcf(), dataset.marked_vcf()
    assert one != two  # a new checksum every run, so the API cannot dedupe the job
    lines = pipeline_lines(one)
    assert lines[0] == "##fileformat=VCFv4.1"
    assert lines[1].startswith("##benchmark_run=")
    assert lines[2:] == pipeline_lines(dataset.vcf)[1:]
    chunks = list(split_vcf_lines(lines, chunk_size=100))
    assert sum(chunk.record_count for chunk in chunks) == 4


def test_plain_text_vcf_gets_the_marker_after_the_first_line():
    dataset = object.__new__(benchmark_clinvar.Dataset)
    dataset.vcf = ("\n".join(HEADER + RECORDS[:1]) + "\n").encode()
    lines = dataset.marked_vcf().decode().splitlines()
    assert lines[0] == HEADER[0]
    assert lines[1].startswith("##benchmark_run=")
    assert lines[2:] == HEADER[1:] + RECORDS[:1]


def test_reject_patterns_describe_clinvar_reference_allele_records():
    rejected = [{"reason": "unsupported_symbolic", "raw_line": RECORDS[0], "line_no": 4}]
    patterns = benchmark_clinvar.reject_patterns(rejected)["unsupported_symbolic"]
    assert patterns["patterns"]["ALT ."] == 1
    assert patterns["patterns"]["HGVS g.N= (no change)"] == 1
    assert patterns["hgvs_examples"] == ["NC_000001.11:g.942451="]
