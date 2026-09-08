"""Orchestration: gather context per species, run checks, collect findings."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Iterable, Sequence

from congen.core.findings import Check, Finding, Report, Severity
from congen.core.metadata.discovery import SpeciesRepo
from congen.core.metadata.models import SpeciesMetadata
from congen.tools.validate import checks as _checks  # noqa: F401 - registers catalog
from congen.tools.validate.context import SRA, Context, ContextGatherer
from congen.tools.validate.registry import registry

DEFAULT_WORKERS = 6


@dataclass
class RunOptions:
    only: Sequence[str] | None = None
    skip: Sequence[str] | None = None
    workers: int = DEFAULT_WORKERS
    #: Tier 5 is opt-in; see --check-sra.
    check_sra: bool = False


def selected_checks(options: RunOptions) -> list[Check]:
    """Choose checks, honouring the tier-5 opt-in.

    Data-driven rather than by ID prefix: a check that needs the SRA
    slice is excluded unless SRA lookups were requested. Excluded rather
    than reported as SKIPPED, so an ordinary run's report is not padded
    with checks nobody asked for.
    """
    chosen = registry.select(only=options.only, skip=options.skip)
    if not options.check_sra:
        chosen = [c for c in chosen if SRA not in c.needs]
    return chosen


def run_species(
    species: SpeciesMetadata,
    gatherer: ContextGatherer,
    checks: Sequence[Check],
    *,
    vgp_list=None,
) -> tuple[list[Finding], Context]:
    required = registry.required_context(checks)
    context = gatherer.gather(species, required, vgp_list=vgp_list)
    findings = registry.run(context, checks)
    for note in context.gather_notes:
        findings.append(
            Finding(
                id="X001",
                severity=Severity.WARN,
                subject=context.subject,
                message=note,
                detail="a gather step failed, so some checks were skipped",
            )
        )
    return findings, context


def run(
    repo: SpeciesRepo,
    species_list: Iterable[SpeciesMetadata],
    gatherer: ContextGatherer,
    options: RunOptions | None = None,
) -> Report:
    """Validate each species, in parallel, and collect one report."""
    options = options or RunOptions()
    checks = selected_checks(options)
    species_list = list(species_list)
    vgp_list = repo.vgp_list

    report = Report(
        subjects=[s.key for s in species_list],
        checks_run=[c.id for c in checks],
    )

    def work(species: SpeciesMetadata):
        return run_species(species, gatherer, checks, vgp_list=vgp_list)

    if len(species_list) == 1 or options.workers <= 1:
        outcomes = [work(species) for species in species_list]
    else:
        with ThreadPoolExecutor(max_workers=options.workers) as pool:
            outcomes = list(pool.map(work, species_list))

    for findings, context in outcomes:
        report.extend(findings)
        if context.upload_status:
            report.add_status(context.upload_status)

    return report


def orphan_findings(repo: SpeciesRepo, gatherer: ContextGatherer) -> list[Finding]:
    """Corpus-level checks: published accessions with no repo species.

    Split by whether the VGP list knows the accession — a listed species
    with data and no metadata is a gap to fill (G020); anything else is
    unexplained data (G021).
    """
    published = set(gatherer.genomeark.accessions())
    vgp = repo.vgp_list

    claimed: set[str] = set()
    for species in repo.iter_species():
        reference = species.reference
        if reference.accession:
            claimed.add(reference.accession)
            counterpart = reference.counterpart()
            if counterpart:
                claimed.add(counterpart)

    out: list[Finding] = []
    for accession in sorted(published - claimed):
        entry = vgp.by_accession(accession)
        if entry:
            out.append(
                Finding(
                    id="G020",
                    severity=Severity.WARN,
                    subject="<corpus>",
                    message=(
                        f"{accession} ({entry.scientific_name}) is published and in the "
                        "VGP list, but has no species directory"
                    ),
                )
            )
        else:
            out.append(
                Finding(
                    id="G021",
                    severity=Severity.WARN,
                    subject="<corpus>",
                    message=(
                        f"{accession} is published but is in neither the repo nor "
                        "the VGP list"
                    ),
                )
            )
    return out
