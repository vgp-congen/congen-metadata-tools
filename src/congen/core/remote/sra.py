"""NCBI SRA lookups: run and experiment accessions to biosamples.

Uses eutils `runinfo`, which returns `Run`, `Experiment`, `BioSample` and
`BioProject` as named CSV columns. NCBI rather than ENA because the tool
already depends on NCBI, because NCBI is the authority of record for the
`SAMN`/`PRJNA` identifiers the sheets use, and because `Experiment`
arriving beside `Run` makes the experiment-expansion question free to
answer. The two services were checked against each other and agree
exactly, DDBJ `DRR` accessions included.

**Batching is not an optimization here, it is the difference between
usable and not.** The corpus holds 3,761 run accessions; at the 3/s
anonymous cap, one request each would take twenty minutes. `esearch`
accepts accessions OR'd into a single term and `efetch` takes the
resulting UID list by POST, so ~150 accessions cost two requests and the
whole corpus is well under a minute. `core.http` paces the calls.
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from congen.core import http
from congen.core.cache import Cache

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
NAMESPACE = "sra-runinfo"

#: Accessions per esearch term. Large enough that the corpus is a handful
#: of round trips, small enough to stay inside eutils' term limits.
DEFAULT_BATCH_SIZE = 150


@dataclass(frozen=True)
class RunInfo:
    run: str
    experiment: str | None = None
    biosample: str | None = None
    bioproject: str | None = None
    sample_name: str | None = None

    def as_dict(self) -> dict:
        return {
            "run": self.run,
            "experiment": self.experiment,
            "biosample": self.biosample,
            "bioproject": self.bioproject,
            "sample_name": self.sample_name,
        }


@dataclass
class RunIndex:
    """Resolution of the accessions that were asked about."""

    #: Queried accession -> the runs it names. A run accession maps to
    #: itself; an experiment maps to every run it contains.
    resolved: dict[str, tuple[RunInfo, ...]] = field(default_factory=dict)
    #: Accessions SRA returned nothing for.
    missing: tuple[str, ...] = ()

    def runs_for(self, accession: str) -> tuple[RunInfo, ...]:
        return self.resolved.get(accession, ())

    def biosamples_for(self, accession: str) -> set[str]:
        return {r.biosample for r in self.runs_for(accession) if r.biosample}

    @property
    def all_runs(self) -> list[RunInfo]:
        seen: dict[str, RunInfo] = {}
        for runs in self.resolved.values():
            for run in runs:
                seen.setdefault(run.run, run)
        return list(seen.values())

    @property
    def bioprojects(self) -> set[str]:
        return {r.bioproject for r in self.all_runs if r.bioproject}


def _batched(items: Sequence[str], size: int) -> Iterable[Sequence[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def parse_runinfo(text: str) -> list[RunInfo]:
    """Parse a `runinfo` CSV.

    Read by header name, not column position: the format has 47 columns
    and only the names are a stable contract.
    """
    rows: list[RunInfo] = []
    for row in csv.DictReader(io.StringIO(text)):
        run = (row.get("Run") or "").strip()
        if not run:
            continue

        def cell(name: str) -> str | None:
            value = (row.get(name) or "").strip()
            return value or None

        rows.append(
            RunInfo(
                run=run,
                experiment=cell("Experiment"),
                biosample=cell("BioSample"),
                bioproject=cell("BioProject"),
                sample_name=cell("SampleName"),
            )
        )
    return rows


class Sra:
    def __init__(
        self,
        cache: Cache | None = None,
        *,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> None:
        self.cache = cache or Cache()
        self.batch_size = batch_size

    def _api_key_params(self) -> dict[str, str]:
        key = http.ncbi_api_key()
        return {"api_key": key} if key else {}

    def _esearch(self, accessions: Sequence[str]) -> list[str]:
        params = {
            "db": "sra",
            "term": " OR ".join(accessions),
            "retmax": str(max(len(accessions) * 4, 500)),
            "retmode": "json",
            **self._api_key_params(),
        }
        payload = json.loads(http.post_text(f"{EUTILS}/esearch.fcgi", params))
        return list((payload.get("esearchresult") or {}).get("idlist") or [])

    def _efetch_runinfo(self, uids: Sequence[str]) -> list[RunInfo]:
        params = {
            "db": "sra",
            "id": ",".join(uids),
            "rettype": "runinfo",
            "retmode": "text",
            **self._api_key_params(),
        }
        return parse_runinfo(http.post_text(f"{EUTILS}/efetch.fcgi", params))

    def _fetch_batch(self, accessions: Sequence[str]) -> list[RunInfo]:
        uids = self._esearch(accessions)
        if not uids:
            return []
        return self._efetch_runinfo(uids)

    def lookup(self, accessions: Iterable[str]) -> RunIndex:
        """Resolve run and experiment accessions to their runs.

        Cached per queried accession, so a second species sharing a
        bioproject costs nothing.
        """
        wanted = sorted({a.strip() for a in accessions if a and a.strip()})
        index = RunIndex()
        pending: list[str] = []

        for accession in wanted:
            cached = self.cache.get(NAMESPACE, accession)
            if cached is None:
                pending.append(accession)
            elif cached:
                index.resolved[accession] = tuple(RunInfo(**row) for row in cached)
            else:
                index.resolved[accession] = ()

        if pending:
            fetched: list[RunInfo] = []
            for batch in _batched(pending, self.batch_size):
                fetched.extend(self._fetch_batch(batch))

            by_run = {r.run: r for r in fetched}
            by_experiment: dict[str, list[RunInfo]] = {}
            for run in fetched:
                if run.experiment:
                    by_experiment.setdefault(run.experiment, []).append(run)

            for accession in pending:
                if accession in by_run:
                    runs = (by_run[accession],)
                elif accession in by_experiment:
                    runs = tuple(sorted(by_experiment[accession], key=lambda r: r.run))
                else:
                    runs = ()
                index.resolved[accession] = runs
                self.cache.set(NAMESPACE, accession, [r.as_dict() for r in runs])

        index.missing = tuple(a for a in wanted if not index.resolved.get(a))
        return index
