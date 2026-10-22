"""Normalizer + validator unit tests."""

import uuid

import pytest

from app.etl.normalizer import (
    classify_variant,
    info_value_for_allele,
    is_transition,
    normalize_chrom,
    normalize_vcf_record,
    parse_info,
    trim_alleles,
)
from app.etl.parser import VcfRecord
from app.etl.validator import validate_variant, validate_vcf_record


def make_record(**overrides) -> VcfRecord:
    values = dict(
        line_number=1,
        chrom="chr1",
        pos="100",
        variant_id=".",
        ref="A",
        alt="G",
        qual="50",
        filter="PASS",
        info="DP=10;AF=0.1",
        alt_index=0,
    )
    values.update(overrides)
    return VcfRecord(**values)


# ----------------------------- chromosomes ----------------------------- #
@pytest.mark.parametrize(
    "raw, expected",
    [
        ("1", "chr1"),
        ("chr1", "chr1"),
        ("chr01", "chr1"),
        ("01", "chr1"),
        ("22", "chr22"),
        ("CHR22", "chr22"),
        ("x", "chrX"),
        ("chrx", "chrX"),
        ("Y", "chrY"),
        ("MT", "chrM"),
        ("chrM", "chrM"),
        ("m", "chrM"),
    ],
)
def test_normalize_chrom_valid(raw, expected):
    assert normalize_chrom(raw) == expected


@pytest.mark.parametrize("raw", ["", "chr", "0", "23", "chr99", "chrUn_KI270302v1", "Z"])
def test_normalize_chrom_invalid(raw):
    assert normalize_chrom(raw) is None


# --------------------------- allele trimming --------------------------- #
def test_trim_spec_example_suffix_then_prefix():
    assert trim_alleles(100, "ATCG", "ATGG") == (102, "C", "G")


def test_trim_suffix_only():
    assert trim_alleles(50, "CAT", "GAT") == (50, "C", "G")


def test_trim_prefix_only_advances_position():
    assert trim_alleles(10, "ACG", "ATG") == (11, "C", "T")  # suffix G, then prefix A
    assert trim_alleles(10, "AGT", "ACC") == (11, "GT", "CC")  # prefix A only


def test_trim_keeps_anchor_base_for_indels():
    assert trim_alleles(100, "AT", "A") == (100, "AT", "A")
    assert trim_alleles(100, "A", "AT") == (100, "A", "AT")


def test_trim_padded_deletion_reduces_to_anchored_deletion():
    # ref ATCGG / alt ATG -> suffix G, G trimmed; prefix A trimmed -> TCG / T at pos+1
    assert trim_alleles(200, "ATCGG", "ATG") == (201, "TCG", "T")


def test_trim_is_idempotent():
    once = trim_alleles(100, "ATCG", "ATGG")
    assert trim_alleles(*once) == once


# ---------------------------- classification --------------------------- #
@pytest.mark.parametrize(
    "ref, alt, expected",
    [
        ("A", "G", "SNP"),
        ("A", "AT", "INSERTION"),
        ("A", "TTA", "INSERTION"),
        ("AT", "A", "DELETION"),
        ("GCA", "A", "DELETION"),
        ("AT", "GC", "MNP"),
        ("ATC", "GCA", "MNP"),
        ("AT", "GCA", "COMPLEX"),
        ("ATG", "C", "COMPLEX"),
    ],
)
def test_classify_variant(ref, alt, expected):
    assert classify_variant(ref, alt) == expected


@pytest.mark.parametrize(
    "ref, alt, expected",
    [
        ("A", "G", True),
        ("G", "A", True),
        ("C", "T", True),
        ("T", "C", True),
        ("A", "C", False),
        ("A", "T", False),
        ("G", "C", False),
        ("G", "T", False),
    ],
)
def test_transition_detection_for_snps(ref, alt, expected):
    assert is_transition(ref, alt, "SNP") is expected


def test_transition_false_for_non_snp():
    assert is_transition("A", "AG", "INSERTION") is False


# ------------------------------- INFO ---------------------------------- #
def test_parse_info_key_values_and_flags():
    info = parse_info("DP=112;AF=0.245;AC=2;DB")
    assert info["DP"] == "112"
    assert info["AF"] == "0.245"
    assert info["AC"] == "2"
    assert info["DB"] is True


@pytest.mark.parametrize("empty", ["", "."])
def test_parse_info_empty(empty):
    assert parse_info(empty) == {}


def test_multiallelic_af_picks_matching_allele():
    info = parse_info("DP=50;AF=0.1,0.02,0.3")
    assert info_value_for_allele(info, "AF", 0) == "0.1"
    assert info_value_for_allele(info, "AF", 1) == "0.02"
    assert info_value_for_allele(info, "AF", 2) == "0.3"
    assert info_value_for_allele(info, "AF", 3) is None


def test_single_value_applies_to_all_alleles():
    info = parse_info("DP=50")
    assert info_value_for_allele(info, "DP", 0) == "50"
    assert info_value_for_allele(info, "DP", 1) == "50"


# ----------------------------- validator ------------------------------- #
def test_validate_accepts_good_variant():
    result = validate_variant("chr1", 100, "A", "G", 50.0, 0.2)
    assert result.is_valid is True
    assert result.reason is None


def test_validation_result_unpacks_as_tuple():
    is_valid, reason = validate_variant("chr1", 100, "A", "G")
    assert (is_valid, reason) == (True, None)


@pytest.mark.parametrize(
    "kwargs, fragment",
    [
        (dict(chrom="chrUn", pos=1, ref="A", alt="G"), "chromosome"),
        (dict(chrom="chr1", pos=0, ref="A", alt="G"), "position"),
        (dict(chrom="chr1", pos=-4, ref="A", alt="G"), "position"),
        (dict(chrom="chr1", pos=5, ref="", alt="G"), "REF"),
        (dict(chrom="chr1", pos=5, ref="A", alt=""), "ALT"),
        (dict(chrom="chr1", pos=5, ref="A", alt="."), "ALT"),
        (dict(chrom="chr1", pos=5, ref="A", alt="<DEL>"), "ALT"),
        (dict(chrom="chr1", pos=5, ref="A", alt="*"), "ALT"),
        (dict(chrom="chr1", pos=5, ref="AX", alt="G"), "REF"),
        (dict(chrom="chr1", pos=5, ref="A", alt="A"), "identical"),
        (dict(chrom="chr1", pos=5, ref="a", alt="A"), "identical"),
        (dict(chrom="chr1", pos=5, ref="A", alt="G", quality=-1.0), "QUAL"),
        (dict(chrom="chr1", pos=5, ref="A", alt="G", quality=float("nan")), "QUAL"),
        (dict(chrom="chr1", pos=5, ref="A", alt="G", allele_frequency=1.2), "frequency"),
        (dict(chrom="chr1", pos=5, ref="A", alt="G", allele_frequency=-0.1), "frequency"),
    ],
)
def test_validate_rejects(kwargs, fragment):
    result = validate_variant(**kwargs)
    assert result.is_valid is False
    assert fragment in result.reason


def test_validate_allows_boundaries():
    assert validate_variant("chr1", 1, "A", "G", 0.0, 0.0).is_valid
    assert validate_variant("chr1", 1, "A", "G", None, 1.0).is_valid
    assert validate_variant("chr1", 1, "N", "A").is_valid


def test_validate_record_rejects_malformed_inputs():
    assert not validate_vcf_record(make_record(parse_error="expected >= 8 columns")).is_valid
    assert not validate_vcf_record(make_record(pos="abc")).is_valid
    assert not validate_vcf_record(make_record(qual="high")).is_valid
    assert not validate_vcf_record(make_record(info="AF=banana")).is_valid
    assert not validate_vcf_record(make_record(info="AF=2.5")).is_valid
    assert not validate_vcf_record(make_record(variant_id="x" * 65)).is_valid


def test_validate_record_uses_matching_multiallelic_af():
    # second ALT carries the out-of-range AF
    record = make_record(alt="T", alt_index=1, info="AF=0.1,1.5")
    assert not validate_vcf_record(record).is_valid
    assert validate_vcf_record(make_record(alt="T", alt_index=0, info="AF=0.1,1.5")).is_valid


def test_validate_record_allows_missing_qual_and_info():
    assert validate_vcf_record(make_record(qual=".", info=".")).is_valid


# ------------------------ end-to-end normalization --------------------- #
def test_normalize_record_spec_example():
    job_id = uuid.uuid4()
    row = normalize_vcf_record(
        make_record(chrom="1", pos="100", ref="ATCG", alt="ATGG", variant_id="rs9"),
        job_id,
    )
    assert row["job_id"] == job_id
    assert row["chrom"] == "chr1"
    assert (row["pos"], row["ref_allele"], row["alt_allele"]) == (102, "C", "G")
    assert row["variant_type"] == "SNP"
    assert row["is_transition"] is False
    assert row["variant_id"] == "rs9"
    assert row["read_depth"] == 10
    assert row["allele_frequency"] == pytest.approx(0.1)
    assert row["filter_status"] == "PASS"


def test_normalize_record_missing_values_become_none():
    row = normalize_vcf_record(
        make_record(variant_id=".", qual=".", info=".", ref="a", alt="g"), None
    )
    assert row["variant_id"] is None
    assert row["quality"] is None
    assert row["read_depth"] is None
    assert row["allele_frequency"] is None
    assert (row["ref_allele"], row["alt_allele"]) == ("A", "G")
    assert row["is_transition"] is True


def test_normalize_record_multiallelic_uses_allele_specific_af():
    row = normalize_vcf_record(
        make_record(alt="T", alt_index=1, info="DP=40;AF=0.1,0.02"), None
    )
    assert row["allele_frequency"] == pytest.approx(0.02)
    assert row["read_depth"] == 40
