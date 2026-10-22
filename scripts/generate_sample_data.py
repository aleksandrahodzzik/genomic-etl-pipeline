"""Synthetic, realistic genomic test data.

Generates a sorted VCF (GRCh38-style chr1..chr22, chrX, chrY) containing:
  * SNPs with a realistic transition bias (~2.1 for bi-allelic SNPs; ~1.7 over the whole
    file because extra ALT alleles at multi-allelic sites are drawn uniformly),
  * anchored insertions and deletions,
  * *unnormalized* indels / SNPs padded with shared flanking bases (need trimming),
  * multi-allelic sites (decomposed by the parser),
  * `DP`, `AF`, `AC`, `AN` INFO tags and mixed chromosome spellings (`chr1`, `1`, `chr01`),
  * ~1.5% deliberately invalid rows so the validator has something to reject,
and a small multi-contig FASTA reference.

Usage:  python scripts/generate_sample_data.py [--num-variants 100000] [--output-dir data]
"""

import argparse
import random
import time
from pathlib import Path
from typing import BinaryIO, Dict, Iterator, List, Tuple

# GRCh38 primary assembly chromosome lengths (bp)
CHROMOSOME_LENGTHS: Dict[str, int] = {
    "chr1": 248_956_422,
    "chr2": 242_193_529,
    "chr3": 198_295_559,
    "chr4": 190_214_555,
    "chr5": 181_538_259,
    "chr6": 170_805_979,
    "chr7": 159_345_973,
    "chr8": 145_138_636,
    "chr9": 138_394_717,
    "chr10": 133_797_422,
    "chr11": 135_086_622,
    "chr12": 133_275_309,
    "chr13": 114_364_328,
    "chr14": 107_043_718,
    "chr15": 101_991_189,
    "chr16": 90_338_345,
    "chr17": 83_257_441,
    "chr18": 80_373_285,
    "chr19": 58_617_616,
    "chr20": 64_444_167,
    "chr21": 46_709_983,
    "chr22": 50_818_468,
    "chrX": 156_040_895,
    "chrY": 57_227_415,
}
# chrY carries far fewer called variants in a typical cohort
CHROMOSOME_WEIGHTS: Dict[str, float] = {
    name: length * (0.25 if name == "chrY" else 1.0)
    for name, length in CHROMOSOME_LENGTHS.items()
}

BASES = ["A", "C", "G", "T"]
BASE_WEIGHTS = [0.295, 0.205, 0.205, 0.295]  # ~41% GC like the human genome
TRANSITION_PARTNER = {"A": "G", "G": "A", "C": "T", "T": "C"}
TRANSVERSION_PARTNERS = {
    "A": ["C", "T"],
    "G": ["C", "T"],
    "C": ["A", "G"],
    "T": ["A", "G"],
}
SAMPLE_COUNT_AN = 2000  # AN: total alleles in called genotypes (1000 diploid samples)

INVALID_RATE = 0.015

VCF_HEADER_LINES = [
    "##fileformat=VCFv4.2",
    "##source=genomic-etl-pipeline/generate_sample_data.py",
    "##reference=GRCh38-synthetic",
    '##INFO=<ID=DP,Number=1,Type=Integer,Description="Total read depth">',
    '##INFO=<ID=AF,Number=A,Type=Float,Description="Allele frequency per ALT allele">',
    '##INFO=<ID=AC,Number=A,Type=Integer,Description="Allele count per ALT allele">',
    '##INFO=<ID=AN,Number=1,Type=Integer,Description="Total number of alleles">',
    '##FILTER=<ID=PASS,Description="All filters passed">',
    '##FILTER=<ID=LowQual,Description="Low quality variant call">',
    "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO",
]


def _rand_base(rng: random.Random) -> str:
    return rng.choices(BASES, weights=BASE_WEIGHTS, k=1)[0]


def _rand_seq(rng: random.Random, length: int) -> str:
    return "".join(rng.choices(BASES, weights=BASE_WEIGHTS, k=length))


def _other_base(rng: random.Random, base: str) -> str:
    return rng.choice([b for b in BASES if b != base])


def _snp_alt(rng: random.Random, base: str) -> str:
    """Substitute allele with a realistic bias: ~68% transitions (Ti/Tv ~ 2.1)."""
    if rng.random() < 0.68:
        return TRANSITION_PARTNER[base]
    return rng.choice(TRANSVERSION_PARTNERS[base])


def _chrom_label(rng: random.Random, chrom: str) -> str:
    """Mix naming conventions so the normalizer has real work to do."""
    roll = rng.random()
    bare = chrom[3:]
    if roll < 0.85:
        return chrom
    if roll < 0.97:
        return bare
    if bare.isdigit() and len(bare) == 1:
        return f"chr0{bare}"
    return f"chr{bare.lower()}"


def _allele_frequency(rng: random.Random) -> float:
    roll = rng.random()
    if roll < 0.55:  # rare
        value = rng.uniform(0.0005, 0.0099)
    elif roll < 0.80:  # low frequency
        value = rng.uniform(0.01, 0.05)
    else:  # common
        value = rng.uniform(0.0501, 0.5)
    return round(value, 4)


def _quality(rng: random.Random) -> float:
    return round(min(5000.0, max(1.0, rng.lognormvariate(5.3, 0.8))), 2)


def _filter_for(quality: float) -> str:
    return "PASS" if quality >= 30.0 else "LowQual"


def _info(depth: int, afs: List[float]) -> str:
    counts = [max(1, round(af * SAMPLE_COUNT_AN)) for af in afs]
    return (
        f"DP={depth};AF={','.join(f'{af:.4f}' for af in afs)};"
        f"AC={','.join(str(c) for c in counts)};AN={SAMPLE_COUNT_AN}"
    )


def _valid_variant(
    rng: random.Random,
) -> Tuple[str, str, List[float]]:
    """Return (ref, alt-field, per-ALT AFs) for one valid record."""
    roll = rng.random()
    base = _rand_base(rng)

    if roll < 0.74:  # SNP (Ti/Tv ~ 2.1 => ~68% transitions)
        return base, _snp_alt(rng, base), [_allele_frequency(rng)]

    if roll < 0.80:  # anchored insertion
        return base, base + _rand_seq(rng, rng.randint(1, 4)), [_allele_frequency(rng)]

    if roll < 0.87:  # anchored deletion
        return base + _rand_seq(rng, rng.randint(1, 4)), base, [_allele_frequency(rng)]

    if roll < 0.92:  # unnormalized: padded with shared flanks
        left = _rand_seq(rng, rng.randint(1, 3))
        right = _rand_seq(rng, rng.randint(1, 3))
        if rng.random() < 0.5:
            mid_ref = _rand_base(rng)
            mid_alt = _snp_alt(rng, mid_ref)  # becomes a SNP after trimming
            return left + mid_ref + right, left + mid_alt + right, [_allele_frequency(rng)]
        deleted = _rand_seq(rng, rng.randint(1, 3))  # becomes a deletion after trimming
        return left + deleted + right, left + right, [_allele_frequency(rng)]

    if roll < 0.94:  # MNP
        length = rng.randint(2, 3)
        ref = _rand_seq(rng, length)
        alt = "".join(_other_base(rng, b) for b in ref)
        return ref, alt, [_allele_frequency(rng)]

    # multi-allelic SNP site (2-3 ALT alleles)
    first_alt = _snp_alt(rng, base)
    others = [b for b in BASES if b not in (base, first_alt)]
    alts = [first_alt] + rng.sample(others, rng.randint(1, 2))
    afs = [round(_allele_frequency(rng) / 2.0, 4) or 0.0005 for _ in alts]
    return base, ",".join(alts), afs


def _invalid_line(rng: random.Random, chrom: str, pos: int) -> str:
    """Produce one structurally or semantically invalid VCF row."""
    kind = rng.randint(0, 7)
    ref = _rand_base(rng)
    alt = _other_base(rng, ref)
    qual = f"{_quality(rng):.2f}"
    info = _info(rng.randint(10, 120), [_allele_frequency(rng)])
    if kind == 0:
        return f"{chrom}\t0\t.\t{ref}\t{alt}\t{qual}\tPASS\t{info}"
    if kind == 1:
        return f"{chrom}\t{pos}\t.\t{ref}\t{ref}\t{qual}\tPASS\t{info}"
    if kind == 2:
        return f"{chrom}\t{pos}\t.\t{ref}\t<DEL>\t{qual}\tPASS\t{info}"
    if kind == 3:
        return f"{chrom}\t{pos}\t.\t{ref}\t{alt}\t-{qual}\tPASS\t{info}"
    if kind == 4:
        return f"{chrom}\t{pos}\t.\t{ref}\t{alt}\t{qual}\tPASS\tDP=50;AF=1.73;AC=9;AN=2000"
    if kind == 5:
        return f"chrUn_KI270302v1\t{pos}\t.\t{ref}\t{alt}\t{qual}\tPASS\t{info}"
    if kind == 6:
        return f"{chrom}\t{pos}\t.\t{ref}"  # truncated row
    return f"{chrom}\tabc\t.\t{ref}\t{alt}\t{qual}\tPASS\t{info}"


def _chromosome_counts(num_variants: int) -> Dict[str, int]:
    total_weight = sum(CHROMOSOME_WEIGHTS.values())
    counts = {
        name: int(num_variants * weight / total_weight)
        for name, weight in CHROMOSOME_WEIGHTS.items()
    }
    counts["chr1"] += num_variants - sum(counts.values())
    return counts


def generate_vcf_lines(
    num_variants: int, seed: int = 42, invalid_rate: float = INVALID_RATE
) -> Iterator[str]:
    """Yield VCF lines (without trailing newline): header first, then sorted data rows.

    `num_variants` is the number of data lines; multi-allelic lines decompose into more
    records during ETL, so the pipeline sees slightly more than `num_variants` records.
    """
    if num_variants < 1:
        raise ValueError("num_variants must be >= 1")
    rng = random.Random(seed)
    yield from VCF_HEADER_LINES

    for chrom, count in _chromosome_counts(num_variants).items():
        if count == 0:
            continue
        length = CHROMOSOME_LENGTHS[chrom]
        positions = sorted(rng.sample(range(10_000, length - 10_000), count))
        for pos in positions:
            label = _chrom_label(rng, chrom)
            if rng.random() < invalid_rate:
                yield _invalid_line(rng, label, pos)
                continue
            ref, alt, afs = _valid_variant(rng)
            quality = _quality(rng)
            variant_id = f"rs{rng.randint(1_000, 999_999_999)}" if rng.random() < 0.55 else "."
            depth = max(4, int(rng.gauss(60, 25)))
            yield (
                f"{label}\t{pos}\t{variant_id}\t{ref}\t{alt}\t{quality:.2f}\t"
                f"{_filter_for(quality)}\t{_info(depth, afs)}"
            )


def write_vcf_stream(
    file_obj: BinaryIO, num_variants: int, seed: int = 42, flush_every: int = 20_000
) -> int:
    """Write generated VCF text to a binary file object. Returns the data-line count."""
    buffer: List[str] = []
    data_lines = 0
    for line in generate_vcf_lines(num_variants, seed=seed):
        buffer.append(line)
        if not line.startswith("#"):
            data_lines += 1
        if len(buffer) >= flush_every:
            file_obj.write(("\n".join(buffer) + "\n").encode("utf-8"))
            buffer.clear()
    if buffer:
        file_obj.write(("\n".join(buffer) + "\n").encode("utf-8"))
    return data_lines


# --------------------------------------------------------------------------- #
# FASTA
# --------------------------------------------------------------------------- #
FASTA_CONTIGS: List[Tuple[str, str, int, float, int]] = [
    # (contig id, description, length, GC fraction, leading N-run length)
    ("NC_000001.11", "Homo sapiens chromosome 1, GRCh38.p14 (synthetic fragment)", 150_000, 0.42, 10_000),
    ("NC_000002.12", "Homo sapiens chromosome 2, GRCh38.p14 (synthetic fragment)", 120_000, 0.40, 5_000),
    ("NC_000019.10", "Homo sapiens chromosome 19, GRCh38.p14 (synthetic fragment)", 90_000, 0.48, 0),
    ("NC_000023.11", "Homo sapiens chromosome X, GRCh38.p14 (synthetic fragment)", 100_000, 0.39, 2_500),
    ("NC_012920.1", "Homo sapiens mitochondrion, complete genome (synthetic)", 16_569, 0.44, 0),
]


def generate_fasta_lines(seed: int = 7, line_width: int = 60) -> Iterator[str]:
    """Yield FASTA lines for a small multi-contig reference."""
    rng = random.Random(seed)
    for contig_id, description, length, gc, leading_n in FASTA_CONTIGS:
        yield f">{contig_id} {description}"
        at = (1.0 - gc) / 2.0
        weights = [at, gc / 2.0, gc / 2.0, at]
        remaining_n = leading_n
        produced = 0
        while produced < length:
            width = min(line_width, length - produced)
            if remaining_n > 0:
                n_here = min(width, remaining_n)
                remaining_n -= n_here
                tail = "".join(rng.choices(BASES, weights=weights, k=width - n_here))
                yield "N" * n_here + tail
            else:
                yield "".join(rng.choices(BASES, weights=weights, k=width))
            produced += width


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate synthetic VCF + FASTA data.")
    parser.add_argument("--num-variants", type=int, default=100_000)
    parser.add_argument("--output-dir", default="data")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    vcf_path = out_dir / "sample_100k.vcf"
    fasta_path = out_dir / "sample_reference.fasta"

    started = time.perf_counter()
    with vcf_path.open("wb") as handle:
        data_lines = write_vcf_stream(handle, args.num_variants, seed=args.seed)
    with fasta_path.open("w", encoding="utf-8", newline="\n") as handle:
        for line in generate_fasta_lines():
            handle.write(line + "\n")
    elapsed = time.perf_counter() - started

    print(f"Wrote {vcf_path} ({data_lines:,} variant lines, {vcf_path.stat().st_size / 1e6:.1f} MB)")
    print(f"Wrote {fasta_path} ({fasta_path.stat().st_size / 1e6:.2f} MB)")
    print(f"Done in {elapsed:.1f}s")


if __name__ == "__main__":
    main()
