"""Variant normalization equivalent to `bcftools norm -m -any -f ref.fa`.

Each biallelic allele is trimmed and left-aligned following Tan, Abecasis & Kang (2015),
"Unified representation of genetic variants", Bioinformatics 31(13):2202-2204.

Why it matters: the same deletion in a repeat can be written at any position inside the
repeat. Without a canonical form, two callers (or two samples) describing the same event
produce different rows, and queries/joins/dedup on (chrom, pos, ref, alt) silently miss.
"""

from variant_etl.pipeline.records import RecordRejected, RejectReason, VariantType
from variant_etl.pipeline.reference import Reference

TRANSITIONS = frozenset({("A", "G"), ("G", "A"), ("C", "T"), ("T", "C")})


def normalize_allele(
    reference: Reference, chrom: str, pos: int, ref: str, alt: str
) -> tuple[int, str, str]:
    """Return the trimmed, left-aligned (pos, ref, alt). `pos` is 1-based.

    The loop repeats two steps until neither applies:
      1. while both alleles end in the same base, drop it;
      2. if that leaves an allele empty, prepend the preceding reference base to both
         alleles (this is what shifts an indel one step to the left).
    A final pass drops a shared leading base so MNVs and complex events are minimal, but
    never below one base per allele (VCF forbids empty alleles).
    """
    if len(ref) == 1 and len(alt) == 1:
        return pos, ref, alt  # SNV (or N): nothing to trim and nothing to align

    while True:
        changed = False
        if ref and alt and ref[-1] == alt[-1]:
            ref, alt = ref[:-1], alt[:-1]
            changed = True
        if not ref or not alt:
            if pos > 1:
                pos -= 1
                base = reference.fetch(chrom, pos - 1, pos)
                ref, alt = base + ref, base + alt
                changed = True
            else:
                # An indel at the first base of a contig has no base to its left to use as
                # anchor; VCF then anchors it on the base to the right instead.
                base = reference.fetch(chrom, pos - 1 + len(ref), pos + len(ref))
                if not base:
                    raise RecordRejected(RejectReason.MALFORMED_LINE)
                return pos, ref + base, alt + base
        if not changed:
            break

    while len(ref) >= 2 and len(alt) >= 2 and ref[0] == alt[0]:
        ref, alt = ref[1:], alt[1:]
        pos += 1
    return pos, ref, alt


def classify(ref: str, alt: str) -> VariantType:
    """Variant class of a normalized biallelic allele pair."""
    if len(ref) == len(alt):
        return VariantType.SNV if len(ref) == 1 else VariantType.MNV
    # Strip the shared anchor base(s) and see what is left of each allele.
    start = 0
    while start < min(len(ref), len(alt)) and ref[start] == alt[start]:
        start += 1
    end = 0
    while end < min(len(ref), len(alt)) - start and ref[-1 - end] == alt[-1 - end]:
        end += 1
    ref_core = ref[start : len(ref) - end]
    alt_core = alt[start : len(alt) - end]
    if not ref_core:
        return VariantType.INS
    if not alt_core:
        return VariantType.DEL
    return VariantType.COMPLEX


def is_transition(ref: str, alt: str) -> bool:
    """True only for A<->G and C<->T single-base substitutions."""
    return (ref, alt) in TRANSITIONS
