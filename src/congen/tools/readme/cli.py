"""``congen readme`` — the command line.

Phase 1 implements `--refresh` only: the network pass that writes
`dataset.json`. Rendering arrives in Phase 3. A bare invocation says so
rather than doing something surprising.

The network/pure split is at the CLI deliberately, not just inside the
code. `--refresh` and a bare render are separate invocations so CI can
run them as separate jobs, on separate triggers, with separate
permissions, without any code changing.
"""

from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import click

from congen.core.cache import Cache
from congen.core.metadata.discovery import MetadataRootNotFound, SpeciesRepo
from congen.core.metadata.writers import would_change

from .harvest import Harvester
from .record import RECORD_JSON, DatasetRecord, load_record

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
    help="Network: re-harvest GenomeArk, NCBI and the QC tables into dataset.json.",
)
@click.option(
    "--json",
    "json_path",
    type=click.Path(path_type=Path),
    help="Write a machine-readable summary of what changed here.",
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
    json_path: Path | None,
    workers: int,
    no_cache: bool,
    dry_run: bool,
) -> None:
    """Generate a per-species README.md in congen-metadata."""
    if not refresh:
        click.echo(
            "congen readme currently implements --refresh only; rendering lands "
            "in phase 3. See docs/readme-design.md.",
            err=True,
        )
        sys.exit(EXIT_MISUSE)

    if not targets and not all_species and not clade:
        click.echo("nothing selected: name a species, or pass --all.", err=True)
        sys.exit(EXIT_MISUSE)

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

    cache = Cache(enabled=not no_cache)
    results: list[tuple[str, DatasetRecord, bool]] = []

    def one(entry):
        harvester = Harvester.build(cache, tool_version=_tool_version())
        record = harvester.harvest(entry)
        path = entry.path / RECORD_JSON
        if dry_run:
            # Compare substance, exactly as write_json does, or a dry run
            # would claim every record differs because of its timestamp.
            previous = load_record(path)
            if previous is not None and previous.substance() == record.substance():
                return entry.key, record, False
            return entry.key, record, would_change(path, record.render_json())
        return entry.key, record, record.write_json(path)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for result in pool.map(one, species):
            results.append(result)

    results.sort(key=lambda r: r[0])
    changed = [key for key, _, did in results if did]
    noted = [(key, record.notes) for key, record, _ in results if record.notes]

    for key, record, did in results:
        state = "published" if record.published else "no data"
        if dry_run:
            mark = "would update" if did else "current"
        else:
            mark = "updated" if did else "unchanged"
        click.echo(
            f"{key:44} {state:10} {len(record.samples):4d} samples  "
            f"{record.count_of():4d} objects  {mark}"
        )

    if noted:
        click.echo("")
        for key, notes in noted:
            for note in notes:
                click.echo(f"note  {key}: {note}")

    click.echo("")
    click.echo(
        f"{len(results)} species · {sum(1 for _, r, _ in results if r.published)} published "
        f"· {len(changed)} {'would change' if dry_run else 'written'}"
    )

    if json_path:
        json_path.write_text(
            json.dumps(
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
                        }
                        for key, record, did in results
                    ]
                },
                indent=2,
            )
            + "\n"
        )

    sys.exit(EXIT_OK)
