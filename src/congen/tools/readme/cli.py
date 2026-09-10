"""``congen readme`` — the command line.

The network/pure split is at the CLI, not just inside the code:

    congen readme --refresh   network, writes dataset.json, renders nothing
    congen readme             offline, writes README.md
    congen readme --check     offline, writes nothing, exit 1 if stale

Three invocations become three CI jobs on three triggers with three sets
of permissions, and no code changes to get there. `--refresh` deliberately
does not go on to render: keeping them separate is what makes the offline
one runnable in a job with no network egress at all.
"""

from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import click

from congen.core.cache import Cache
from congen.core.metadata.citations import load_citations
from congen.core.metadata.discovery import MetadataRootNotFound, SpeciesRepo
from congen.core.metadata.writers import would_change
from congen.core.validation_record import load_record as load_validation

from .gate import evaluate
from .harvest import Harvester
from .record import RECORD_JSON, DatasetRecord, load_record
from .render import OUTPUT_NAME, build_context, write_document, would_change_document

DEFAULT_WORKERS = 6

EXIT_OK = 0
EXIT_PROBLEM = 1
EXIT_MISUSE = 2


def _tool_version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("congen-metadata-tools")
    except PackageNotFoundError:  # pragma: no cover - only when not installed
        return "0.0.0"


def _select(repo: SpeciesRepo, targets: tuple[str, ...], all_species: bool, clade: str | None):
    if all_species or clade:
        return list(repo.iter_species(clade))
    return [repo.load(target) for target in targets]


def _resolve(
    targets: tuple[str, ...],
    all_species: bool,
    clade: str | None,
    metadata_root: Path | None,
):
    try:
        repo = SpeciesRepo(metadata_root) if metadata_root else SpeciesRepo.discover()
        species = _select(repo, targets, all_species, clade)
    except (MetadataRootNotFound, FileNotFoundError, ValueError) as exc:
        click.echo(f"{exc}", err=True)
        sys.exit(EXIT_MISUSE)
    if not species:
        # Selecting nothing and exiting 0 would let a CI job with a wrong
        # --metadata-root pass vacuously.
        click.echo(
            f"no species found under {repo.species_root}"
            + (f" for clade {clade!r}" if clade else ""),
            err=True,
        )
        sys.exit(EXIT_MISUSE)
    return repo, species


@click.command("readme")
@click.argument("targets", nargs=-1)
@click.option("--all", "all_species", is_flag=True, help="Every species in the repo.")
@click.option("--clade", help="Restrict to one clade (implies --all).")
@click.option(
    "--metadata-root",
    type=click.Path(path_type=Path, file_okay=False),
    help="congen-metadata checkout (default: discovered, or $CONGEN_METADATA_ROOT).",
)
@click.option(
    "--refresh",
    is_flag=True,
    help="Network: re-harvest into dataset.json. Renders nothing.",
)
@click.option(
    "--check",
    "check_only",
    is_flag=True,
    help="Offline: exit 1 if regenerating would change anything. Writes nothing.",
)
@click.option(
    "--json",
    "json_path",
    type=click.Path(path_type=Path),
    help="Write a machine-readable summary of what changed here.",
)
@click.option(
    "--output-name",
    default=OUTPUT_NAME,
    show_default=True,
    help="Name of the generated document.",
)
@click.option(
    "--gate-report",
    is_flag=True,
    help="Offline: which species would render fully, and why the rest would not.",
)
@click.option("--workers", type=int, default=DEFAULT_WORKERS, show_default=True)
@click.option("--no-cache", is_flag=True, help="Bypass the on-disk cache.")
@click.option("--dry-run", is_flag=True, help="With --refresh: harvest but write nothing.")
def readme(
    targets: tuple[str, ...],
    all_species: bool,
    clade: str | None,
    metadata_root: Path | None,
    refresh: bool,
    check_only: bool,
    json_path: Path | None,
    output_name: str,
    gate_report: bool,
    workers: int,
    no_cache: bool,
    dry_run: bool,
) -> None:
    """Generate a per-species README.md in congen-metadata."""
    if gate_report:
        _gate_report(targets, all_species, clade, metadata_root)
        return

    if not targets and not all_species and not clade:
        click.echo("nothing selected: name a species, or pass --all.", err=True)
        sys.exit(EXIT_MISUSE)

    _repo, species = _resolve(targets, all_species, clade, metadata_root)

    if refresh:
        _refresh(
            species, workers=workers, no_cache=no_cache, dry_run=dry_run, json_path=json_path
        )
    else:
        _render(
            species,
            vgp=_repo.vgp_list,
            citations=load_citations(_repo.root),
            output_name=output_name,
            check_only=check_only,
            json_path=json_path,
        )


# -- the network pass ------------------------------------------------------


def _refresh(
    species, *, workers: int, no_cache: bool, dry_run: bool, json_path: Path | None
) -> None:
    cache = Cache(enabled=not no_cache)
    results: list[tuple[str, DatasetRecord, bool, list[str]]] = []

    def one(entry):
        harvester = Harvester.build(cache, tool_version=_tool_version())
        record = harvester.harvest(entry)
        path = entry.path / RECORD_JSON
        if harvester.failures:
            # Never overwrite a good record with a degraded one. The old
            # one stays, and the caller exits non-zero so an unattended
            # run commits nothing.
            return entry.key, record, False, list(harvester.failures)
        if dry_run:
            # Compare substance, exactly as write_json does, or a dry run
            # would claim every record differs because of its timestamp.
            previous = load_record(path)
            if previous is not None and previous.substance() == record.substance():
                return entry.key, record, False, []
            return entry.key, record, would_change(path, record.render_json()), []
        return entry.key, record, record.write_json(path), []

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        results.extend(pool.map(one, species))
    results.sort(key=lambda row: row[0])

    for key, record, did, failures in results:
        state = "failed" if failures else ("published" if record.published else "no data")
        if dry_run:
            mark = "would update" if did else "current"
        else:
            mark = "updated" if did else "unchanged"
        click.echo(
            f"{key:44} {state:10} {len(record.samples):4d} samples  "
            f"{record.count_of():4d} objects  {mark}"
        )

    noted = [(key, record.notes) for key, record, _, _ in results if record.notes]
    if noted:
        click.echo("")
        for key, notes in noted:
            for note in notes:
                click.echo(f"note  {key}: {note}")

    changed = [key for key, _, did, _ in results if did]
    failed = [(key, f) for key, _, _, f in results if f]
    click.echo("")
    click.echo(
        f"{len(results)} species · {sum(1 for _, r, _, _ in results if r.published)} published "
        f"· {len(changed)} {'would change' if dry_run else 'written'}"
        + (f" · {len(failed)} failed" if failed else "")
    )
    if json_path:
        _write_json(
            json_path,
            {
                "harvested": [
                    {
                        "subject": key,
                        "published": record.published,
                        "accession": record.accession,
                        "samples": len(record.samples),
                        "objects": record.count_of(),
                        "changed": did,
                        "notes": record.notes,
                        "failures": next(
                            (f for k, _, _, f in results if k == key), []
                        ),
                    }
                    for key, record, did, _ in results
                ]
            },
        )

    if failed:
        click.echo("", err=True)
        click.echo(
            f"{len(failed)} species could not be harvested; their records were "
            "left untouched:",
            err=True,
        )
        for key, reasons in failed:
            for reason in reasons:
                click.echo(f"  {key}: {reason}", err=True)
        sys.exit(EXIT_PROBLEM)
    sys.exit(EXIT_OK)


# -- the offline pass ------------------------------------------------------


def context_for(entry, vgp=None, citations=None):
    """Assemble a render context from local files only."""
    dataset = load_record(entry.path / RECORD_JSON) or DatasetRecord(subject=entry.key)
    record = load_validation(entry.path / "validation.json")
    verdict = evaluate(entry.path, record, harvested_accession=dataset.accession)
    return build_context(
        entry, dataset, verdict, record, vgp=vgp, citations=citations
    )


def _render(
    species,
    *,
    vgp=None,
    citations=None,
    output_name: str,
    check_only: bool,
    json_path: Path | None,
) -> None:
    rows = []
    for entry in species:
        context = context_for(entry, vgp, citations)
        path = entry.path / output_name
        did = (
            would_change_document(context, path)
            if check_only
            else write_document(context, path)
        )
        rows.append((entry.key, context, did))

    for key, context, did in rows:
        mode = str(context.verdict.mode)
        cited = "cited" if context.verdict.cited else "uncited"
        mark = ("STALE" if did else "current") if check_only else ("written" if did else "unchanged")
        click.echo(f"{key:44} {mode:10} {cited:8} {mark}")

    changed = [key for key, _, did in rows if did]
    full = sum(1 for _, c, _ in rows if c.verdict.is_full)
    cited = sum(1 for _, c, _ in rows if c.verdict.is_full and c.verdict.cited)
    click.echo("")
    click.echo(
        f"{len(rows)} species · {cited} full and cited · {full - cited} citations blocked "
        f"· {len(rows) - full} truncated · {len(changed)} "
        f"{'out of date' if check_only else 'written'}"
    )

    if json_path:
        _write_json(
            json_path,
            {
                "documents": [
                    {
                        "subject": key,
                        "mode": str(context.verdict.mode),
                        "cited": context.verdict.cited,
                        "provenance": (
                            str(context.verdict.provenance)
                            if context.verdict.provenance
                            else None
                        ),
                        "blockers": [str(b.code) for b in context.verdict.blockers],
                        "changed": did,
                    }
                    for key, context, did in rows
                ]
            },
        )

    if check_only and changed:
        click.echo("", err=True)
        click.echo(
            f"{len(changed)} document(s) out of date; run `congen readme` to regenerate:",
            err=True,
        )
        for key in changed:
            click.echo(f"  {key}", err=True)
        sys.exit(EXIT_PROBLEM)
    sys.exit(EXIT_OK)


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n")


# -- the gate report -------------------------------------------------------


def _gate_report(
    targets: tuple[str, ...],
    all_species: bool,
    clade: str | None,
    metadata_root: Path | None,
) -> None:
    """Print the gate outcome per species. Offline, reads nothing remote.

    Deliberately a report rather than a test assertion: the counts move
    legitimately every time a pipeline run finishes, so pinning them in
    the suite would fail for the right reasons. The suite asserts the
    logic; this shows the numbers.
    """
    repo, species = _resolve(targets, all_species or not targets, clade, metadata_root)
    citations = load_citations(repo.root)
    rows = [
        (entry.key, context_for(entry, repo.vgp_list, citations).verdict)
        for entry in species
    ]

    full = [row for row in rows if row[1].is_full]
    cited = [row for row in full if row[1].cited]
    blocked = [row for row in full if not row[1].cited]
    truncated = [row for row in rows if not row[1].is_full]

    if truncated:
        click.echo("truncated — no dataset to describe:")
        for key, verdict in truncated:
            click.echo(f"  {key:42} {'; '.join(str(r) for r in verdict.blockers)}")
        click.echo("")
    if blocked:
        click.echo("full, citations blocked:")
        for key, verdict in blocked:
            fired = ",".join(verdict.fired) or "nothing fired"
            consequent = ",".join(
                f"{s.check}={s.outcome}"
                for s in verdict.unsound
                if s.check not in verdict.fired
            )
            click.echo(
                f"  {key:42} {str(verdict.provenance):8} fired={fired}"
                + (f"  consequent={consequent}" if consequent else "")
            )
        click.echo("")
    click.echo(
        f"{len(rows)} species · {len(cited)} full and cited · "
        f"{len(blocked)} citations blocked · {len(truncated)} truncated"
    )
