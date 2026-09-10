"""NCBI BioProject titles, over eutils.

Separate from `remote/ncbi.py`, which speaks the Datasets API about
assemblies, and from `remote/sra.py`, which speaks eutils about runs. One
module per resource rather than per host, matching what is already here.

This exists because of what the Europe PMC probe showed: a title resolves
for every bioproject in the corpus (25/25 sampled) while a publication
resolves for about two thirds. So even a bioproject with no citable paper
can be given a human-readable line in a document instead of a bare
accession, and a reviewer working the queue can see what each project
actually is.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from congen.core import http
from congen.core.cache import Cache

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
NAMESPACE = "bioproject-summary"

DEFAULT_BATCH_SIZE = 100


@dataclass(frozen=True)
class BioProjectSummary:
    accession: str
    title: str = ""
    submitter: str = ""
    description: str = ""

    @property
    def url(self) -> str:
        return f"https://www.ncbi.nlm.nih.gov/bioproject/{self.accession}"


def _api_key_params() -> dict[str, str]:
    key = http.ncbi_api_key()
    return {"api_key": key} if key else {}


def parse_summaries(text: str) -> dict[str, BioProjectSummary]:
    """Parse an esummary JSON payload, keyed by accession.

    Tolerant: a document missing the fields we want is skipped rather
    than raising, because this is metadata about other people's records.
    """
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {}
    result = payload.get("result") or {}
    out: dict[str, BioProjectSummary] = {}
    for uid in result.get("uids") or []:
        document = result.get(uid) or {}
        accession = str(document.get("project_acc") or "").strip()
        if not accession:
            continue
        out[accession] = BioProjectSummary(
            accession=accession,
            title=str(document.get("project_title") or "").strip(),
            submitter=str(document.get("submitter_organization") or "").strip(),
            description=str(document.get("project_description") or "").strip(),
        )
    return out


def _batched(items: Sequence[str], size: int) -> Iterable[Sequence[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


@dataclass
class BioProjects:
    cache: Cache = field(default_factory=Cache)
    batch_size: int = DEFAULT_BATCH_SIZE

    def _uids(self, accessions: Sequence[str]) -> list[str]:
        params = {
            "db": "bioproject",
            "term": " OR ".join(f"{a}[Project Accession]" for a in accessions),
            "retmax": str(max(len(accessions) * 2, 200)),
            "retmode": "json",
            **_api_key_params(),
        }
        payload = json.loads(http.post_text(f"{EUTILS}/esearch.fcgi", params))
        return list((payload.get("esearchresult") or {}).get("idlist") or [])

    def _summaries(self, uids: Sequence[str]) -> dict[str, BioProjectSummary]:
        params = {
            "db": "bioproject",
            "id": ",".join(uids),
            "retmode": "json",
            **_api_key_params(),
        }
        return parse_summaries(http.post_text(f"{EUTILS}/esummary.fcgi", params))

    def lookup(self, accessions: Iterable[str]) -> dict[str, BioProjectSummary]:
        """Titles for `accessions`, cached per accession.

        Batched because the corpus has 208 distinct bioprojects and one
        request per accession would be rude as well as slow.
        """
        wanted = sorted({a.strip() for a in accessions if a and a.strip()})
        out: dict[str, BioProjectSummary] = {}
        pending: list[str] = []
        for accession in wanted:
            cached = self.cache.get(NAMESPACE, accession)
            if cached is not None:
                out[accession] = BioProjectSummary(**cached)
            else:
                pending.append(accession)

        for batch in _batched(pending, self.batch_size):
            uids = self._uids(batch)
            if not uids:
                continue
            found = self._summaries(uids)
            for accession, summary in found.items():
                out[accession] = summary
            # Cache only what was asked for, so a stray match from the
            # OR-query does not pollute the namespace.
            for accession in batch:
                if accession in found:
                    self.cache.set(NAMESPACE, accession, found[accession].__dict__)
        return out
