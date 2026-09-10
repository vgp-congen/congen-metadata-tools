"""``congen citations`` — the command line.

    congen citations --report     offline: the review queue, worst first
    congen citations --propose    network: write candidates to a staging file

`--propose` never touches the curated file. It writes proposals beside
it, for a human to accept, reject or ignore. That separation is the whole
point: the lookup resolves about two thirds of bioprojects and cannot
tell a paper that generated data from one that reused it, so its output
is evidence, not a decision.
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

import click

from congen.core.cache import Cache
from congen.core.metadata.citations import (
    CITATIONS_FILE,
    Citation,
    Status,
    load_citations,
    merge_proposals,
    render_citations,
)
from congen.core.metadata.discovery import MetadataRootNotFound, SpeciesRepo
from congen.core.metadata.writers import write_if_changed
from congen.core.remote.bioproject import BioProjects
from congen.core.remote.literature import EuropePmc
from congen.tools.readme.record import RECORD_JSON, load_record

EXIT_OK = 0
EXIT_PROBLEM = 1
EXIT_MISUSE = 2


def _repo(metadata_root: Path | None) -> SpeciesRepo:
    try:
        return SpeciesRepo(metadata_root) if metadata_root else SpeciesRepo.discover()
    except (MetadataRootNotFound, FileNotFoundError, ValueError) as exc:
        click.echo(f"{exc}", err=True)
        sys.exit(EXIT_MISUSE)


def survey(repo: SpeciesRepo) -> tuple[dict[str, Counter], dict[str, set[str]]]:
    """Which bioprojects the corpus depends on, and how much.

    Two sources, and the union is what needs a citation. `README.txt`
    says what a species *claims* to draw on; `dataset.json`'s SRA
    mapping says what actually contributed runs. `E002` and `E003` exist
    because those two disagree for three species, so taking either alone
    would build the wrong queue.

    Weight is measured in **samples**, not species: one unreviewed
    project contributing forty samples matters more than one contributing
    one.
    """
    samples: dict[str, Counter] = {}
    species_of: dict[str, set[str]] = {}
    for species in repo.iter_species():
        dataset = load_record(species.path / RECORD_JSON)
        attributed = (
            dataset.bioprojects_by_sample(species.sheet) if dataset else {}
        )
        for sample, projects in attributed.items():
            for project in projects:
                samples.setdefault(project, Counter())[species.key] += 1
                species_of.setdefault(project, set()).add(species.key)
        declared = species.readme.bioprojects if species.readme else []
        for project in declared:
            samples.setdefault(project, Counter())
            species_of.setdefault(project, set()).add(species.key)
    return samples, species_of


@click.command("citations")
@click.option(
    "--metadata-root",
    type=click.Path(path_type=Path, file_okay=False),
    help="congen-metadata checkout (default: discovered, or $CONGEN_METADATA_ROOT).",
)
@click.option("--report", "report", is_flag=True, help="Offline: the review queue.")
@click.option(
    "--propose",
    is_flag=True,
    help="Network: write candidate citations to a staging file for review.",
)
@click.option(
    "--only-unreviewed/--all-projects",
    default=True,
    show_default=True,
    help="With --propose: skip bioprojects a human has already ruled on.",
)
@click.option("--limit", type=int, help="With --propose: stop after this many lookups.")
@click.option("--no-cache", is_flag=True, help="Bypass the on-disk cache.")
def citations(
    metadata_root: Path | None,
    report: bool,
    propose: bool,
    only_unreviewed: bool,
    limit: int | None,
    no_cache: bool,
) -> None:
    """Curate the BioProject citations the generated READMEs cite."""
    if report == propose:
        click.echo("choose exactly one of --report or --propose.", err=True)
        sys.exit(EXIT_MISUSE)

    repo = _repo(metadata_root)
    if not repo.species_dirs():
        click.echo(f"no species found under {repo.species_root}", err=True)
        sys.exit(EXIT_MISUSE)

    index = load_citations(repo.root)
    for issue in index.issues:
        click.echo(f"warning  {issue.code} {issue.message}", err=True)

    samples, species_of = survey(repo)
    if report:
        _report(repo, index, samples, species_of)
    else:
        _propose(
            repo,
            index,
            samples,
            species_of,
            only_unreviewed=only_unreviewed,
            limit=limit,
            no_cache=no_cache,
        )


def _rank(samples: dict[str, Counter], species_of: dict[str, set[str]]):
    """Worst first: most samples, then most species, then accession."""
    return sorted(
        samples,
        key=lambda project: (
            -sum(samples[project].values()),
            -len(species_of.get(project, ())),
            project,
        ),
    )


def _report(repo, index, samples, species_of) -> None:
    ranked = _rank(samples, species_of)
    by_status = Counter(index.status_of(project) for project in ranked)

    click.echo(f"{len(ranked)} bioprojects across {len(list(repo.species_dirs()))} species")
    for status in (Status.CONFIRMED, Status.NONE, Status.UNREVIEWED):
        click.echo(f"  {by_status.get(status, 0):4d}  {status}")

    total_samples = sum(sum(counts.values()) for counts in samples.values())
    covered = sum(
        sum(samples[project].values())
        for project in ranked
        if index.get(project).is_citable
    )
    if total_samples:
        click.echo(
            f"\nsample coverage: {covered} of {total_samples} attributed samples "
            f"have a confirmed citation ({100 * covered / total_samples:.0f}%)"
        )

    queue = [project for project in ranked if not index.status_of(project).is_reviewed]
    if not queue:
        click.echo("\nnothing awaiting review")
        return
    click.echo(f"\nawaiting review, worst first ({len(queue)}):")
    click.echo(f"  {'bioproject':16} {'samples':>7} {'species':>7}  title")
    for project in queue:
        entry = index.get(project)
        n_samples = sum(samples[project].values())
        click.echo(
            f"  {project:16} {n_samples:7d} {len(species_of.get(project, ())):7d}  "
            f"{(entry.title or '—')[:56]}"
        )


def _propose(
    repo, index, samples, species_of, *, only_unreviewed: bool, limit: int | None, no_cache: bool
) -> None:
    ranked = _rank(samples, species_of)
    wanted = [
        project
        for project in ranked
        if not (only_unreviewed and index.status_of(project).is_reviewed)
    ]
    if limit:
        wanted = wanted[:limit]
    if not wanted:
        click.echo("nothing to propose")
        sys.exit(EXIT_OK)

    cache = Cache(enabled=not no_cache)
    titles = BioProjects(cache).lookup(wanted)
    europepmc = EuropePmc(cache)

    proposals: list[Citation] = []
    found = errors = 0
    for project in wanted:
        summary = titles.get(project)
        hits = europepmc.search_accession(project)
        if hits.error:
            errors += 1
        top = hits.candidates[0] if hits.candidates else None
        if top:
            found += 1
        # Always `unreviewed`: a proposal is evidence for a human, and
        # writing `confirmed` here would be the tool deciding.
        proposals.append(
            Citation(
                bioproject=project,
                status=Status.UNREVIEWED,
                title=summary.title if summary else "",
                submitter=summary.submitter if summary else "",
                doi=top.doi if top else "",
                citation=top.citation if top else "",
                notes=_note(hits),
            )
        )
        click.echo(
            f"{project:16} {sum(samples[project].values()):4d} samples  "
            f"{'hit ' if top else 'none'}  {(top.doi if top else '') or (hits.error or '')}"
        )

    rows, touched = merge_proposals(index, proposals)
    path = repo.root / CITATIONS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    wrote = write_if_changed(path, render_citations(rows))
    click.echo("")
    click.echo(
        f"{len(proposals)} looked up · {found} with a candidate · "
        f"{len(proposals) - found - errors} with none · {errors} lookup failures"
    )
    click.echo(f"{touched} row(s) filled; {'wrote' if wrote else 'unchanged'} {path}")
    click.echo(
        "Every row stays `unreviewed` until a human changes it. Set `confirmed` "
        "where the candidate is the right paper, or `none` where there is "
        "genuinely none — `none` is what stops it being proposed again."
    )
    sys.exit(EXIT_OK)


def _note(hits) -> str:
    if hits.error:
        return f"lookup failed: {hits.error}"
    if not hits.candidates:
        return "no Europe PMC full-text match"
    if hits.total > 1:
        return f"{hits.total} candidates; top one shown"
    return ""
