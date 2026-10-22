"""Streaming parser tests (VCF batching, multi-allelic decomposition, FASTA statistics)."""

import hashlib

import pytest

from app.etl.parser import parse_fasta_stream, parse_vcf_stream


def snp_line(pos: int) -> str:
    return f"chr1\t{pos}\t.\tA\tG\t50\tPASS\tDP=10;AF=0.1"


def test_vcf_skips_headers_and_blank_lines(sample_vcf_text):
    batches = list(parse_vcf_stream(sample_vcf_text.splitlines()))
    records = [r for batch in batches for r in batch]
    assert len(batches) == 1
    assert all(not r.chrom.startswith("#") for r in records)
    assert records[0].chrom == "1"
    assert records[0].pos == "100"


def test_vcf_batches_respect_batch_size():
    lines = ["##meta", "#CHROM\tPOS"] + [snp_line(i) for i in range(1, 251)]
    sizes = [len(batch) for batch in parse_vcf_stream(iter(lines), batch_size=100)]
    assert sizes == [100, 100, 50]


def test_vcf_exact_multiple_has_no_empty_trailing_batch():
    lines = [snp_line(i) for i in range(1, 201)]
    sizes = [len(batch) for batch in parse_vcf_stream(lines, batch_size=100)]
    assert sizes == [100, 100]


def test_vcf_is_lazy_over_input_iterator():
    consumed = []

    def source():
        for i in range(1, 1001):
            consumed.append(i)
            yield snp_line(i)

    stream = parse_vcf_stream(source(), batch_size=10)
    first = next(stream)
    assert len(first) == 10
    assert len(consumed) == 10  # only one batch worth of lines was pulled


def test_vcf_multiallelic_decomposition():
    line = "chr3\t500\trs5\tA\tG,T,C\t99\tPASS\tDP=77;AF=0.1,0.02,0.3"
    (batch,) = list(parse_vcf_stream([line]))
    assert [r.alt for r in batch] == ["G", "T", "C"]
    assert [r.alt_index for r in batch] == [0, 1, 2]
    assert {r.pos for r in batch} == {"500"}
    assert {r.info for r in batch} == {"DP=77;AF=0.1,0.02,0.3"}


def test_vcf_malformed_line_is_kept_with_parse_error():
    (batch,) = list(parse_vcf_stream(["chr1\t100\t.\tA"]))
    assert len(batch) == 1
    assert batch[0].parse_error is not None
    assert batch[0].chrom == "chr1"


def test_vcf_handles_crlf_and_extra_columns():
    line = "chr1\t100\t.\tA\tG\t50\tPASS\tDP=10\tGT\t0/1\r\n"
    (batch,) = list(parse_vcf_stream([line]))
    assert batch[0].info == "DP=10"
    assert batch[0].parse_error is None


def test_vcf_rejects_invalid_batch_size():
    with pytest.raises(ValueError):
        list(parse_vcf_stream([], batch_size=0))


def test_vcf_empty_input_yields_nothing():
    assert list(parse_vcf_stream([])) == []


def test_fasta_gc_content_and_md5(sample_fasta_text):
    contigs = list(parse_fasta_stream(sample_fasta_text.splitlines()))
    assert [c.contig_id for c in contigs] == ["seq1", "seq2"]

    first, second = contigs
    assert first.description == "first test contig"
    assert first.sequence_length == 14
    assert first.n_count == 2
    assert first.gc_count == 6
    assert first.gc_content_percent == pytest.approx(50.0)  # 6 GC / 12 called bases
    assert first.md5_checksum == hashlib.md5(b"ACGTNNACGTACGT").hexdigest()

    assert second.description is None
    assert second.sequence_length == 4
    assert second.gc_content_percent == pytest.approx(100.0)
    assert second.md5_checksum == hashlib.md5(b"GGCC").hexdigest()


def test_fasta_md5_independent_of_line_wrapping_and_case():
    wrapped = list(parse_fasta_stream([">c", "acgt", "ACGT", "aC"]))
    single = list(parse_fasta_stream([">c", "ACGTACGTAC"]))
    assert wrapped[0].md5_checksum == single[0].md5_checksum
    assert wrapped[0].sequence_length == 10


def test_fasta_all_n_contig_has_zero_gc():
    (contig,) = list(parse_fasta_stream([">gap", "NNNN"]))
    assert contig.n_count == 4
    assert contig.gc_content_percent == 0.0


def test_fasta_ignores_sequence_before_first_header_and_empty_input():
    assert list(parse_fasta_stream([])) == []
    contigs = list(parse_fasta_stream(["ACGT", ">a", "GG"]))
    assert [(c.contig_id, c.sequence_length) for c in contigs] == [("a", 2)]


def test_fasta_streams_large_contig_with_constant_state():
    line = "ACGT" * 15  # 60 bp
    lines = [">big"] + [line] * 5000

    (contig,) = list(parse_fasta_stream(iter(lines)))
    assert contig.sequence_length == 300_000
    assert contig.gc_content_percent == pytest.approx(50.0)
    assert contig.md5_checksum == hashlib.md5((line * 5000).encode()).hexdigest()
