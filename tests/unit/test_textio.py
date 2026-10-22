import gzip

from variant_etl.pipeline.textio import iter_lines, maybe_gunzip


def blocks(data: bytes, size: int) -> list[bytes]:
    return [data[i : i + size] for i in range(0, len(data), size)]


def test_plain_text_passes_through_unchanged():
    data = b"a\nb\nc\n"
    assert b"".join(maybe_gunzip(blocks(data, 2))) == data


def test_gzip_is_detected_by_magic_not_by_name_and_decoded_across_tiny_blocks():
    data = b"line1\nline2\n" * 500
    assert b"".join(maybe_gunzip(blocks(gzip.compress(data), 7))) == data


def test_concatenated_gzip_members_are_all_decoded():
    # bgzip/BGZF files are many gzip members back to back.
    first, second = b"first member\n" * 50, b"second member\n" * 50
    stream = gzip.compress(first) + gzip.compress(second)
    assert b"".join(maybe_gunzip(blocks(stream, 5))) == first + second


def test_single_byte_blocks_still_detect_gzip():
    data = b"x\n" * 10
    assert b"".join(maybe_gunzip(blocks(gzip.compress(data), 1))) == data


def test_empty_stream_yields_nothing():
    assert list(maybe_gunzip([])) == []
    assert list(iter_lines([])) == []


def test_iter_lines_handles_lines_split_across_blocks_and_crlf():
    data = [b"ab", b"c\r\nde", b"f\n", b"g"]
    assert list(iter_lines(data)) == ["abc", "def", "g"]


def test_iter_lines_keeps_blank_lines_so_line_numbers_stay_aligned():
    assert list(iter_lines([b"a\n\nb\n"])) == ["a", "", "b"]


def test_iter_lines_replaces_invalid_utf8_instead_of_crashing():
    (line,) = iter_lines([b"ab\xffcd\n"])
    assert line.startswith("ab") and line.endswith("cd")
