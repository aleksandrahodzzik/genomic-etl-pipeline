"""One test per reject reason, plus the precedence between them.

Reference used (chr1, 1-based): G1 G2 T3 A4 C5 A6 C7 A8 C9 T10 T11 C12 A13 A14 A15 A16 G17 T18
"""

import pytest

from variant_etl.pipeline.records import RecordRejected, RejectReason
from variant_etl.pipeline.transform import process_line, process_lines


def line(chrom="chr1", pos="3", ref="T", alt="G", qual="30"):
    return "\t".join([chrom, pos, ".", ref, alt, qual, "PASS", "."])


def reason_of(text, reference):
    with pytest.raises(RecordRejected) as exc:
        process_line(text, reference)
    return exc.value.reason


def test_valid_record_is_accepted(repeat_reference):
    (row,) = process_line(line(), repeat_reference)
    assert (row.chrom, row.pos, row.ref, row.alt) == ("chr1", 3, "T", "G")


def test_unknown_contig(repeat_reference):
    assert reason_of(line(chrom="chrZ"), repeat_reference) is RejectReason.UNKNOWN_CONTIG


def test_contig_alias_is_resolved_to_the_reference_name(repeat_reference):
    (row,) = process_line(line(chrom="1"), repeat_reference)
    assert row.chrom == "chr1"


@pytest.mark.parametrize("pos", ["0", "19", "1000"])
def test_pos_outside_contig(pos, repeat_reference):
    assert reason_of(line(pos=pos), repeat_reference) is RejectReason.POS_OUT_OF_BOUNDS


def test_ref_running_past_contig_end_is_out_of_bounds(repeat_reference):
    # chr1 has 18 bases; a 2-base REF at 18 would need base 19.
    assert reason_of(line(pos="18", ref="TT"), repeat_reference) is RejectReason.POS_OUT_OF_BOUNDS


def test_last_base_of_contig_is_in_bounds(repeat_reference):
    (row,) = process_line(line(pos="18", ref="T", alt="C"), repeat_reference)
    assert row.pos == 18


@pytest.mark.parametrize("alt", ["<DEL>", "<DUP:TANDEM>", "*", ".", "A[chr2:5[", "]chr2:5]A"])
def test_unsupported_symbolic_alt(alt, repeat_reference):
    assert reason_of(line(alt=alt), repeat_reference) is RejectReason.UNSUPPORTED_SYMBOLIC


def test_one_symbolic_alt_rejects_the_whole_multiallelic_line(repeat_reference):
    assert reason_of(line(alt="G,<DEL>"), repeat_reference) is RejectReason.UNSUPPORTED_SYMBOLIC


@pytest.mark.parametrize("ref,alt", [("T", "X"), ("T", "G1"), ("U", "G"), ("T", "G-")])
def test_alleles_outside_acgtn(ref, alt, repeat_reference):
    assert reason_of(line(ref=ref, alt=alt), repeat_reference) is RejectReason.INVALID_ALLELE_CHARS


def test_n_is_an_allowed_base(repeat_reference):
    (row,) = process_line(line(alt="N"), repeat_reference)
    assert row.alt == "N"


def test_ref_mismatch(repeat_reference):
    assert reason_of(line(ref="A", alt="G"), repeat_reference) is RejectReason.REF_MISMATCH


def test_ref_comparison_is_case_insensitive(repeat_reference):
    (row,) = process_line(line(ref="t", alt="g"), repeat_reference)
    assert (row.ref, row.alt) == ("T", "G")


def test_multi_base_ref_mismatch(repeat_reference):
    assert reason_of(line(pos="4", ref="ACAG", alt="A"), repeat_reference) is (
        RejectReason.REF_MISMATCH
    )


def test_non_numeric_qual(repeat_reference):
    assert reason_of(line(qual="x"), repeat_reference) is RejectReason.NON_NUMERIC_QUAL


def test_malformed_line(repeat_reference):
    assert reason_of("chr1\t3", repeat_reference) is RejectReason.MALFORMED_LINE


def test_alt_equal_to_ref_is_rejected(repeat_reference):
    assert reason_of(line(ref="T", alt="T"), repeat_reference) is RejectReason.MALFORMED_LINE


def test_precedence_contig_before_bounds_before_symbolic_before_chars_before_ref(
    repeat_reference,
):
    assert reason_of(line(chrom="nope", pos="0", alt="<DEL>"), repeat_reference) is (
        RejectReason.UNKNOWN_CONTIG
    )
    assert reason_of(line(pos="0", alt="<DEL>"), repeat_reference) is (
        RejectReason.POS_OUT_OF_BOUNDS
    )
    assert reason_of(line(alt="<DEL>", ref="Z"), repeat_reference) is (
        RejectReason.UNSUPPORTED_SYMBOLIC
    )
    assert reason_of(line(ref="Z", alt="G"), repeat_reference) is (
        RejectReason.INVALID_ALLELE_CHARS
    )


def test_chunk_processing_counts_reasons_and_keeps_original_line_numbers(repeat_reference):
    lines = [
        line(),  # file line 10: accepted
        "",  # file line 11: blank, skipped but counted
        line(chrom="chrZ"),  # 12
        line(ref="A"),  # 13
        line(alt="C,G"),  # 14: multi-allelic, accepted as 2 rows
        line(chrom="chrZ"),  # 15
    ]
    result = process_lines(lines, 10, repeat_reference, max_rejected_samples=2)
    assert (result.total, result.accepted, result.rejected) == (5, 2, 3)
    assert len(result.rows) == 3
    assert dict(result.reject_reasons) == {"unknown_contig": 2, "ref_mismatch": 1}
    # Samples are capped, counts are not.
    assert [r.line_no for r in result.rejected_samples] == [12, 13]
    assert result.rejected_samples[0].raw_line == line(chrom="chrZ")
