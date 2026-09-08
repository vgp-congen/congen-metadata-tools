"""Tier 2 — sample identity.

The first of the two questions the validator exists to answer: are the
biosamples in the sheet exactly the biosamples in the VCF and the BAMs?

Every comparison is over **unique sample_id values**. 42 of the 79 sheets
repeat a sample_id because snpArcher merges multiple sequencing runs per
biosample, so comparing row counts would false-positive on more than half
the corpus.

S001-S003 deliberately never assert a direction. Either the sheet is
stale or the published data is; which one is not derivable from the
artifacts, and always needs a human. The findings state the disagreement
and the evidence, and stop there.
"""

from __future__ import annotations

from congen.core.findings import Finding, Location, Severity
from congen.tools.validate.context import (
    BAM_HEADERS,
    QC_SAMPLES,
    S3,
    S3_SHEET,
    SHEET,
    VCF_HEADER,
    Context,
)
from congen.tools.validate.registry import check

MAX_LISTED = 5


def _describe(names: set[str]) -> str:
    ordered = sorted(names)
    shown = ", ".join(ordered[:MAX_LISTED])
    more = f" (+{len(ordered) - MAX_LISTED} more)" if len(ordered) > MAX_LISTED else ""
    return f"{shown}{more}"


def _compare(
    context: Context,
    check_id: str,
    left_name: str,
    left: set[str],
    right_name: str,
    right: set[str],
    *,
    location: Location | None = None,
) -> list[Finding]:
    only_left = left - right
    only_right = right - left
    if not only_left and not only_right:
        return []

    parts = []
    if only_left:
        parts.append(f"only in {left_name}: {_describe(only_left)}")
    if only_right:
        parts.append(f"only in {right_name}: {_describe(only_right)}")
    return [
        Finding(
            id=check_id,
            severity=Severity.ERROR,
            subject=context.subject,
            message=(
                f"{left_name} ({len(left)}) and {right_name} ({len(right)}) "
                "describe different samples"
            ),
            detail="; ".join(parts) + " — needs human review; either side may be the stale one",
            location=location,
        )
    ]


@check(
    id="S001",
    tier="S",
    severity=Severity.ERROR,
    summary="sheet samples == VCF samples",
    needs=(SHEET, VCF_HEADER),
)
def sheet_matches_vcf(context: Context) -> list[Finding]:
    assert context.vcf_header
    return _compare(
        context,
        "S001",
        "sample_sheet.csv",
        context.sheet.unique_sample_ids,
        "raw.vcf.gz",
        set(context.vcf_header.samples),
        location=Location(context.sheet.path),
    )


@check(
    id="S002",
    tier="S",
    severity=Severity.ERROR,
    summary="sheet samples == published BAMs",
    needs=(SHEET, S3),
)
def sheet_matches_bams(context: Context) -> list[Finding]:
    """Only meaningful once the publication is complete.

    During a partial upload the BAM set is by definition not final, so
    comparing it to the sheet measures how far the upload got rather than
    whether the metadata is right. `grus-americana` — 57 samples in the
    sheet, 42 BAMs published, no VCF — is exactly that case, and this
    check reported it as a metadata mismatch until the status model
    existed to distinguish the two.
    """
    assert context.inventory
    if not context.inventory.bam_objects:
        return []  # G012 owns an empty bams/
    if not context.publication_is_complete:
        return []
    return _compare(
        context,
        "S002",
        "sample_sheet.csv",
        context.sheet.unique_sample_ids,
        "bams/",
        set(context.inventory.bam_objects),
        location=Location(context.sheet.path),
    )


@check(
    id="S003",
    tier="S",
    severity=Severity.ERROR,
    summary="VCF samples == published BAMs",
    needs=(VCF_HEADER, S3),
)
def vcf_matches_bams(context: Context) -> list[Finding]:
    assert context.vcf_header and context.inventory
    if not context.inventory.bam_objects:
        return []
    if not context.publication_is_complete:
        return []  # see S002: a partial upload's BAM set is not final
    return _compare(
        context,
        "S003",
        "raw.vcf.gz",
        set(context.vcf_header.samples),
        "bams/",
        set(context.inventory.bam_objects),
    )


@check(
    id="S004",
    tier="S",
    severity=Severity.ERROR,
    summary="each BAM's @RG SM matches its filename",
    needs=(BAM_HEADERS,),
)
def bam_read_groups_match_filenames(context: Context) -> list[Finding]:
    out: list[Finding] = []
    for name, header in sorted(context.bam_headers.items()):
        samples = header.sample_names
        if samples == {name}:
            continue
        out.append(
            Finding(
                id="S004",
                severity=Severity.ERROR,
                subject=context.subject,
                message=f"{name}.bam has @RG SM {sorted(samples) or ['<none>']}",
                detail="the read group should name the sample the file is named for",
            )
        )
    return out


@check(
    id="S005",
    tier="S",
    severity=Severity.WARN,
    summary="qc/individuals.samps.txt agrees with the VCF",
    needs=(QC_SAMPLES, VCF_HEADER),
)
def qc_samples_match_vcf(context: Context) -> list[Finding]:
    assert context.qc_samples is not None and context.vcf_header
    qc = set(context.qc_samples)
    vcf = set(context.vcf_header.samples)
    if qc == vcf:
        return []
    parts = []
    if qc - vcf:
        parts.append(f"only in QC: {_describe(qc - vcf)}")
    if vcf - qc:
        parts.append(f"only in VCF: {_describe(vcf - qc)}")
    return [
        Finding(
            id="S005",
            severity=Severity.WARN,
            subject=context.subject,
            message=f"qc/individuals.samps.txt ({len(qc)}) disagrees with the VCF ({len(vcf)})",
            detail="; ".join(parts),
        )
    ]


@check(
    id="S006",
    tier="S",
    severity=Severity.WARN,
    summary="the published sample sheet matches the repo copy",
    needs=(S3_SHEET, SHEET),
)
def published_sheet_matches_repo(context: Context) -> list[Finding]:
    """Catches upload drift — but note it cannot catch a shared error.

    For `anser-albifrons` the published copy is byte-identical to the repo
    sheet and both disagree with the VCF, which is exactly why this is a
    warning and S001 is the error.
    """
    assert context.s3_sheet is not None
    repo_rows = [(r.sample_id, r.input_type, r.input) for r in context.sheet.rows]
    published_rows = [(r.sample_id, r.input_type, r.input) for r in context.s3_sheet.rows]
    if repo_rows == published_rows:
        return []

    repo_ids = context.sheet.unique_sample_ids
    published_ids = context.s3_sheet.unique_sample_ids
    if repo_ids == published_ids:
        detail = "same samples, different rows (run-level differences only)"
    else:
        parts = []
        if repo_ids - published_ids:
            parts.append(f"only in repo: {_describe(repo_ids - published_ids)}")
        if published_ids - repo_ids:
            parts.append(f"only published: {_describe(published_ids - repo_ids)}")
        detail = "; ".join(parts)
    return [
        Finding(
            id="S006",
            severity=Severity.WARN,
            subject=context.subject,
            message="the published sample_sheet.csv differs from the repo copy",
            detail=detail,
            location=Location(context.sheet.path),
        )
    ]


@check(
    id="S007",
    tier="S",
    severity=Severity.WARN,
    summary="@RG LB agrees with library_id where the sheet declares it",
    needs=(BAM_HEADERS, SHEET),
)
def read_group_libraries_match_sheet(context: Context) -> list[Finding]:
    declared: dict[str, set[str]] = {}
    for row in context.sheet.rows:
        if row.library_id:
            declared.setdefault(row.sample_id, set()).add(row.library_id)
    if not declared:
        return []

    out: list[Finding] = []
    for name, header in sorted(context.bam_headers.items()):
        expected = declared.get(name)
        if not expected:
            continue
        observed = header.library_names
        if observed == expected:
            continue
        out.append(
            Finding(
                id="S007",
                severity=Severity.WARN,
                subject=context.subject,
                message=f"{name}.bam @RG LB {sorted(observed)} != sheet library_id {sorted(expected)}",
            )
        )
    return out
