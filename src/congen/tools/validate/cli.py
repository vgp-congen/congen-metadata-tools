"""``congen validate`` — the command line."""

from __future__ import annotations

import sys
from pathlib import Path

import click

from congen.core.cache import Cache
from congen.core.findings import Severity
from congen.core.metadata.discovery import MetadataRootNotFound, SpeciesRepo
from congen.core.report import render_github, render_human, render_json
from congen.tools.validate.context import (
    DEFAULT_BAM_SAMPLE,
    DEFAULT_MISSING_THRESHOLD,
    ContextGatherer,
)
from congen.tools.validate.registry import registry
from congen.tools.validate.runner import (
    DEFAULT_WORKERS,
    RunOptions,
    orphan_findings,
    run,
    selected_checks,
)


def _split(values: tuple[str, ...]) -> list[str] | None:
    """Accept both repeated flags and comma-separated lists."""
    out: list[str] = []
    for value in values:
        out.extend(part.strip() for part in value.split(",") if part.strip())
    return out or None


@click.command("validate")
@click.argument("targets", nargs=-1)
@click.option("--all", "all_species", is_flag=True, help="Validate every species.")
@click.option("--clade", help="Restrict --all to one clade.")
@click.option(
    "--metadata-root",
    type=click.Path(path_type=Path, file_okay=False),
    help="congen-metadata checkout (default: discovered, or $CONGEN_METADATA_ROOT).",
)
@click.option("--only", multiple=True, help="Check IDs or prefixes to run, e.g. -o S,F020.")
@click.option("--skip", multiple=True, help="Check IDs or prefixes to skip.")
@click.option("--json", "json_path", type=click.Path(path_type=Path), help="Write JSON here.")
@click.option("--github", is_flag=True, help="Emit GitHub workflow annotations.")
@click.option("--strict", is_flag=True, help="Exit non-zero on warnings too.")
@click.option("--show-skipped", is_flag=True, help="List skipped checks individually.")
@click.option("--quiet", is_flag=True, help="Hide info-level findings.")
@click.option("--all-bams", is_flag=True, help="Read every BAM header, not a sample.")
@click.option(
    "--bam-sample",
    type=int,
    default=DEFAULT_BAM_SAMPLE,
    show_default=True,
    help="How many BAM headers to read per species.",
)
@click.option(
    "--missing-contig-threshold",
    type=float,
    default=DEFAULT_MISSING_THRESHOLD,
    show_default=True,
    help="Percent of assembly bases that may be absent from a VCF before F009 warns.",
)
@click.option("--workers", type=int, default=DEFAULT_WORKERS, show_default=True)
@click.option("--no-cache", is_flag=True, help="Bypass the on-disk cache.")
@click.option("--list-checks", is_flag=True, help="Print the check catalog and exit.")
def validate(
    targets: tuple[str, ...],
    all_species: bool,
    clade: str | None,
    metadata_root: Path | None,
    only: tuple[str, ...],
    skip: tuple[str, ...],
    json_path: Path | None,
    github: bool,
    strict: bool,
    show_skipped: bool,
    quiet: bool,
    all_bams: bool,
    bam_sample: int,
    missing_contig_threshold: float,
    workers: int,
    no_cache: bool,
    list_checks: bool,
) -> None:
    """Cross-check species metadata against the data published on GenomeArk.

    Read-only: this never modifies congen-metadata.
    """
    if list_checks:
        for check in registry.all:
            needs = ", ".join(check.needs) or "-"
            click.echo(f"{check.id}  {str(check.severity):8s} {check.summary}  [needs: {needs}]")
        return

    if not targets and not all_species:
        raise click.UsageError("give one or more species, or --all")

    try:
        repo = SpeciesRepo(metadata_root) if metadata_root else SpeciesRepo.discover()
    except MetadataRootNotFound as exc:
        raise click.ClickException(str(exc)) from exc

    if all_species:
        species_list = [repo.load_dir(d) for d in repo.species_dirs(clade=clade)]
    else:
        try:
            species_list = [repo.load(target) for target in targets]
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc

    gatherer = ContextGatherer(
        cache=Cache(enabled=not no_cache),
        bam_sample=bam_sample,
        all_bams=all_bams,
        missing_contig_threshold=missing_contig_threshold,
    )
    options = RunOptions(only=_split(only), skip=_split(skip), workers=workers)
    if not selected_checks(options):
        raise click.UsageError(
            "--only/--skip selected no checks; run --list-checks to see the catalog"
        )

    report = run(repo, species_list, gatherer, options)

    if all_species and not clade:
        orphans = orphan_findings(repo, gatherer)
        if orphans:
            report.subjects.append("<corpus>")
            report.extend(orphans)

    if github:
        annotations = render_github(report.findings, root=repo.root)
        if annotations:
            click.echo(annotations)
    else:
        click.echo(
            render_human(
                report, show_skipped=show_skipped, show_info=not quiet, root=repo.root
            )
        )

    if json_path:
        json_path.write_text(render_json(report), "utf-8")
        click.echo(f"wrote {json_path}", err=True)

    sys.exit(report.exit_code(strict=strict))
