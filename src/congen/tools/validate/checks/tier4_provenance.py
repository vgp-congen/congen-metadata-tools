"""Tier 4 — config against recorded provenance.

The VCF header preserves the actual GATK invocations, which makes
`config.yaml` checkable against what really ran. Everything here is a
**warning**: the config is a record of intent that may legitimately have
been edited after a run, so a disagreement is worth surfacing but is not
a defect in the data.

Across the corpus these agree everywhere — all 67 published VCFs record
GATK 4.6.2.0 with `--sample-ploidy 2` and `--heterozygosity 0.005`,
matching every config. The detectors are therefore covered by tests
rather than by real findings.

`P010` used to record the pipeline versions here. It was withdrawn: a
validation finding has to name something that can be *fixed*, and a
version stamp is inventory with no remedy. That belongs to the readme
generator, which is where it came from.
"""

from __future__ import annotations

from congen.core.findings import Finding, Location, Severity
from congen.tools.validate.context import CONFIG, VCF_HEADER, Context
from congen.tools.validate.registry import check

#: Relative tolerance for comparing a float the header rendered as text
#: with the one the config declares. Guards against 0.005 vs 5e-3 style
#: differences without letting a real change through.
FLOAT_TOLERANCE = 1e-9


def _as_float(value) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _compare_numeric(
    context: Context,
    check_id: str,
    label: str,
    flag: str,
    declared,
    *,
    integral: bool = False,
) -> list[Finding]:
    """Compare a config value with the matching recorded GATK argument."""
    assert context.vcf_header
    recorded_text = context.vcf_header.gatk_argument(flag)
    if recorded_text is None or declared is None:
        return []  # nothing recorded, or nothing declared: no claim to make

    recorded = _as_float(recorded_text)
    expected = _as_float(declared)
    if recorded is None or expected is None:
        return [
            Finding(
                id=check_id,
                severity=Severity.WARN,
                subject=context.subject,
                message=(
                    f"{label}: cannot compare config {declared!r} with recorded "
                    f"--{flag} {recorded_text!r}"
                ),
                location=Location(context.config.path),
            )
        ]

    if integral:
        matches = int(recorded) == int(expected)
    else:
        scale = max(abs(recorded), abs(expected), 1.0)
        matches = abs(recorded - expected) <= FLOAT_TOLERANCE * scale
    if matches:
        return []

    return [
        Finding(
            id=check_id,
            severity=Severity.WARN,
            subject=context.subject,
            message=f"{label}: config says {declared}, the run recorded --{flag} {recorded_text}",
            detail="config.yaml records intent and may have been edited after the run",
            location=Location(context.config.path),
        )
    ]


@check(
    id="P001",
    tier="P",
    severity=Severity.WARN,
    summary="variant_calling.ploidy matches the recorded --sample-ploidy",
    needs=(VCF_HEADER, CONFIG),
)
def ploidy_matches_the_run(context: Context) -> list[Finding]:
    return _compare_numeric(
        context,
        "P001",
        "ploidy",
        "sample-ploidy",
        context.config.ploidy,
        integral=True,
    )


@check(
    id="P002",
    tier="P",
    severity=Severity.WARN,
    summary="gatk.het_prior matches the recorded --heterozygosity",
    needs=(VCF_HEADER, CONFIG),
)
def het_prior_matches_the_run(context: Context) -> list[Finding]:
    return _compare_numeric(
        context,
        "P002",
        "heterozygosity prior",
        "heterozygosity",
        context.config.het_prior,
    )


@check(
    id="P003",
    tier="P",
    severity=Severity.WARN,
    summary="variant_calling.tool matches the caller recorded in the VCF",
    needs=(VCF_HEADER, CONFIG),
)
def caller_matches_the_run(context: Context) -> list[Finding]:
    """Only reports when a *different* caller is positively identified.

    Absence of evidence is not evidence: a header this code does not
    recognize produces nothing rather than a guess. Note also that every
    GATK-called VCF here carries `##bcftools_concatCommand`, because
    snpArcher merges its per-interval VCFs with bcftools — which is why
    caller detection looks for `bcftools_call` specifically and not for
    bcftools in general.
    """
    assert context.vcf_header
    declared = context.config.caller
    if not declared:
        return []
    declared = str(declared).strip().lower()

    recorded = context.vcf_header.callers()
    if not recorded:
        return []

    # parabricks emits GATK-compatible headers, so GATK evidence is
    # consistent with a parabricks config rather than contradicting it.
    if declared == "parabricks" and recorded == {"gatk"}:
        return []
    if declared in recorded:
        return []

    return [
        Finding(
            id="P003",
            severity=Severity.WARN,
            subject=context.subject,
            message=(
                f"config declares tool {declared!r}, but the VCF header records "
                f"{', '.join(sorted(recorded))}"
            ),
            detail=f"recorded invocations: {', '.join(context.vcf_header.gatk_tool_ids()) or 'none'}",
            location=Location(context.config.path),
        )
    ]
