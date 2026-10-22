import pytest

from variant_etl.pipeline.records import RecordRejected, RejectReason
from variant_etl.pipeline.vcf import is_header_line, parse_record


def line(pos="100", ref="A", alt="C", qual="50", flt="PASS", info="AF=0.25"):
    return "\t".join(["chr1", pos, ".", ref, alt, qual, flt, info])


def test_parses_all_fixed_fields():
    record = parse_record(line())
    assert (record.chrom, record.pos, record.ref, record.alts) == ("chr1", 100, "A", ["C"])
    assert record.qual == 50.0
    assert record.filter == "PASS"
    assert record.af == [0.25]


def test_missing_values_become_none():
    record = parse_record(line(qual=".", flt=".", info="."))
    assert (record.qual, record.filter, record.af) == (None, None, None)


def test_alleles_are_upper_cased():
    record = parse_record(line(ref="acg", alt="a,tt"))
    assert (record.ref, record.alts) == ("ACG", ["A", "TT"])


def test_multi_allelic_af_is_one_value_per_alt():
    assert parse_record(line(alt="C,G", info="DP=3;AF=0.1,0.2")).af == [0.1, 0.2]


def test_af_key_must_match_exactly():
    assert parse_record(line(info="MAF=0.4;DP=9")).af is None


def test_af_with_wrong_arity_or_garbage_is_dropped_not_misattributed():
    assert parse_record(line(alt="C,G", info="AF=0.1")).af is None
    assert parse_record(line(info="AF=abc")).af is None
    assert parse_record(line(info="AF=nan")).af is None


def test_genotype_columns_are_ignored():
    record = parse_record(line() + "\tGT\t0/1\t1/1")
    assert record.pos == 100


@pytest.mark.parametrize(
    "bad",
    [
        "chr1\t100\t.\tA\tC",  # too few columns
        "chr1 100 . A C 50 PASS .",  # spaces instead of tabs
        "",
        line(pos="abc"),
        line(pos="-5"),
        line(pos="+5"),
        line(pos="1_0"),
        line(pos="12.5"),
        line(pos=""),
        line(ref=""),
        line(alt=""),
        line(alt="C,,G"),
        "\t100\t.\tA\tC\t50\tPASS\t.",  # empty chrom
    ],
)
def test_structurally_broken_lines_are_malformed(bad):
    with pytest.raises(RecordRejected) as exc:
        parse_record(bad)
    assert exc.value.reason is RejectReason.MALFORMED_LINE


@pytest.mark.parametrize("qual", ["high", "5,5", "1e", "nan", "inf", "-inf"])
def test_non_numeric_qual(qual):
    with pytest.raises(RecordRejected) as exc:
        parse_record(line(qual=qual))
    assert exc.value.reason is RejectReason.NON_NUMERIC_QUAL


def test_scientific_notation_qual_is_numeric():
    assert parse_record(line(qual="1.5e2")).qual == 150.0


def test_header_detection():
    assert is_header_line("##fileformat=VCFv4.2")
    assert is_header_line("#CHROM\tPOS")
    assert not is_header_line("chr1\t1\t.\tA\tC\t.\t.\t.")
