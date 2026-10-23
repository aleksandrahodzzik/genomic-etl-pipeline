import pytest

from variant_etl.pipeline.chunking import (
    FIRST_LINE_KEY,
    VcfFormatError,
    read_chunk,
    split_vcf_lines,
)
from variant_etl.pipeline.textio import iter_lines
from variant_etl.pipeline.transform import process_lines

HEADER = [
    "##fileformat=VCFv4.2",
    "##contig=<ID=chr1,length=18>",
    "#CHROM\tPOS\tID\tREF\tALT\t.\t.\t.",
]


def records(n: int) -> list[str]:
    return [f"chr1\t{i + 1}\t.\tG\tA\t10\tPASS\t." for i in range(n)]


def chunks(lines: list[str], size: int):
    return list(split_vcf_lines(lines, size))


def body_of(chunk) -> list[str]:
    _first, rest = read_chunk(iter_lines([chunk.payload]))
    return list(rest)


def test_every_chunk_but_the_last_has_exactly_chunk_size_records():
    result = chunks([*HEADER, *records(25)], 10)
    assert [c.record_count for c in result] == [10, 10, 5]
    assert [c.index for c in result] == [0, 1, 2]


def test_exact_multiple_does_not_produce_an_empty_trailing_chunk():
    assert [c.record_count for c in chunks([*HEADER, *records(20)], 10)] == [10, 10]


def test_concatenating_chunk_bodies_gives_back_the_original_records():
    original = records(23)
    rebuilt = [line for c in chunks([*HEADER, *original], 7) for line in body_of(c)]
    assert rebuilt == original


def test_each_chunk_is_a_standalone_vcf_with_header_and_first_line_marker():
    (first, *_rest) = chunks([*HEADER, *records(5)], 2)
    text = first.payload.decode().splitlines()
    assert text[0] == "##fileformat=VCFv4.2"
    marker = next(i for i, line in enumerate(text) if line.startswith(FIRST_LINE_KEY))
    assert text[marker + 1].startswith("#CHROM")  # a meta line, so it sits before #CHROM
    assert len([line for line in text if not line.startswith("#")]) == 2


def test_first_line_numbers_refer_to_the_original_file():
    result = chunks([*HEADER, *records(6)], 2)
    # 3 header lines, so records start at line 4.
    assert [c.first_line for c in result] == [4, 6, 8]
    assert [read_chunk(iter_lines([c.payload]))[0] for c in result] == [4, 6, 8]


def test_blank_lines_are_kept_but_not_counted_so_line_numbers_stay_exact(repeat_reference):
    original = [*HEADER, *records(2), "", *records(2)]
    result = chunks(original, 2)
    assert [c.record_count for c in result] == [2, 2]
    # Chunk 1 starts at the blank line (file line 6), and the lines inside it are numbered
    # from there, so a bad record reports its true position in the uploaded file.
    assert result[1].first_line == 6
    first_line, body = read_chunk(iter_lines([result[1].payload]))
    outcome = process_lines(
        [*[*body][:1], "chr1\tbroken"], first_line, repeat_reference, max_rejected_samples=5
    )
    assert [(r.line_no, r.reason) for r in outcome.rejected_samples] == [(7, "malformed_line")]


def test_header_only_vcf_gives_no_chunks():
    assert chunks(HEADER, 10) == []


@pytest.mark.parametrize("lines", [[], ["chr1\t1\t.\tA\tC\t.\t.\t."], ["##fileformat=VCFv4.2"]])
def test_input_without_a_chrom_header_is_rejected_as_not_a_vcf(lines):
    with pytest.raises(VcfFormatError):
        chunks(lines, 10)


def test_chunk_without_marker_is_a_format_error():
    with pytest.raises(VcfFormatError):
        read_chunk(iter(["##fileformat=VCFv4.2", "#CHROM\tPOS"]))
