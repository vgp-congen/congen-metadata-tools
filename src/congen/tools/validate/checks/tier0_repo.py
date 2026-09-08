"""Tier 0 — repo self-consistency. No network.

Most of the work is already done by the tolerant loaders, which return
issues rather than raising. These checks map loader issue codes onto
public check IDs and add the semantic rules the loaders deliberately
leave alone.
"""

from __future__ import annotations

from congen.core.findings import Finding, Location, Severity
from congen.core.metadata.models import BIOSAMPLE_RE
from congen.tools.validate.context import CONFIG, README, SHEET, Context
from congen.tools.validate.registry import check

#: Loader issue code -> (check id, severity, human prefix).
CONFIG_ISSUE_MAP = {
    "missing_file": ("R001", Severity.ERROR),
    "unreadable": ("R001", Severity.ERROR),
    "parse_error": ("R001", Severity.ERROR),
    "missing_key": ("R001", Severity.ERROR),
}

SHEET_ISSUE_MAP = {
    "missing_file": ("R010", Severity.ERROR),
    "unreadable": ("R010", Severity.ERROR),
    "parse_error": ("R010", Severity.ERROR),
    "missing_column": ("R010", Severity.ERROR),
    "short_row": ("R010", Severity.ERROR),
    "empty_sample_id": ("R011", Severity.ERROR),
    "unknown_input_type": ("R012", Severity.ERROR),
    "duplicate_run": ("R014", Severity.ERROR),
    # A blank row is tidied away by the loader; worth mentioning, not fixing.
    "blank_row": ("R010", Severity.INFO),
}


def _from_issues(context: Context, issues, mapping) -> list[Finding]:
    out: list[Finding] = []
    for issue in issues:
        mapped = mapping.get(issue.code)
        if not mapped:
            continue
        check_id, severity = mapped
        out.append(
            Finding(
                id=check_id,
                severity=severity,
                subject=context.subject,
                message=issue.message,
                detail=issue.code,
                location=Location(issue.path, issue.line) if issue.path else None,
            )
        )
    return out


@check(
    id="R001",
    tier="R",
    severity=Severity.ERROR,
    summary="config.yaml parses and has the required keys",
    needs=(),
)
def config_is_wellformed(context: Context) -> list[Finding]:
    return _from_issues(context, context.config.issues, CONFIG_ISSUE_MAP)


@check(
    id="R002",
    tier="R",
    severity=Severity.ERROR,
    summary="reference.source is an accession, URL or path",
    needs=(CONFIG,),
)
def reference_source_is_usable(context: Context) -> list[Finding]:
    reference = context.species.reference
    if reference.source is None:
        return []  # R001 already reported the absence
    if reference.is_accession or reference.is_url or reference.is_local_path:
        return []
    return [
        Finding(
            id="R002",
            severity=Severity.ERROR,
            subject=context.subject,
            message=f"reference.source {reference.source!r} is not an accession, URL or path",
            location=Location(context.config.path),
        )
    ]


@check(
    id="R003",
    tier="R",
    severity=Severity.WARN,
    summary="reference.name looks like a species name",
    needs=(CONFIG,),
)
def reference_name_is_a_name(context: Context) -> list[Finding]:
    """snpArcher stages the reference as ``results/reference/<name>.fa.gz``.

    An accession there means the staged file is named after the accession
    rather than the species, which is legal but makes the BAM provenance
    harder to read and breaks the `F006` cross-check.
    """
    reference = context.species.reference
    if not reference.name:
        return []
    from congen.core.metadata.models import ACCESSION_RE

    if not ACCESSION_RE.match(reference.name):
        return []
    return [
        Finding(
            id="R003",
            severity=Severity.WARN,
            subject=context.subject,
            message=f"reference.name is an accession ({reference.name}), not a species name",
            detail="snpArcher stages the reference as results/reference/<name>.fa.gz",
            location=Location(context.config.path),
        )
    ]


@check(
    id="R010",
    tier="R",
    severity=Severity.ERROR,
    summary="sample_sheet.csv parses with the required columns",
    needs=(),
)
def sheet_is_wellformed(context: Context) -> list[Finding]:
    """Structural problems the sheet loader found.

    Covers a missing or unreadable file, absent required columns, rows
    with too few fields, an empty ``sample_id``, an unrecognized
    ``input_type``, and a genuinely repeated ``(sample_id, input)`` pair.
    A blank row is reported here too, but only as a note.
    """
    return _from_issues(context, context.sheet.issues, SHEET_ISSUE_MAP)


@check(
    id="R013",
    tier="R",
    severity=Severity.ERROR,
    summary="srr inputs are SRA run or experiment accessions",
    needs=(SHEET,),
)
def srr_inputs_are_sra_accessions(context: Context) -> list[Finding]:
    out: list[Finding] = []
    for row in context.sheet.rows:
        if row.input_type != "srr" or row.is_sra_accession:
            continue
        out.append(
            Finding(
                id="R013",
                severity=Severity.ERROR,
                subject=context.subject,
                message=f"{row.sample_id}: input {row.input!r} is not an SRA accession",
                detail="expected a run (SRR/ERR/DRR) or experiment (SRX/ERX/DRX) accession",
                location=Location(context.sheet.path, row.line),
            )
        )
    return out


@check(
    id="R015",
    tier="R",
    severity=Severity.WARN,
    summary="inputs are not local filesystem paths",
    needs=(SHEET,),
)
def inputs_are_not_local_paths(context: Context) -> list[Finding]:
    """A path on someone's cluster cannot be re-fetched by anyone else.

    The run is not reproducible from the sheet alone: whoever repeats it
    needs the original filesystem. Usually a sign that reads were
    recovered locally rather than pulled from SRA.
    """
    out: list[Finding] = []
    for row in context.sheet.rows:
        if not row.is_local_path:
            continue
        out.append(
            Finding(
                id="R015",
                severity=Severity.WARN,
                subject=context.subject,
                message=f"{row.sample_id}: input is a local path, so the run is not reproducible",
                detail=row.input,
                location=Location(context.sheet.path, row.line),
            )
        )
    return out


@check(
    id="R016",
    tier="R",
    severity=Severity.WARN,
    summary="sample_id values are BioSample accessions",
    needs=(SHEET,),
)
def sample_ids_are_biosamples(context: Context) -> list[Finding]:
    offenders = sorted(
        {row.sample_id for row in context.sheet.rows if not BIOSAMPLE_RE.match(row.sample_id)}
    )
    if not offenders:
        return []
    shown = ", ".join(offenders[:5])
    more = f" (+{len(offenders) - 5} more)" if len(offenders) > 5 else ""
    return [
        Finding(
            id="R016",
            severity=Severity.WARN,
            subject=context.subject,
            message=f"{len(offenders)} sample_id(s) are not BioSample accessions",
            detail=f"{shown}{more}",
            location=Location(context.sheet.path),
        )
    ]


@check(
    id="R020",
    tier="R",
    severity=Severity.WARN,
    summary="README.txt accession matches the config",
    needs=(README, CONFIG),
)
def readme_accession_matches(context: Context) -> list[Finding]:
    readme = context.readme
    assert readme is not None
    declared = context.species.reference.source
    if not readme.accession or not declared or readme.accession == declared:
        return []
    return [
        Finding(
            id="R020",
            severity=Severity.WARN,
            subject=context.subject,
            message=(
                f"README.txt says {readme.accession}, config says {declared}"
            ),
            location=Location(readme.path),
        )
    ]
