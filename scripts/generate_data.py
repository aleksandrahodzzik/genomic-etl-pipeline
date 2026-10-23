"""Generate a synthetic reference genome and a consistent VCF with known ground truth.

    python scripts/generate_data.py --contigs 3 --contig-length 5_000_000 \
        --variants 1_000_000 --titv 2.0 --error-rate 0.005 --seed 42

Outputs in --out-dir: ref.fa, ref.fa.fai, variants.vcf, truth.json.

What makes the data useful for testing (rather than random noise):
  * the reference contains homopolymer and dinucleotide repeats, and indels are placed in
    them *written at the right end of the repeat*, i.e. NOT left-aligned, so normalization
    has real work to do;
  * some sites are multi-allelic (SNV-only, and deletion+SNV);
  * a requested fraction of lines is deliberately broken, cycling through every reject
    reason, each built to trigger exactly one reason;
  * SNV alleles are drawn so the transition/transversion ratio approaches --titv;
  * truth.json records what was actually written (counts by type, Ti/Tv, line numbers and
    reasons of every broken line), computed from how each record was built, not by running
    the pipeline;
  * output is a pure function of the arguments: same seed, same bytes.
"""

import argparse
import hashlib
import json
import random
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

BASES = "ACGT"
GC_FRACTION = 0.41
TRANSITION = {"A": "G", "G": "A", "C": "T", "T": "C"}
TRANSVERSIONS = {"A": "CT", "G": "CT", "C": "AG", "T": "AG"}
REJECT_REASONS = [
    "malformed_line",
    "non_numeric_qual",
    "unknown_contig",
    "pos_out_of_bounds",
    "unsupported_symbolic",
    "invalid_allele_chars",
    "ref_mismatch",
]
FASTA_LINE_WIDTH = 60
# Cumulative probabilities of record kinds (valid records only).
KIND_WEIGHTS = [
    ("snv", 0.74),
    ("snv_multi", 0.05),
    ("indel_snv_multi", 0.02),
    ("mnv", 0.01),
    ("repeat_del", 0.04),
    ("repeat_ins", 0.04),
    ("random_del", 0.05),
    ("random_ins", 0.05),
]


@dataclass(frozen=True)
class Repeat:
    start: int  # 0-based
    unit: str
    copies: int


@dataclass(frozen=True)
class AltTruth:
    """What one ALT allele is after normalization, as known from how it was built."""

    kind: str  # SNV | MNV | INS | DEL
    transition: bool = False


@dataclass
class Record:
    contig_index: int
    pos: int
    line: str
    alts: list[AltTruth]
    reject_reason: str | None = None


def build_contig(rng: random.Random, length: int) -> tuple[str, list[Repeat]]:
    weights = [(1 - GC_FRACTION) / 2, GC_FRACTION / 2, GC_FRACTION / 2, (1 - GC_FRACTION) / 2]
    seq = bytearray("".join(rng.choices(BASES, weights, k=length)), "ascii")
    candidates: list[Repeat] = []
    # (unit length, one repeat per this many bases, min copies, max copies)
    for unit_len, spacing, min_copies, max_copies in ((1, 1500, 5, 10), (2, 3000, 3, 6)):
        count = max(length // spacing, 1)
        stride = length // count
        for i in range(count):
            if unit_len == 1:
                unit = rng.choice(BASES)
            else:
                first, second = rng.sample(BASES, 2)
                unit = first + second
            copies = rng.randint(min_copies, max_copies)
            span = unit_len * copies
            if stride <= span + 3:
                continue
            start = i * stride + rng.randrange(2, stride - span - 1)
            seq[start : start + span] = (unit * copies).encode()
            candidates.append(Repeat(start, unit, copies))
    sequence = seq.decode("ascii")
    # A later repeat may have overwritten part of an earlier one; keep only intact ones.
    repeats = [
        r
        for r in candidates
        if sequence[r.start : r.start + len(r.unit) * r.copies] == r.unit * r.copies
    ]
    return sequence, repeats


def write_reference(path: Path, contigs: dict[str, str]) -> None:
    fai: list[str] = []
    offset = 0
    with path.open("wb") as fasta:
        for name, seq in contigs.items():
            header = f">{name}\n".encode("ascii")
            fasta.write(header)
            offset += len(header)
            fai.append(f"{name}\t{len(seq)}\t{offset}\t{FASTA_LINE_WIDTH}\t{FASTA_LINE_WIDTH + 1}")
            for i in range(0, len(seq), FASTA_LINE_WIDTH):
                row = (seq[i : i + FASTA_LINE_WIDTH] + "\n").encode("ascii")
                fasta.write(row)
                offset += len(row)
    Path(str(path) + ".fai").write_text("\n".join(fai) + "\n", encoding="ascii", newline="\n")


class Generator:
    def __init__(self, rng: random.Random, contigs: dict[str, str], repeats: list[list[Repeat]]):
        self.rng = rng
        self.names = list(contigs)
        self.seqs = list(contigs.values())
        self.repeats = repeats
        self.titv = 2.0
        self.transitions = 0
        self.transversions = 0

    # -- alleles --------------------------------------------------------------------------

    def snv_alt(self, ref_base: str, exclude: set[str]) -> tuple[str, AltTruth]:
        """Pick an SNV allele, steering by running counts so Ti/Tv converges on the target.

        Independent random draws would drift: multi-allelic sites need distinct ALT bases
        and each REF has only one transition partner, which skews the realized ratio low.
        """
        want_transition = self.transitions < self.titv * self.transversions
        candidates = [TRANSITION[ref_base]] if want_transition else list(TRANSVERSIONS[ref_base])
        candidates = [b for b in candidates if b not in exclude] or [
            b for b in BASES if b != ref_base and b not in exclude
        ]
        alt = self.rng.choice(candidates)
        transition = alt == TRANSITION[ref_base]
        self.transitions += transition
        self.transversions += not transition
        return alt, AltTruth("SNV", transition)

    def random_bases(self, n: int) -> str:
        return "".join(self.rng.choice(BASES) for _ in range(n))

    # -- valid records --------------------------------------------------------------------

    def valid_site(self, kind: str, contig: int) -> tuple[int, str, list[str], list[AltTruth]]:
        rng, seq = self.rng, self.seqs[contig]
        repeats = self.repeats[contig]
        if kind.startswith("repeat") and not repeats:
            kind = kind.replace("repeat", "random")
        pos = rng.randint(2, len(seq) - 12)  # 1-based, room for the longest REF

        if kind == "snv":
            ref = seq[pos - 1]
            alt, truth = self.snv_alt(ref, set())
            return pos, ref, [alt], [truth]
        if kind == "snv_multi":
            ref = seq[pos - 1]
            alts, truths, used = [], [], set()
            for _ in range(rng.randint(2, 3)):
                alt, truth = self.snv_alt(ref, used)
                used.add(alt)
                alts.append(alt)
                truths.append(truth)
            return pos, ref, alts, truths
        if kind == "indel_snv_multi":
            # GT -> G (deletion of T) and GT -> GC (SNV T>C once trimmed).
            ref = seq[pos - 1 : pos + 1]
            new_base, truth = self.snv_alt(ref[1], set())
            return pos, ref, [ref[0], ref[0] + new_base], [AltTruth("DEL"), truth]
        if kind == "mnv":
            length = rng.randint(2, 3)
            ref = seq[pos - 1 : pos - 1 + length]
            # Every base differs, so trimming cannot shrink it into an SNV.
            alt = "".join(rng.choice([b for b in BASES if b != r]) for r in ref)
            return pos, ref, [alt], [AltTruth("MNV")]
        if kind in ("repeat_del", "repeat_ins"):
            repeat = rng.choice(repeats)
            size = len(repeat.unit)
            end = repeat.start + size * repeat.copies  # 0-based, exclusive
            if kind == "repeat_del":
                # Delete the LAST unit, anchored on the base just before it: valid but not
                # left-aligned, because that anchor lies inside the repeat.
                anchor = end - size - 1
                ref = seq[anchor : anchor + 1 + size]
                return anchor + 1, ref, [ref[0]], [AltTruth("DEL")]
            # Insert one more unit after the repeat's last base.
            ref = seq[end - 1]
            return end, ref, [ref + repeat.unit], [AltTruth("INS")]
        if kind == "random_del":
            ref = seq[pos - 1 : pos + rng.randint(1, 5)]
            return pos, ref, [ref[0]], [AltTruth("DEL")]
        # random_ins
        ref = seq[pos - 1]
        return pos, ref, [ref + self.random_bases(rng.randint(1, 5))], [AltTruth("INS")]

    def info_and_fields(self, alt_count: int) -> tuple[str, str, str]:
        rng = self.rng
        qual = f"{rng.uniform(15, 60):.1f}"
        flt = rng.choices(["PASS", "LowQual", "q10"], [0.93, 0.05, 0.02])[0]
        afs = [min(max(rng.betavariate(0.4, 3.0), 0.0005), 0.9995) for _ in range(alt_count)]
        info = "AF=" + ",".join(f"{af:.4f}" for af in afs) + f";DP={rng.randint(8, 120)}"
        return qual, flt, info

    def valid_record(self) -> Record:
        kind = self._draw_kind()
        contig = self.rng.randrange(len(self.names))
        pos, ref, alts, truths = self.valid_site(kind, contig)
        qual, flt, info = self.info_and_fields(len(alts))
        line = "\t".join([self.names[contig], str(pos), ".", ref, ",".join(alts), qual, flt, info])
        return Record(contig, pos, line, truths)

    def _draw_kind(self) -> str:
        roll, total = self.rng.random(), 0.0
        for kind, weight in KIND_WEIGHTS:
            total += weight
            if roll < total:
                return kind
        return KIND_WEIGHTS[-1][0]

    # -- invalid records ------------------------------------------------------------------

    def invalid_record(self, reason: str) -> Record:
        rng = self.rng
        contig = rng.randrange(len(self.names))
        seq = self.seqs[contig]
        name = self.names[contig]
        pos = rng.randint(2, len(seq) - 12)
        ref = seq[pos - 1]
        alt = rng.choice([b for b in BASES if b != ref])
        qual, flt, info = self.info_and_fields(1)

        def make(chrom: str = name, p: int = pos, r: str = ref, a: str = alt, q: str = qual) -> str:
            return "\t".join([chrom, str(p), ".", r, a, q, flt, info])

        if reason == "malformed_line":
            if rng.random() < 0.5:
                line = "\t".join(make().split("\t")[:5])  # truncated
            else:
                line = make(p=pos).replace(f"\t{pos}\t", f"\t{pos}x\t", 1)  # POS not an integer
        elif reason == "non_numeric_qual":
            line = make(q=rng.choice(["high", "NA", "5,5", "inf"]))
        elif reason == "unknown_contig":
            line = make(chrom=f"chrUn_{rng.randint(1, 999)}")
        elif reason == "pos_out_of_bounds":
            line = make(p=len(seq) + rng.randint(1, 5000))
        elif reason == "unsupported_symbolic":
            line = make(a=rng.choice(["<DEL>", "<DUP>", "<INS>", "*", f"{ref}[{name}:100["]))
        elif reason == "invalid_allele_chars":
            line = make(a=rng.choice("RYKMSWX"))
        elif reason == "ref_mismatch":
            line = make(r=rng.choice([b for b in BASES if b != ref]), a=alt)
        else:
            raise ValueError(reason)
        return Record(contig, pos, line, [], reject_reason=reason)


def generate(args: argparse.Namespace) -> dict[str, object]:
    rng = random.Random(args.seed)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    contigs: dict[str, str] = {}
    repeats: list[list[Repeat]] = []
    for i in range(args.contigs):
        sequence, contig_repeats = build_contig(rng, args.contig_length)
        contigs[f"chr{i + 1}"] = sequence
        repeats.append(contig_repeats)
    write_reference(out_dir / "ref.fa", contigs)

    generator = Generator(rng, contigs, repeats)
    generator.titv = args.titv
    invalid_count = round(args.variants * args.error_rate)
    records = [generator.valid_record() for _ in range(args.variants - invalid_count)]
    records += [
        generator.invalid_record(REJECT_REASONS[i % len(REJECT_REASONS)])
        for i in range(invalid_count)
    ]
    # Stable sort by genomic position, like a real caller output. The original index breaks
    # ties so the order does not depend on dict/set iteration.
    order = sorted(range(len(records)), key=lambda i: (records[i].contig_index, records[i].pos, i))
    records = [records[i] for i in order]

    header = [
        "##fileformat=VCFv4.2",
        f"##source=variant-etl/generate_data.py seed={args.seed}",
        *(f"##contig=<ID={name},length={len(seq)}>" for name, seq in contigs.items()),
        '##INFO=<ID=AF,Number=A,Type=Float,Description="Allele frequency">',
        '##INFO=<ID=DP,Number=1,Type=Integer,Description="Read depth">',
        '##FILTER=<ID=LowQual,Description="Low quality">',
        '##FILTER=<ID=q10,Description="Quality below 10">',
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO",
    ]
    rejected_lines: list[list[object]] = []
    by_type: Counter[str] = Counter()
    transitions = transversions = multi_allelic = 0
    reject_counts: Counter[str] = Counter()
    with (out_dir / "variants.vcf").open("w", encoding="ascii", newline="\n") as vcf:
        vcf.write("\n".join(header) + "\n")
        for line_no, record in enumerate(records, start=len(header) + 1):
            vcf.write(record.line + "\n")
            if record.reject_reason:
                rejected_lines.append([line_no, record.reject_reason])
                reject_counts[record.reject_reason] += 1
                continue
            multi_allelic += len(record.alts) > 1
            for alt in record.alts:
                by_type[alt.kind] += 1
                if alt.kind == "SNV":
                    transitions += alt.transition
                    transversions += not alt.transition

    truth: dict[str, object] = {
        "params": {
            "contigs": args.contigs,
            "contig_length": args.contig_length,
            "variants": args.variants,
            "titv": args.titv,
            "error_rate": args.error_rate,
            "seed": args.seed,
        },
        "contigs": {name: len(seq) for name, seq in contigs.items()},
        "records_total": len(records),
        "records_accepted": len(records) - invalid_count,
        "records_rejected": invalid_count,
        "rejected_by_reason": dict(sorted(reject_counts.items())),
        "multi_allelic_records": multi_allelic,
        "rows_total": sum(by_type.values()),
        "rows_by_type": {t: by_type.get(t, 0) for t in ("SNV", "MNV", "INS", "DEL")},
        "snv_transitions": transitions,
        "snv_transversions": transversions,
        "titv": round(transitions / transversions, 4) if transversions else None,
        "rejected_lines": rejected_lines,
    }
    (out_dir / "truth.json").write_text(
        json.dumps(truth, indent=1, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )
    return truth


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--contigs", type=int, default=3)
    parser.add_argument(
        "--contig-length", type=lambda s: int(s.replace("_", "")), default=5_000_000
    )
    parser.add_argument("--variants", type=lambda s: int(s.replace("_", "")), default=1_000_000)
    parser.add_argument("--titv", type=float, default=2.0, help="target Ti/Tv of SNVs")
    parser.add_argument(
        "--error-rate", type=float, default=0.005, help="fraction of lines made invalid"
    )
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out-dir", default="data")
    args = parser.parse_args(argv)
    if args.contig_length < 100:
        parser.error("--contig-length must be at least 100")
    if not 0 <= args.error_rate < 1:
        parser.error("--error-rate must be in [0, 1)")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    truth = generate(args)
    out_dir = Path(args.out_dir)
    digest = hashlib.sha256((out_dir / "variants.vcf").read_bytes()).hexdigest()[:12]
    print(
        f"wrote {out_dir}/ref.fa, variants.vcf ({truth['records_total']} records, "
        f"{truth['records_rejected']} invalid), truth.json  [vcf sha256 {digest}]"
    )
    print(f"rows by type: {truth['rows_by_type']}  Ti/Tv: {truth['titv']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
