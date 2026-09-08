"""Tier 5 — the sheet against NCBI SRA. Opt-in, `--check-sra`.

Everything so far compares the repo against GenomeArk. These checks ask a
different question: are the accessions in the sample sheet the ones the
submitter actually deposited?

Opt-in because it is the only tier whose cost scales with sample count
rather than species count, and because a routine validation should not
fail when a third-party service is unavailable.

`R017` lives here rather than in tier 0. Offline it could only report the
*form* of an accession and guess at the consequence; with `runinfo` the
consequence is knowable, so it reports what is actually true.
"""

from __future__ import annotations

from congen.core.findings import Finding, Location, Severity
from congen.tools.validate.context import README, SHEET, SRA, Context
from congen.tools.validate.registry import check

MAX_LISTED = 5


def _describe(names) -> str:
    ordered = sorted(names)
    shown = ", ".join(ordered[:MAX_LISTED])
    more = f" (+{len(ordered) - MAX_LISTED} more)" if len(ordered) > MAX_LISTED else ""
    return f"{shown}{more}"


def _sra_rows(context: Context):
    """Sheet rows whose input is an SRA accession."""
    return [row for row in context.sheet.rows if row.is_sra_accession]


@check(
    id="E001",
    tier="E",
    severity=Severity.ERROR,
    summary="each srr input belongs to the biosample the sheet claims",
    needs=(SHEET, SRA),
)
def inputs_belong_to_their_biosample(context: Context) -> list[Finding]:
    assert context.sra
    out: list[Finding] = []
    unresolved: list[str] = []

    for row in _sra_rows(context):
        runs = context.sra.runs_for(row.input)
        if not runs:
            unresolved.append(row.input)
            continue
        biosamples = {r.biosample for r in runs if r.biosample}
        if biosamples == {row.sample_id}:
            continue
        out.append(
            Finding(
                id="E001",
                severity=Severity.ERROR,
                subject=context.subject,
                message=(
                    f"{row.input} belongs to "
                    f"{_describe(biosamples) or '<no biosample>'}, "
                    f"but the sheet lists it under {row.sample_id}"
                ),
                location=Location(context.sheet.path, row.line),
            )
        )

    if unresolved:
        out.append(
            Finding(
                id="E001",
                severity=Severity.ERROR,
                subject=context.subject,
                message=f"{len(unresolved)} input accession(s) are unknown to SRA",
                detail=_describe(unresolved),
                location=Location(context.sheet.path),
            )
        )
    return out


@check(
    id="E002",
    tier="E",
    severity=Severity.WARN,
    summary="every run's bioproject is listed in README.txt",
    needs=(SHEET, SRA, README),
)
def run_bioprojects_are_documented(context: Context) -> list[Finding]:
    assert context.sra and context.readme
    documented = set(context.readme.bioprojects)
    if not documented:
        return []  # nothing claimed, so nothing to contradict

    observed = context.sra.bioprojects
    undocumented = observed - documented
    if not undocumented:
        return []
    return [
        Finding(
            id="E002",
            severity=Severity.WARN,
            subject=context.subject,
            message=(
                f"{len(undocumented)} bioproject(s) contribute runs but are not in "
                "README.txt"
            ),
            detail=f"{_describe(undocumented)}; README lists {_describe(documented)}",
            location=Location(context.readme.path),
        )
    ]


@check(
    id="E003",
    tier="E",
    severity=Severity.WARN,
    summary="every README.txt bioproject contributes runs",
    needs=(SHEET, SRA, README),
)
def documented_bioprojects_contribute(context: Context) -> list[Finding]:
    assert context.sra and context.readme
    documented = set(context.readme.bioprojects)
    if not documented:
        return []

    observed = context.sra.bioprojects
    idle = documented - observed
    if not idle:
        return []
    return [
        Finding(
            id="E003",
            severity=Severity.WARN,
            subject=context.subject,
            message=(
                f"README.txt cites {len(idle)} bioproject(s) that contribute no runs "
                "to the sheet"
            ),
            detail=_describe(idle),
            location=Location(context.readme.path),
        )
    ]


@check(
    id="R017",
    tier="R",
    severity=Severity.ERROR,
    summary="srr inputs identify exactly one run each",
    needs=(SHEET, SRA),
)
def experiment_inputs_are_unambiguous(context: Context) -> list[Finding]:
    """An experiment holding several runs does not say which reads were used.

    Error when it expands to more than one run — that is a genuine
    ambiguity about what was analysed. Informational when it expands to
    exactly one, which is the case for all 42 experiment accessions in
    `birds/anser-anser`: worth recording as an imprecise way to name a
    run, not worth blocking on.
    """
    assert context.sra
    ambiguous: list[tuple[str, str, tuple[str, ...]]] = []
    single: list[str] = []

    for row in context.sheet.rows:
        if not row.is_experiment_accession:
            continue
        runs = tuple(r.run for r in context.sra.runs_for(row.input))
        if len(runs) > 1:
            ambiguous.append((row.sample_id, row.input, runs))
        elif len(runs) == 1:
            single.append(row.input)

    out: list[Finding] = []
    for sample_id, experiment, runs in ambiguous:
        out.append(
            Finding(
                id="R017",
                severity=Severity.ERROR,
                subject=context.subject,
                message=(
                    f"{sample_id}: experiment {experiment} contains {len(runs)} runs, "
                    "so the sheet does not identify which reads were used"
                ),
                detail=f"runs: {_describe(runs)}",
                location=Location(context.sheet.path, context.sheet.line_of(sample_id)),
            )
        )

    if single:
        out.append(
            Finding(
                id="R017",
                severity=Severity.INFO,
                subject=context.subject,
                message=(
                    f"{len(single)} input(s) name an SRA experiment rather than a run, "
                    "each containing exactly one run"
                ),
                detail=_describe(single),
                location=Location(context.sheet.path),
            )
        )
    return out
