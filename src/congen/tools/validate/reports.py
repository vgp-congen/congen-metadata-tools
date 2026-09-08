"""Writing validation reports into congen-metadata.

The only part of the validator that writes. Everything here is reachable
only through an explicit flag; a bare `congen validate` never touches the
metadata repo.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from congen.core.findings import Finding, Report, Severity
from congen.core.metadata.discovery import SpeciesRepo
from congen.core.metadata.models import SpeciesMetadata
from congen.core.metadata.writers import write_if_changed
from congen.core.report.markdown import (
    CHECKS_FILE,
    render_checks_reference,
    render_corpus_report,
    render_species_report,
)
from congen.core.remote.genomeark import GenomeArk
from congen.core.validation_record import (
    REPORT_JSON,
    ReportState,
    REPORT_MARKDOWN,
    Staleness,
    ValidationRecord,
    assess,
    assess_data,
    build_record,
    catalog_digest,
    load_record,
    mark_stale,
    utc_now,
)

CORPUS_MARKDOWN = "VALIDATION.md"
CORPUS_JSON = "validation.json"


def tool_version() -> str:
    try:
        return version("congen-metadata-tools")
    except PackageNotFoundError:  # pragma: no cover - editable installs always resolve
        return "0.0.0+unknown"


def species_display_name(species: SpeciesMetadata) -> str:
    """A readable name for the report heading."""
    name = species.reference.name
    if name and not name.startswith(("GCA_", "GCF_")):
        return name.replace("_", " ")
    return species.slug.replace("-", " ").title()


@dataclass
class WriteResult:
    subject: str
    markdown_changed: bool
    json_changed: bool

    @property
    def changed(self) -> bool:
        return self.markdown_changed or self.json_changed


def checks_href(species: SpeciesMetadata, root: Path | None) -> str | None:
    """Relative link from a species directory up to the corpus CHECKS.md."""
    if root is None:
        return None
    try:
        depth = len(species.path.relative_to(root).parts)
    except ValueError:
        return None
    return "../" * depth + CHECKS_FILE


def write_species_report(
    species: SpeciesMetadata, record: ValidationRecord, *, root: Path | None = None
) -> WriteResult:
    markdown = render_species_report(
        record, species_display_name(species), checks_href=checks_href(species, root)
    )
    return WriteResult(
        subject=species.key,
        markdown_changed=write_if_changed(species.path / REPORT_MARKDOWN, markdown),
        json_changed=record.write_json(species.path / REPORT_JSON),
    )


def record_for(
    species: SpeciesMetadata,
    findings: list[Finding],
    context,
    *,
    checks_run: list[str],
    checks_available: int,
    catalog: str,
    root: Path | None = None,
) -> ValidationRecord:
    return build_record(
        subject=species.key,
        findings=findings,
        status=context.upload_status,
        species_dir=species.path,
        inventory=context.inventory,
        accession=context.resolved_accession or context.declared_accession,
        checks_run=checks_run,
        checks_available=checks_available,
        catalog=catalog,
        tool_version=tool_version(),
        root=root,
    )


def existing_record(species: SpeciesMetadata) -> ValidationRecord | None:
    return load_record(species.path / REPORT_JSON)


def stale_species(
    repo: SpeciesRepo,
    genomeark: GenomeArk,
    *,
    catalog: str,
    check_published: bool = True,
) -> list[tuple[SpeciesMetadata, Staleness]]:
    """Every species needing revalidation, and why.

    One bucket listing covers the whole corpus, so noticing that data has
    appeared for a previously unpublished species costs a single request.
    """
    published: set[str] | None = None
    if check_published:
        try:
            published = set(genomeark.accessions())
        except Exception:  # noqa: BLE001 - fall back to local-only staleness
            published = None

    out: list[tuple[SpeciesMetadata, Staleness]] = []
    for species in repo.iter_species():
        verdict = assess(
            existing_record(species),
            species_dir=species.path,
            catalog=catalog,
            published_accessions=published,
        )
        if verdict:
            out.append((species, verdict))
    return out


def _needs_stamp(species: SpeciesMetadata) -> tuple[ValidationRecord, str] | None:
    """A report that no longer describes its files and is not yet stamped.

    The catalog is deliberately ignored here. A catalog change means the
    species should be *revalidated* — a `--stale` concern — but it does
    not make the existing report describe the wrong files, which is what
    stamping is about. Including it would also make the offline check
    depend on whether `--check-sra` was passed, which it must not.
    """
    record = existing_record(species)
    if record is None:
        return None  # never validated is not the same as stale
    if record.state is ReportState.STALE:
        return None  # already superseded; stamping again would be a no-op
    verdict = assess(record, species_dir=species.path, catalog=None)
    if not verdict or not verdict.reason or "last validation" in verdict.reason:
        return None
    return record, verdict.reason


def check_stale(repo: SpeciesRepo, *, catalog: str | None = None) -> list[tuple[str, str]]:
    """Offline: which reports no longer describe their files.

    Never consults GenomeArk — this is what runs where there may be no
    network and where the data may legitimately not exist yet.
    """
    out: list[tuple[str, str]] = []
    for species in repo.iter_species():
        pending = _needs_stamp(species)
        if pending:
            out.append((species.key, pending[1]))
    return out


def mark_species_stale(repo: SpeciesRepo, *, catalog: str | None = None) -> list[WriteResult]:
    """Stamp STALE on reports whose inputs changed. Offline and instant."""
    written: list[WriteResult] = []
    for species in repo.iter_species():
        pending = _needs_stamp(species)
        if pending:
            record, reason = pending
            written.append(write_species_report(species, mark_stale(record, reason)))
    return written


def write_checks_reference(repo: SpeciesRepo, checks) -> WriteResult:
    """Write the ID lookup table next to the reports that use it."""
    markdown = render_checks_reference(checks, tool_version=tool_version())
    return WriteResult(
        subject="<checks>",
        markdown_changed=write_if_changed(repo.root / CHECKS_FILE, markdown),
        json_changed=False,
    )


def write_corpus_report(
    repo: SpeciesRepo, report: Report, *, validated_at: str | None = None
) -> WriteResult:
    """The repo-root table. Only meaningful after a full unfiltered run."""
    records: list[tuple[str, ValidationRecord]] = []
    for species in repo.iter_species():
        record = existing_record(species)
        if record:
            records.append((str(species.path.relative_to(repo.root)), record))

    corpus_findings = [
        f.as_dict()
        for f in report.findings
        if f.subject == "<corpus>" and f.severity is not Severity.SKIPPED
    ]
    stamp = validated_at or utc_now()
    markdown = render_corpus_report(
        records,
        validated_at=stamp,
        tool_version=tool_version(),
        corpus_findings=corpus_findings,
    )
    import json

    payload = {
        "validated_at": stamp,
        "tool_version": tool_version(),
        "counts": {state.value: 0 for state in {r.state for _, r in records}},
        "corpus_findings": corpus_findings,
        "species": {r.subject: r.state.value for _, r in records},
    }
    for _, record in records:
        payload["counts"][record.state.value] += 1

    return WriteResult(
        subject="<corpus>",
        markdown_changed=write_if_changed(repo.root / CORPUS_MARKDOWN, markdown),
        json_changed=write_if_changed(
            repo.root / CORPUS_JSON, json.dumps(payload, indent=2) + "\n"
        ),
    )


__all__ = [
    "CORPUS_JSON",
    "CORPUS_MARKDOWN",
    "WriteResult",
    "assess_data",
    "catalog_digest",
    "check_stale",
    "existing_record",
    "mark_species_stale",
    "record_for",
    "stale_species",
    "tool_version",
    "write_checks_reference",
    "write_corpus_report",
    "write_species_report",
]
