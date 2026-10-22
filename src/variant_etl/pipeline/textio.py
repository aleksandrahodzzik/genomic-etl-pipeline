"""Streaming helpers: turn an iterator of byte blocks (an S3 body, a file) into text lines.

Everything here is incremental so memory stays flat regardless of file size.
"""

import zlib
from collections.abc import Iterable, Iterator

GZIP_MAGIC = b"\x1f\x8b"


def maybe_gunzip(blocks: Iterable[bytes]) -> Iterator[bytes]:
    """Yield decompressed blocks if the stream is gzip, otherwise pass it through.

    Detection uses the magic number, not the file name, because presigned uploads keep
    whatever name the client chose. Concatenated members (BGZF, which is what bgzip and
    bcftools write) are handled by restarting the decompressor on `unused_data`; plain
    `zlib.decompressobj(31)` would silently stop after the first member.
    """
    iterator = iter(blocks)
    head = b""
    for block in iterator:
        head += block
        if len(head) >= 2:
            break

    def rejoined() -> Iterator[bytes]:
        if head:
            yield head
        yield from iterator

    if head[:2] != GZIP_MAGIC:
        yield from rejoined()
        return

    decompressor = zlib.decompressobj(wbits=31)
    for block in rejoined():
        while block:
            out = decompressor.decompress(block)
            if out:
                yield out
            if decompressor.eof:
                block = decompressor.unused_data
                decompressor = zlib.decompressobj(wbits=31)
            else:
                block = b""


def iter_lines(blocks: Iterable[bytes]) -> Iterator[str]:
    """Split byte blocks into text lines without the line terminator (LF or CRLF)."""
    leftover = b""
    for block in blocks:
        parts = (leftover + block).split(b"\n")
        leftover = parts.pop()
        for part in parts:
            yield part.rstrip(b"\r").decode("utf-8", errors="replace")
    if leftover:
        yield leftover.rstrip(b"\r").decode("utf-8", errors="replace")
