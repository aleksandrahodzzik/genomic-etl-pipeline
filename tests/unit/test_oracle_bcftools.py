"""Oracle test: our normalizer vs `bcftools norm -m -any -f ref.fa` on the same input.

Both sides get the same records. bcftools gets the VCF with the deliberately invalid lines
removed (it would abort on them); our pipeline gets the whole file, so this also checks
that exactly the invalid lines are dropped. The comparison is a multiset of
(CHROM, POS, REF, ALT), which is what normalization is supposed to make canonical.

bcftools is used from PATH when present (CI installs it with apt). Otherwise the pinned
biocontainers image is run through Docker. If neither is available the tests are skipped
with an explicit reason; they are never silently passed.
"""

import json
import shutil
import subprocess
from collections import Counter
from pathlib import Path

import pytest

from variant_etl.pipeline import transform
from variant_etl.pipeline.reference import FastaReference
from variant_etl.pipeline.transform import process_lines

pytestmark = pytest.mark.oracle

BCFTOOLS_IMAGE = "quay.io/biocontainers/bcftools:1.21--h8b25389_0"
DOCKER_RUN_FAILED = 125  # `docker run` itself failed (daemon down, image pull failed, ...)

Keys = Counter[tuple[str, int, str, str]]


def run_bcftools_norm(workdir: Path) -> str:
    args = ["norm", "-m", "-any"]
    if shutil.which("bcftools"):
        command = ["bcftools", *args, "-f", str(workdir / "ref.fa"), str(workdir / "valid.vcf")]
    elif shutil.which("docker"):
        command = [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{workdir}:/work",
            BCFTOOLS_IMAGE,
            "bcftools",
            *args,
            "-f",
            "/work/ref.fa",
            "/work/valid.vcf",
        ]
    else:
        pytest.skip("bcftools oracle skipped: neither `bcftools` nor `docker` found in PATH")
    proc = subprocess.run(command, capture_output=True, text=True, timeout=300)
    if command[0] == "docker" and proc.returncode == DOCKER_RUN_FAILED:
        pytest.skip(
            f"bcftools oracle skipped: docker could not run {BCFTOOLS_IMAGE}: {proc.stderr}"
        )
    assert proc.returncode == 0, f"bcftools failed:\n{proc.stderr}"
    return proc.stdout


def key_multiset(vcf_text: str) -> Keys:
    keys: Keys = Counter()
    for line in vcf_text.splitlines():
        if line and not line.startswith("#"):
            chrom, pos, _id, ref, alt = line.split("\t")[:5]
            keys[(chrom, int(pos), ref.upper(), alt.upper())] += 1
    return keys


def oracle_and_ours(data_dir: Path, workdir: Path) -> tuple[Keys, Keys]:
    truth = json.loads((data_dir / "truth.json").read_text())
    invalid_lines = {line_no for line_no, _reason in truth["rejected_lines"]}

    lines = (data_dir / "variants.vcf").read_text().splitlines()
    valid_only = [line for i, line in enumerate(lines, start=1) if i not in invalid_lines]
    (workdir / "valid.vcf").write_text("\n".join(valid_only) + "\n", newline="\n")
    for name in ("ref.fa", "ref.fa.fai"):
        shutil.copy(data_dir / name, workdir / name)

    oracle = key_multiset(run_bcftools_norm(workdir))

    header_len = sum(1 for line in lines if line.startswith("#"))
    reference = FastaReference(data_dir / "ref.fa")
    result = process_lines(lines[header_len:], header_len + 1, reference, 10_000)
    reference.close()
    ours: Keys = Counter((r.chrom, r.pos, r.ref, r.alt) for r in result.rows)
    return oracle, ours


def test_normalization_matches_bcftools_norm(data_dir, tmp_path):
    oracle, ours = oracle_and_ours(data_dir, tmp_path)
    assert sum(oracle.values()) > 2_000, "oracle produced suspiciously few records"
    only_ours = list((ours - oracle).items())[:10]
    only_oracle = list((oracle - ours).items())[:10]
    assert ours == oracle, f"only ours: {only_ours}\nonly bcftools: {only_oracle}"


def test_oracle_detects_a_normalizer_that_does_not_left_align(data_dir, tmp_path, monkeypatch):
    """Guards the guard: a trim-only normalizer must NOT agree with bcftools on this data,
    otherwise the fixture would not be exercising left-alignment at all."""

    def trim_only(reference, chrom, pos, ref, alt):
        while len(ref) >= 2 and len(alt) >= 2 and ref[-1] == alt[-1]:
            ref, alt = ref[:-1], alt[:-1]
        while len(ref) >= 2 and len(alt) >= 2 and ref[0] == alt[0]:
            ref, alt, pos = ref[1:], alt[1:], pos + 1
        return pos, ref, alt

    monkeypatch.setattr(transform, "normalize_allele", trim_only)
    oracle, ours = oracle_and_ours(data_dir, tmp_path)
    assert ours != oracle
    # Many indels, not a stray one, differ.
    assert sum((oracle - ours).values()) > 50
