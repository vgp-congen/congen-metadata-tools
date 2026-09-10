"""``congen citations`` — the command line.

    congen citations --report     offline: what is decided, what remains
    congen citations --collect    offline: file finished blocks out of the queue
    congen citations --propose    network: add newly-seen BioProjects to the queue

Each has one job. `--propose` never decides anything and never touches
the record; `--collect` never looks anything up; `--report` writes
nothing.
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

import click

from congen.core.cache import Cache
from congen.core.metadata.citations import (
    QUEUE_FILE,
    RECORD_FILE,
    Entry,
    Status,
    load_citations,
    merge_into_record,
    merge_proposals,
    parse_queue,
    parse_record,
    render_queue,
    render_record,
    reopen,
)
from congen.core.metadata.discovery import MetadataRootNotFound, SpeciesRepo
from congen.core.metadata.writers import write_if_changed
from congen.core.remote.bioproject import BioProjects
from congen.core.remote.doi import Doi, DoiNotFound
from congen.core.remote.literature import EuropePmc
from congen.tools.readme.record import RECORD_JSON, load_record

EXIT_OK = 0
EXIT_PROBLEM = 1
EXIT_MISUSE = 2


def _repo(metadata_root: Path | None) -> SpeciesRepo:
    try:
        repo = SpeciesRepo(metadata_root) if metadata_root else SpeciesRepo.discover()
    except (MetadataRootNotFound, FileNotFoundError, ValueError) as exc:
        click.echo(f"{exc}", err=True)
        sys.exit(EXIT_MISUSE)
    if not repo.species_dirs():
        click.echo(f"no species found under {repo.species_root}", err=True)
        sys.exit(EXIT_MISUSE)
    return repo


def survey(repo: SpeciesRepo) -> tuple[dict[str, int], dict[str, list[str]]]:
    """Which BioProjects the corpus depends on, and how much.

    The **union** of two sources. `README.txt` says what a species claims
    to draw on; the SRA mapping in `dataset.json` says what actually
    contributed runs. Those disagree — `E002` and `E003` exist because of
    it — so either source alone builds the wrong queue.

    Weight is samples, not species: one unreviewed project carrying 150
    samples matters more than one carrying a single sample.
    """
    samples: Counter = Counter()
    species_of: dict[str, set[str]] = {}
    for species in repo.iter_species():
        dataset = load_record(species.path / RECORD_JSON)
        attributed = dataset.bioprojects_by_sample(species.sheet) if dataset else {}
        for projects in attributed.values():
            for project in projects:
                samples[project] += 1
                species_of.setdefault(project, set()).add(species.key)
        for project in species.readme.bioprojects if species.readme else []:
            samples.setdefault(project, 0)
            species_of.setdefault(project, set()).add(species.key)
    return dict(samples), {k: sorted(v) for k, v in species_of.items()}


@click.command("citations")
@click.option(
    "--metadata-root",
    type=click.Path(path_type=Path, file_okay=False),
    help="congen-metadata checkout (default: discovered, or $CONGEN_METADATA_ROOT).",
)
@click.option("--report", is_flag=True, help="Offline: progress and what remains.")
@click.option(
    "--collect",
    is_flag=True,
    help="Offline: file decided blocks into the record and drop them from the queue.",
)
@click.option(
    "--verify",
    is_flag=True,
    help="Network: check accepted DOIs resolve, and fill in their references.",
)
@click.option(
    "--propose",
    is_flag=True,
    help="Network: look up newly-seen BioProjects and add them to the queue.",
)
@click.option("--limit", type=int, help="With --propose: stop after this many lookups.")
@click.option("--no-cache", is_flag=True, help="Bypass the on-disk cache.")
def citations(
    metadata_root: Path | None,
    report: bool,
    collect: bool,
    verify: bool,
    propose: bool,
    limit: int | None,
    no_cache: bool,
) -> None:
    """Curate the BioProject citations the generated READMEs depend on."""
    chosen = [
        name
        for name, on in (
            ("--report", report),
            ("--collect", collect),
            ("--verify", verify),
            ("--propose", propose),
        )
        if on
    ]
    if len(chosen) != 1:
        click.echo(
            "choose exactly one of --report, --collect, --verify or --propose.", err=True
        )
        sys.exit(EXIT_MISUSE)

    repo = _repo(metadata_root)
    index = load_citations(repo.root)
    for issue in index.issues:
        click.echo(f"warning  {issue.code} {issue.message}", err=True)

    samples, species_of = survey(repo)
    if report:
        _report(repo, index, samples, species_of)
    elif collect:
        _collect(repo, index)
    elif verify:
        _verify(repo, no_cache=no_cache)
    else:
        _propose(repo, index, samples, species_of, limit=limit, no_cache=no_cache)


def _rank(accessions, samples, species_of):
    return sorted(
        accessions,
        key=lambda a: (-samples.get(a, 0), -len(species_of.get(a, ())), a),
    )


def _report(repo, index, samples, species_of) -> None:
    known = set(samples) | set(index.entries)
    by_status = Counter(index.status_of(a) for a in known)
    total = sum(samples.values())
    covered = sum(samples.get(a, 0) for a in known if index.get(a).is_citable)
    settled = sum(samples.get(a, 0) for a in known if index.status_of(a).is_reviewed)

    click.echo(f"{len(known)} BioProjects · {total} attributed samples")
    for status in (Status.CONFIRMED, Status.NOT_FOUND, Status.UNREVIEWED):
        label = {
            Status.NOT_FOUND: "not_found  (searched; re-checked when something new appears)",
        }.get(status, str(status))
        click.echo(f"  {by_status.get(status, 0):4d}  {label}")
    if total:
        click.echo("")
        click.echo(
            f"reviewed:  {settled:5d} of {total} samples ({100 * settled / total:.0f}%)"
        )
        click.echo(
            f"citable:   {covered:5d} of {total} samples ({100 * covered / total:.0f}%)"
        )

    if index.unfiled:
        click.echo("")
        click.echo(
            f"{len(index.unfiled)} decided block(s) still in the queue; "
            f"run `congen citations --collect` to file them."
        )

    queue = _rank(index.needs_review(known), samples, species_of)
    if not queue:
        click.echo("\nnothing awaiting review")
        return

    # How far a given number of decisions gets you. The weight is heavily
    # concentrated, and a reviewer facing 298 items deserves to know that
    # the first two dozen are most of the value.
    milestones = _milestones([samples.get(a, 0) for a in queue], total - settled)
    if milestones:
        click.echo("")
        click.echo("the queue is front-loaded:")
        for target, count in milestones:
            click.echo(
                f"  {count:4d} more decision(s) would cover {target}% of the "
                f"remaining {total - settled} samples"
            )
    click.echo(f"\nawaiting review ({len(queue)}), worst first:")
    for accession in queue[:15]:
        entry = index.get(accession)
        click.echo(
            f"  {accession:16} {samples.get(accession, 0):5d} samples  "
            f"{(entry.title or '—')[:56]}"
        )
    if len(queue) > 15:
        click.echo(f"  … and {len(queue) - 15} more; see {QUEUE_FILE}")


def _collect(repo, index) -> None:
    """Move decided blocks out of the queue and into the record."""
    try:
        queued, _ = parse_queue((repo.root / QUEUE_FILE).read_text("utf-8"))
    except (FileNotFoundError, OSError):
        click.echo(f"no queue at {repo.root / QUEUE_FILE}", err=True)
        sys.exit(EXIT_MISUSE)
    try:
        record = parse_record((repo.root / RECORD_FILE).read_text("utf-8"))
    except (FileNotFoundError, OSError):
        record = {}

    decided = {a: e for a, e in queued.items() if e.status.is_reviewed}
    remaining = {a: e for a, e in queued.items() if not e.status.is_reviewed}
    if not decided:
        click.echo("nothing decided in the queue")
        sys.exit(EXIT_OK)

    for accession, entry in sorted(decided.items()):
        # Merge, never replace: the record knows which DOIs resolve and
        # what their references say, and the queue cannot.
        record[accession] = merge_into_record(record.get(accession), entry)
        detail = ", ".join(entry.dois) if entry.dois else "no publication"
        click.echo(f"filed  {accession:16} {entry.status}  {detail}")

    (repo.root / RECORD_FILE).parent.mkdir(parents=True, exist_ok=True)
    wrote_record = write_if_changed(repo.root / RECORD_FILE, render_record(record.values()))
    wrote_queue = write_if_changed(repo.root / QUEUE_FILE, render_queue(remaining.values()))
    click.echo("")
    click.echo(
        f"{len(decided)} filed · {len(remaining)} still awaiting review · "
        f"record {'updated' if wrote_record else 'unchanged'}, "
        f"queue {'updated' if wrote_queue else 'unchanged'}"
    )
    sys.exit(EXIT_OK)


def _propose(repo, index, samples, species_of, *, limit: int | None, no_cache: bool) -> None:
    try:
        queued, _ = parse_queue((repo.root / QUEUE_FILE).read_text("utf-8"))
    except (FileNotFoundError, OSError):
        queued = {}

    known = set(samples) | set(index.entries)
    # `confirmed` and `none` are final and never looked up again. `pending`
    # is looked up precisely so a paper that has since appeared can be
    # noticed. And a queued block that already has candidates is left
    # alone — someone may be part-way through reviewing it.
    wanted = _rank(
        [
            a
            for a in known
            if index.status_of(a) is not Status.CONFIRMED
            and not queued.get(a, Entry(a)).dois
        ],
        samples,
        species_of,
    )
    if limit:
        wanted = wanted[:limit]
    if not wanted:
        click.echo("nothing to look up; every BioProject is decided or already queued")
        sys.exit(EXIT_OK)

    cache = Cache(enabled=not no_cache)
    titles = BioProjects(cache).lookup(wanted)
    europepmc = EuropePmc(cache)

    proposals: list[Entry] = []
    found = errors = reopened = starred = 0
    for accession in wanted:
        summary = titles.get(accession)
        hits = europepmc.search_accession(accession)
        errors += 1 if hits.error else 0
        found += 1 if hits.candidates else 0
        entry = Entry(
            bioproject=accession,
            status=Status.UNREVIEWED,
            title=summary.title if summary else "",
            submitter=summary.submitter if summary else "",
            samples=samples.get(accession, 0),
            species=species_of.get(accession, []),
        )
        # Every candidate, not just the top one. 79 of 298 had more than
        # one, and storing only the best made a quarter of the evidence
        # unreachable.
        for candidate in hits.candidates:
            if not candidate.doi:
                continue
            entry.dois.append(candidate.doi)
            entry.references[candidate.doi] = candidate.citation
            # The submitter's own affiliation on a paper is the strongest
            # signal available for free that the data was generated for it.
            if entry.submitter and candidate.matches_submitter(entry.submitter):
                entry.starred.add(candidate.doi)
                starred += 1
        settled = index.get(accession)
        if settled.status is Status.NOT_FOUND:
            # Only genuinely new evidence reopens a "no paper yet".
            revived = reopen(settled, entry.dois)
            if revived is None:
                click.echo(
                    f"{accession:16} {entry.samples:5d} samples  "
                    f"still no new candidate; stays filed"
                )
                continue
            entry.dois = revived.dois
            entry.rejected = revived.rejected
            entry.considered = revived.considered
            reopened += 1
        proposals.append(entry)
        click.echo(
            f"{accession:16} {entry.samples:5d} samples  "
            f"{len(entry.dois)} candidate(s)"
            + (f", {len(entry.starred)} matching the submitter" if entry.starred else "")
            + (f"  {hits.error}" if hits.error else "")
        )

    if errors:
        # A lookup that failed is not a lookup that found nothing. Writing
        # the queue now would record "no candidates" for BioProjects
        # nobody managed to search, and a reviewer would file them as
        # NO PUBLICATION FOUND on the strength of an outage.
        click.echo("", err=True)
        click.echo(
            f"{errors} lookup(s) failed, so the queue was not written. "
            "Re-run when the service is back; cached results make it cheap.",
            err=True,
        )
        sys.exit(EXIT_PROBLEM)

    merged, changed = merge_proposals(queued, proposals)
    (repo.root / QUEUE_FILE).parent.mkdir(parents=True, exist_ok=True)
    wrote = write_if_changed(repo.root / QUEUE_FILE, render_queue(merged.values()))
    click.echo("")
    click.echo(
        f"{len(wanted)} looked up · {found} with at least one candidate · "
        f"{errors} lookup failures"
        + (f" · {starred} affiliation match(es)" if starred else "")
        + (f" · {reopened} reopened by new evidence" if reopened else "")
    )
    click.echo(
        f"{changed} block(s) changed; queue {'updated' if wrote else 'unchanged'} "
        f"at {repo.root / QUEUE_FILE}"
    )
    click.echo("Review by deleting the lines that are not true, then --collect.")
    sys.exit(EXIT_OK)


def _milestones(weights, remaining: int, targets=(50, 75, 90)) -> list[tuple[int, int]]:
    if not remaining:
        return []
    out = []
    running = 0
    pending = list(targets)
    for index, weight in enumerate(weights, start=1):
        running += weight
        while pending and running >= remaining * pending[0] / 100:
            out.append((pending.pop(0), index))
    return out


def _verify(repo, *, no_cache: bool) -> None:
    """Check every accepted DOI resolves, and fill in its reference.

    Two jobs, one request each. A reviewer types DOIs by hand, and a
    typo is invisible to everything else here — a capital O for a zero
    looks fine in a diff and would put a dead link in 79 documents. The
    same request that proves the DOI exists returns its title, authors
    and journal, so a hand-typed citation gets prose instead of
    rendering as a bare DOI.

    Only unverified DOIs are checked, so this costs one request per new
    citation rather than 300 every run.
    """
    try:
        record = parse_record((repo.root / RECORD_FILE).read_text("utf-8"))
    except (FileNotFoundError, OSError):
        click.echo(f"no record at {repo.root / RECORD_FILE}", err=True)
        sys.exit(EXIT_MISUSE)

    pending = [
        (accession, doi)
        for accession, entry in sorted(record.items())
        for doi in entry.dois
        if doi not in entry.verified
    ]
    if not pending:
        total = sum(len(e.dois) for e in record.values())
        click.echo(f"all {total} accepted DOI(s) already verified")
        sys.exit(EXIT_OK)

    resolver = Doi(Cache(enabled=not no_cache))
    bad: list[tuple[str, str]] = []
    outages: list[tuple[str, str]] = []
    filled = 0

    for accession, doi in pending:
        entry = record[accession]
        try:
            work = resolver.resolve(doi)
        except DoiNotFound:
            bad.append((accession, doi))
            click.echo(f"BAD   {accession:16} {doi}  does not resolve")
            continue
        except Exception as exc:  # noqa: BLE001 - an outage is not a verdict
            outages.append((accession, doi))
            click.echo(f"?     {accession:16} {doi}  lookup failed: {exc}")
            continue
        entry.verified.add(doi)
        # Only fill a blank. A reference a human wrote, or one Europe PMC
        # supplied, is not ours to overwrite.
        if not entry.reference(doi) and work.reference:
            entry.references[doi] = work.reference
            filled += 1
        click.echo(f"ok    {accession:16} {doi}  {work.reference[:70]}")

    if outages:
        # Never record a verdict nobody managed to reach.
        click.echo("", err=True)
        click.echo(
            f"{len(outages)} lookup(s) failed, so the record was not written. "
            "Re-run when doi.org is reachable; results are cached.",
            err=True,
        )
        sys.exit(EXIT_PROBLEM)

    wrote = write_if_changed(repo.root / RECORD_FILE, render_record(record.values()))
    click.echo("")
    click.echo(
        f"{len(pending)} checked · {len(pending) - len(bad)} resolved · "
        f"{len(bad)} did not · {filled} reference(s) filled in"
    )
    click.echo(f"record {'updated' if wrote else 'unchanged'}")

    if bad:
        click.echo("", err=True)
        click.echo(
            f"{len(bad)} DOI(s) do not resolve. Most likely a typo — fix them in "
            f"{RECORD_FILE} or put the BioProject back in the queue:",
            err=True,
        )
        for accession, doi in bad:
            click.echo(f"  {accession}: {doi}", err=True)
        sys.exit(EXIT_PROBLEM)
    sys.exit(EXIT_OK)
