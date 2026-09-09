"""Tier 3a — reference identity.

The second of the two questions the validator exists to answer: was the
published data actually produced against the reference config.yaml
declares? Tier 3b asks whether that reference is the *right* one; this
tier asks whether the data matches it at all.

The assembly report gives every sequence with its GenBank and RefSeq
accessions, its length and its role, so the comparison is concrete.

The name comparison is deliberately **asymmetric**, because the two
directions mean different things. A contig in the VCF that the assembly
does not contain means the reference is not what the config claims — an
error. An assembly sequence absent from the VCF is expected in principle,
since interval logic could legitimately drop small contigs — so it is a
threshold warning. Measured across all 67 species with a published VCF,
the two sets are exactly equal, so nothing is being filtered in practice.
"""

from __future__ import annotations

from congen.core.findings import Finding, Location, Severity
from congen.core.remote.ncbi import SCHEMES
from congen.tools.validate.context import (
    ASSEMBLY_REPORT,
    BAM_HEADERS,
    CONFIG,
    CONTIG_MAP,
    VCF_HEADER,
    Context,
)
from congen.tools.validate.registry import check

MAX_LISTED = 5


def _describe(names) -> str:
    ordered = sorted(names)
    shown = ", ".join(ordered[:MAX_LISTED])
    more = f" (+{len(ordered) - MAX_LISTED} more)" if len(ordered) > MAX_LISTED else ""
    return f"{shown}{more}"


def _observed_contigs(context: Context) -> dict[str, int | None]:
    assert context.vcf_header
    return dict(context.vcf_header.contigs)


def _scheme(context: Context) -> str:
    assert context.assembly_report
    return context.assembly_report.best_scheme(set(_observed_contigs(context)))


def _contig_set_is_coherent(context: Context) -> bool:
    """True when the VCF's contigs all resolve under one scheme.

    F009 and F011 measure what the assembly has that the VCF lacks. That
    number is only meaningful if the VCF's own names are accounted for:
    against the wrong assembly it reads "100% of bases missing", and with
    mixed naming it reads whatever fraction happened to use the other
    scheme. Both are artifacts of the problem F001 or F003 already
    reported, so they stay quiet and leave one root cause standing.
    """
    report = context.assembly_report
    assert report
    known = set(report.lengths(_scheme(context)))
    return all(name in known for name in _observed_contigs(context))


@check(
    id="F001",
    tier="F",
    severity=Severity.ERROR,
    summary="every VCF contig exists in the declared assembly",
    needs=(VCF_HEADER, ASSEMBLY_REPORT),
)
def vcf_contigs_exist_in_assembly(context: Context) -> list[Finding]:
    report = context.assembly_report
    assert report
    observed = _observed_contigs(context)
    scheme = _scheme(context)
    known = report.lengths(scheme)

    # A name that belongs to a different naming scheme is F003's problem,
    # not evidence of the wrong assembly.
    unknown = [
        name for name in observed if name not in known and report.scheme_of(name) is None
    ]
    if not unknown:
        return []
    return [
        Finding(
            id="F001",
            severity=Severity.ERROR,
            subject=context.subject,
            message=(
                f"{len(unknown)} of {len(observed)} VCF contig(s) do not exist in "
                f"{report.accession} ({report.assembly_name})"
            ),
            detail=f"{_describe(unknown)} — the reference is not what the config declares",
            location=Location(context.config.path),
        )
    ]


@check(
    id="F002",
    tier="F",
    severity=Severity.ERROR,
    summary="VCF contig lengths match the assembly",
    needs=(VCF_HEADER, ASSEMBLY_REPORT),
)
def vcf_contig_lengths_match(context: Context) -> list[Finding]:
    """The real wrong-genome detector.

    A different assembly of the same species reuses naming conventions
    but not sequence lengths.
    """
    report = context.assembly_report
    assert report
    observed = _observed_contigs(context)
    known = report.lengths(_scheme(context))

    mismatched = [
        (name, length, known[name])
        for name, length in observed.items()
        if name in known and length is not None and known[name] is not None
        and length != known[name]
    ]
    if not mismatched:
        return []
    examples = "; ".join(
        f"{name}: VCF {vcf}, assembly {asm}" for name, vcf, asm in mismatched[:3]
    )
    return [
        Finding(
            id="F002",
            severity=Severity.ERROR,
            subject=context.subject,
            message=(
                f"{len(mismatched)} contig(s) have a different length in the VCF than "
                f"in {report.accession}"
            ),
            detail=f"{examples} — a different assembly of the same species",
            location=Location(context.config.path),
        )
    ]


@check(
    id="F003",
    tier="F",
    severity=Severity.ERROR,
    summary="contig names use one consistent naming scheme",
    needs=(VCF_HEADER, ASSEMBLY_REPORT),
)
def contig_naming_is_consistent(context: Context) -> list[Finding]:
    report = context.assembly_report
    assert report
    observed = set(_observed_contigs(context))
    chosen = _scheme(context)

    strays: dict[str, list[str]] = {}
    for name in observed:
        if name in report.lengths(chosen):
            continue
        other = report.scheme_of(name)
        if other:
            strays.setdefault(other, []).append(name)
    if not strays:
        return []
    parts = ", ".join(f"{len(names)} {scheme}" for scheme, names in sorted(strays.items()))
    return [
        Finding(
            id="F003",
            severity=Severity.ERROR,
            subject=context.subject,
            message=(
                f"contig names mix naming schemes: mostly {chosen}, plus {parts}"
            ),
            detail=_describe([n for names in strays.values() for n in names]),
        )
    ]


@check(
    id="F004",
    tier="F",
    severity=Severity.ERROR,
    summary="BAM @SQ matches the VCF contigs",
    needs=(VCF_HEADER, BAM_HEADERS),
)
def bam_sequences_match_vcf(context: Context) -> list[Finding]:
    assert context.vcf_header
    vcf = context.vcf_header.contigs
    out: list[Finding] = []
    for name, header in sorted(context.bam_headers.items()):
        bam = header.reference_lengths
        if bam == {k: v for k, v in vcf.items() if k in bam} and set(bam) == set(vcf):
            continue
        only_bam = set(bam) - set(vcf)
        only_vcf = set(vcf) - set(bam)
        bad_length = [
            k for k in set(bam) & set(vcf) if vcf[k] is not None and bam[k] != vcf[k]
        ]
        parts = []
        if only_bam:
            parts.append(f"only in BAM: {_describe(only_bam)}")
        if only_vcf:
            parts.append(f"only in VCF: {_describe(only_vcf)}")
        if bad_length:
            parts.append(f"different lengths: {_describe(bad_length)}")
        out.append(
            Finding(
                id="F004",
                severity=Severity.ERROR,
                subject=context.subject,
                message=(
                    f"{name}.bam @SQ ({len(bam)}) does not match the VCF contigs "
                    f"({len(vcf)})"
                ),
                detail="; ".join(parts),
            )
        )
    return out


@check(
    id="F005",
    tier="F",
    severity=Severity.ERROR,
    summary="all BAMs share one @SQ list",
    needs=(BAM_HEADERS,),
)
def bams_agree_with_each_other(context: Context) -> list[Finding]:
    """Note the sample size: by default only a few BAMs are read.

    A clean result therefore means "the BAMs examined agree", which the
    finding says explicitly rather than implying it covered all of them.
    """
    if len(context.bam_headers) < 2:
        return []
    reference = None
    disagreeing: list[str] = []
    for name, header in sorted(context.bam_headers.items()):
        sequences = header.reference_lengths
        if reference is None:
            reference = sequences
            continue
        if sequences != reference:
            disagreeing.append(name)
    if not disagreeing:
        return []
    scope = "all" if context.bam_headers_complete else f"{len(context.bam_headers)} sampled"
    return [
        Finding(
            id="F005",
            severity=Severity.ERROR,
            subject=context.subject,
            message=(
                f"{len(disagreeing)} of {scope} BAM(s) have a different @SQ list "
                "from the others"
            ),
            detail=f"{_describe(disagreeing)} — not all BAMs used the same reference",
        )
    ]


@check(
    id="F006",
    tier="F",
    severity=Severity.WARN,
    summary="the BAM's bwa command line names reference.name",
    needs=(BAM_HEADERS, CONFIG),
)
def bwa_reference_matches_config(context: Context) -> list[Finding]:
    declared = context.species.reference.name
    if not declared:
        return []
    out: list[Finding] = []
    for name, header in sorted(context.bam_headers.items()):
        observed = header.bwa_reference_name
        if observed is None or observed == declared:
            continue
        out.append(
            Finding(
                id="F006",
                severity=Severity.WARN,
                subject=context.subject,
                message=(
                    f"{name}.bam was aligned against {observed!r}, but "
                    f"reference.name is {declared!r}"
                ),
                detail="snpArcher stages the reference as results/reference/<name>.fa.gz",
                location=Location(context.config.path),
            )
        )
    return out


@check(
    id="F008",
    tier="F",
    severity=Severity.WARN,
    summary="qc/contig_map.tsv agrees with the VCF contigs",
    needs=(CONTIG_MAP, VCF_HEADER),
)
def contig_map_matches_vcf(context: Context) -> list[Finding]:
    assert context.contig_map and context.vcf_header
    mapped = set(context.contig_map.original_contigs)
    vcf = set(context.vcf_header.contigs)
    # The map covers the contigs that reached plink, which is a subset of
    # the VCF; only names it invents are suspicious.
    unexpected = mapped - vcf
    if not unexpected:
        return []
    return [
        Finding(
            id="F008",
            severity=Severity.WARN,
            subject=context.subject,
            message=(
                f"qc/contig_map.tsv names {len(unexpected)} contig(s) that are not in "
                "the VCF"
            ),
            detail=_describe(unexpected),
        )
    ]


@check(
    id="F009",
    tier="F",
    severity=Severity.WARN,
    summary="assembly sequences absent from the VCF",
    needs=(VCF_HEADER, ASSEMBLY_REPORT),
)
def assembly_sequences_present_in_vcf(context: Context) -> list[Finding]:
    """The permissive direction of the name comparison.

    Warns above the threshold, informs below it, so the ordinary case of a
    few dropped short scaffolds stays quiet without hiding a reference
    that is missing a real fraction of the genome.
    """
    if not _contig_set_is_coherent(context):
        return []

    report = context.assembly_report
    assert report
    observed = set(_observed_contigs(context))
    known = report.lengths(_scheme(context))
    missing = {name: length for name, length in known.items() if name not in observed}
    if not missing:
        return []

    total = report.total_length(_scheme(context))
    missing_bases = sum(length or 0 for length in missing.values())
    fraction = (missing_bases / total * 100) if total else 0.0
    threshold = context.missing_contig_threshold

    severity = Severity.WARN if fraction > threshold else Severity.INFO
    biggest = sorted(missing.items(), key=lambda kv: -(kv[1] or 0))[:3]
    detail = ", ".join(f"{name} ({(length or 0) / 1e6:.1f} Mb)" for name, length in biggest)
    return [
        Finding(
            id="F009",
            severity=severity,
            subject=context.subject,
            message=(
                f"{len(missing)} assembly sequence(s) absent from the VCF, "
                f"{fraction:.2f}% of assembly bases"
            ),
            detail=f"largest: {detail}",
        )
    ]


@check(
    id="F011",
    tier="F",
    severity=Severity.WARN,
    summary="no whole assembled molecule is absent from the VCF",
    needs=(VCF_HEADER, ASSEMBLY_REPORT),
)
def assembled_molecules_are_present(context: Context) -> list[Finding]:
    """More sensitive than F009, at any fraction.

    A missing unplaced scaffold is unremarkable; a missing chromosome is
    not, however small it is.
    """
    if not _contig_set_is_coherent(context):
        return []

    report = context.assembly_report
    assert report
    observed = set(_observed_contigs(context))
    scheme = _scheme(context)
    roles = report.roles(scheme)
    lengths = report.lengths(scheme)

    missing = [
        name
        for name, role in roles.items()
        if name not in observed and role == "assembled-molecule"
    ]
    if not missing:
        return []
    detail = ", ".join(
        f"{name} ({(lengths.get(name) or 0) / 1e6:.1f} Mb)"
        for name in sorted(missing, key=lambda n: -(lengths.get(n) or 0))[:MAX_LISTED]
    )
    return [
        Finding(
            id="F011",
            severity=Severity.WARN,
            subject=context.subject,
            message=f"{len(missing)} assembled molecule(s) absent from the VCF",
            detail=detail,
        )
    ]
