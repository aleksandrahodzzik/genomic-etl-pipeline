"""The pure-Python pipeline run over the committed fixture, checked against truth.json.

truth.json is produced by the generator from how each record was *built* (not by running
this pipeline), so agreement here means validation, splitting, normalization and
classification all land on what the data was constructed to contain.
"""

import json
from collections import Counter

import pytest

from variant_etl.pipeline.reference import FastaReference
from variant_etl.pipeline.transform import ChunkResult, process_lines


@pytest.fixture(scope="module")
def truth(data_dir):
    return json.loads((data_dir / "truth.json").read_text())


@pytest.fixture(scope="module")
def result(data_dir) -> ChunkResult:
    reference = FastaReference(data_dir / "ref.fa")
    lines = (data_dir / "variants.vcf").read_text().splitlines()
    header_len = sum(1 for line in lines if line.startswith("#"))
    return process_lines(lines[header_len:], header_len + 1, reference, max_rejected_samples=10_000)


def test_record_counts(result, truth):
    assert result.total == truth["records_total"]
    assert result.accepted == truth["records_accepted"]
    assert result.rejected == truth["records_rejected"]


def test_every_reject_reason_is_exercised_and_counted_exactly(result, truth):
    assert set(truth["rejected_by_reason"]) == {
        "malformed_line",
        "non_numeric_qual",
        "unknown_contig",
        "pos_out_of_bounds",
        "unsupported_symbolic",
        "invalid_allele_chars",
        "ref_mismatch",
    }
    assert dict(result.reject_reasons) == truth["rejected_by_reason"]


def test_rejected_line_numbers_and_reasons_match_the_original_file(result, truth):
    actual = [[r.line_no, r.reason] for r in result.rejected_samples]
    assert actual == truth["rejected_lines"]


def test_rows_by_type_match(result, truth):
    counts = Counter(row.variant_type for row in result.rows)
    assert len(result.rows) == truth["rows_total"]
    assert {t: counts.get(t, 0) for t in ("SNV", "MNV", "INS", "DEL")} == truth["rows_by_type"]
    assert counts.get("COMPLEX", 0) == 0


def test_transitions_and_transversions_match(result, truth):
    snvs = [row for row in result.rows if row.variant_type == "SNV"]
    transitions = sum(row.is_transition for row in snvs)
    assert transitions == truth["snv_transitions"]
    assert len(snvs) - transitions == truth["snv_transversions"]


def test_titv_is_close_to_the_requested_value(truth):
    assert truth["titv"] == pytest.approx(truth["params"]["titv"], abs=0.05)


def test_normalization_actually_changed_something(result):
    # Split multi-allelic rows and left-aligned indels are flagged; plain SNVs are not.
    assert 0 < result.normalized < len(result.rows)
    flagged_snvs = sum(r.was_normalized for r in result.rows if r.variant_type == "SNV")
    assert flagged_snvs < sum(r.variant_type == "SNV" for r in result.rows)


def test_all_rows_are_inside_their_contig(result, truth):
    for row in result.rows:
        assert 1 <= row.pos <= truth["contigs"][row.chrom]
