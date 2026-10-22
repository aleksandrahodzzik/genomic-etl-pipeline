"""Hand-worked normalization cases.

Reference (chr1, 1-based): G1 G2 T3 A4 C5 A6 C7 A8 C9 T10 T11 C12 A13 A14 A15 A16 G17 T18
  - dinucleotide repeat (AC)x3 at 4-9, preceded by T3
  - homopolymer A-run at 13-16, preceded by C12
chr2 = AAATGC (indels at the very first base)
"""

import pytest

from variant_etl.pipeline.normalize import classify, is_transition, normalize_allele
from variant_etl.pipeline.records import VariantType
from variant_etl.pipeline.transform import process_line


def norm(reference, pos, ref, alt, chrom="chr1"):
    return normalize_allele(reference, chrom, pos, ref, alt)


# --- trimming ---------------------------------------------------------------------------


def test_snv_is_untouched(repeat_reference):
    assert norm(repeat_reference, 5, "C", "G") == (5, "C", "G")


def test_left_trim_of_shared_prefix_moves_pos_forward(repeat_reference):
    # CA -> CG at 5 is really A -> G at 6.
    assert norm(repeat_reference, 5, "CA", "CG") == (6, "A", "G")


def test_right_trim_of_shared_suffix(repeat_reference):
    # AC -> GC at 4: suffix C is shared, so it is A -> G at 4.
    assert norm(repeat_reference, 4, "AC", "GC") == (4, "A", "G")


def test_trim_both_sides_of_an_mnv(repeat_reference):
    # ACA -> AGA at 4 is C -> G at 5.
    assert norm(repeat_reference, 4, "ACA", "AGA") == (5, "C", "G")


def test_already_minimal_mnv_is_untouched(repeat_reference):
    assert norm(repeat_reference, 4, "AC", "TG") == (4, "AC", "TG")


def test_deletion_in_a_short_run_is_anchored_on_the_base_before_the_run(repeat_reference):
    # T10 T11 is a run of two T preceded by C9; deleting one T written at 10 moves to 9.
    assert norm(repeat_reference, 10, "TT", "T") == (9, "CT", "C")


# --- left alignment in a homopolymer ------------------------------------------------------


def test_homopolymer_deletion_written_at_the_right_end_moves_to_the_left(repeat_reference):
    # Delete one A of A13..A16, written anchored at A15. Left-most anchor is C12.
    assert norm(repeat_reference, 15, "AA", "A") == (12, "CA", "C")


def test_homopolymer_deletion_already_left_aligned_is_stable(repeat_reference):
    assert norm(repeat_reference, 12, "CA", "C") == (12, "CA", "C")


def test_homopolymer_insertion_written_at_the_right_end_moves_to_the_left(repeat_reference):
    # Insert an A after A16, written at 16. Left-most anchor is C12.
    assert norm(repeat_reference, 16, "A", "AA") == (12, "C", "CA")


def test_every_equivalent_homopolymer_deletion_has_one_canonical_form(repeat_reference):
    forms = {norm(repeat_reference, pos, "AA", "A") for pos in (13, 14, 15)}
    assert forms == {(12, "CA", "C")}


def test_every_equivalent_homopolymer_insertion_has_one_canonical_form(repeat_reference):
    forms = {norm(repeat_reference, pos, "A", "AA") for pos in (13, 14, 15, 16)}
    assert forms == {(12, "C", "CA")}


# --- left alignment in a dinucleotide repeat ---------------------------------------------


def test_dinucleotide_deletion_moves_to_the_start_of_the_repeat(repeat_reference):
    # Delete AC (positions 8-9), written anchored at C7. Left-most anchor is T3.
    assert norm(repeat_reference, 7, "CAC", "C") == (3, "TAC", "T")


def test_dinucleotide_insertion_moves_to_the_start_of_the_repeat(repeat_reference):
    # Insert AC after C9.
    assert norm(repeat_reference, 9, "C", "CAC") == (3, "T", "TAC")


def test_deletion_that_cannot_slide_left_stays_where_it_is(repeat_reference):
    # Deleting both T10 and T11, anchored on C9: the base before the anchor (A8) differs
    # from the last deleted base (T), so it cannot move further.
    assert norm(repeat_reference, 9, "CTT", "C") == (9, "CTT", "C")


# --- contig start ------------------------------------------------------------------------


def test_deletion_of_first_base_is_anchored_on_the_right(repeat_reference):
    # chr2 = A A A T G C: delete the first A. No base to the left, so anchor on the right.
    assert norm(repeat_reference, 1, "AA", "A", chrom="chr2") == (1, "AA", "A")


def test_insertion_before_first_base_is_anchored_on_the_right(repeat_reference):
    assert norm(repeat_reference, 1, "A", "AA", chrom="chr2") == (1, "A", "AA")


def test_left_alignment_stops_at_contig_start_inside_a_run(repeat_reference):
    # Delete one A of the leading A-run (A1 A2 A3), written at the run's end.
    assert norm(repeat_reference, 3, "AA", "A", chrom="chr2") == (1, "AA", "A")


# --- classification -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "ref,alt,expected",
    [
        ("A", "G", VariantType.SNV),
        ("AC", "TG", VariantType.MNV),
        ("A", "AT", VariantType.INS),
        ("A", "ATTT", VariantType.INS),
        ("AT", "A", VariantType.DEL),
        ("ATTT", "A", VariantType.DEL),
        ("AT", "GCC", VariantType.COMPLEX),
        ("AT", "ACG", VariantType.COMPLEX),
        ("TAC", "T", VariantType.DEL),
        ("T", "TAC", VariantType.INS),
    ],
)
def test_classify(ref, alt, expected):
    assert classify(ref, alt) is expected


@pytest.mark.parametrize(
    "ref,alt,expected",
    [
        ("A", "G", True),
        ("G", "A", True),
        ("C", "T", True),
        ("T", "C", True),
        ("A", "C", False),
        ("A", "T", False),
        ("G", "C", False),
        ("G", "T", False),
        ("A", "N", False),
        ("AC", "GT", False),
    ],
)
def test_is_transition(ref, alt, expected):
    assert is_transition(ref, alt) is expected


# --- whole-record behaviour (split + normalize + flags) -------------------------------------


def line(pos, ref, alt, chrom="chr1", info="."):
    return "\t".join([chrom, str(pos), ".", ref, alt, "40", "PASS", info])


def test_multiallelic_snv_site_is_split_into_one_row_per_alt(repeat_reference):
    rows = process_line(line(5, "C", "A,G,T", info="AF=0.1,0.2,0.3"), repeat_reference)
    assert [(r.pos, r.ref, r.alt, r.af) for r in rows] == [
        (5, "C", "A", 0.1),
        (5, "C", "G", 0.2),
        (5, "C", "T", 0.3),
    ]
    assert all(r.was_normalized for r in rows)  # present only because of the split


def test_multiallelic_with_indel_and_snv_alleles_are_trimmed_independently(repeat_reference):
    # At 10: TT -> T (deletion, left-aligns to 9) and TT -> TC (really an SNV T->C at 11).
    rows = process_line(line(10, "TT", "T,TC"), repeat_reference)
    assert [(r.pos, r.ref, r.alt, r.variant_type) for r in rows] == [
        (9, "CT", "C", "DEL"),
        (11, "T", "C", "SNV"),
    ]


def test_biallelic_untouched_record_is_not_flagged(repeat_reference):
    (row,) = process_line(line(5, "C", "G"), repeat_reference)
    assert row.was_normalized is False


def test_left_aligned_indel_is_flagged_and_typed(repeat_reference):
    (row,) = process_line(line(15, "AA", "A"), repeat_reference)
    assert (row.pos, row.ref, row.alt) == (12, "CA", "C")
    assert row.variant_type == "DEL"
    assert row.was_normalized is True


def test_is_transition_is_set_on_the_row(repeat_reference):
    (ti,) = process_line(line(5, "C", "T"), repeat_reference)  # C -> T
    (tv,) = process_line(line(5, "C", "A"), repeat_reference)  # C -> A
    assert (ti.is_transition, tv.is_transition) == (True, False)


def test_qual_filter_and_af_are_carried_to_each_split_row(repeat_reference):
    rows = process_line(
        "\t".join(["chr1", "5", ".", "C", "A,G", "12.5", "q10", "AF=0.4,0.6"]), repeat_reference
    )
    assert [(r.qual, r.filter, r.af) for r in rows] == [(12.5, "q10", 0.4), (12.5, "q10", 0.6)]
