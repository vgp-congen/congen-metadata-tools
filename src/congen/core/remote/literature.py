"""Europe PMC: publication candidates for a data accession.

**Why Europe PMC and not NCBI `elink`.** The validator design proposed
`elink` from bioproject to pubmed. Measured on 25 sampled bioprojects
from this corpus:

    NCBI elink bioproject -> pubmed      2 / 25   (8%)
    Europe PMC full-text accession       17 / 25  (68%)

Europe PMC searches for the accession string in article full text, which
is how data citation actually happens. `elink` relies on a submitter
having linked the record, which mostly nobody does.

**These are candidates, not answers.** Several accessions return more
than one hit, and nothing in the response distinguishes the paper that
generated the data from one that reused it. This module therefore returns
what it found and makes no judgement; `congen citations --propose` writes
the candidates to a staging file for a human to accept or reject.
"""

from __future__ import annotations

import json
import urllib.parse
from dataclasses import dataclass, field

from congen.core import http
from congen.core.cache import Cache

SEARCH_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"

NAMESPACE = "europepmc-accession"

#: More than a handful of hits for one accession means the search matched
#: something generic rather than a data citation, and a human reviewing
#: the queue is better served by the top few than by forty.
MAX_CANDIDATES = 5


@dataclass(frozen=True)
class Candidate:
    doi: str = ""
    title: str = ""
    journal: str = ""
    year: str = ""
    authors: str = ""
    pmid: str = ""

    @property
    def citation(self) -> str:
        """A plain one-line reference, for a human to accept or rewrite."""
        parts = [part for part in (self.authors, f"({self.year})" if self.year else "", self.title) if part]
        line = " ".join(parts).strip()
        if self.journal:
            line = f"{line} {self.journal}." if line else f"{self.journal}."
        return line.strip()


@dataclass
class AccessionHits:
    accession: str
    total: int = 0
    candidates: tuple[Candidate, ...] = ()
    #: Set when the lookup itself failed, as opposed to finding nothing.
    #: The two must not be conflated: an outage is not evidence of
    #: absence, and recording it as `none` would retire a bioproject
    #: nobody looked at.
    error: str | None = None

    @property
    def found(self) -> bool:
        return bool(self.candidates)


def _first_author(payload: dict) -> str:
    authors = payload.get("authorString") or ""
    if not authors:
        return ""
    first = authors.split(",")[0].strip().rstrip(".")
    return f"{first} et al." if "," in authors else first


def parse_search(accession: str, text: str) -> AccessionHits:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        return AccessionHits(accession=accession, error=f"unparseable response: {exc}")
    results = (payload.get("resultList") or {}).get("result") or []
    candidates = tuple(
        Candidate(
            doi=str(item.get("doi") or ""),
            title=str(item.get("title") or "").rstrip("."),
            journal=str(item.get("journalTitle") or ""),
            year=str(item.get("pubYear") or ""),
            authors=_first_author(item),
            pmid=str(item.get("pmid") or ""),
        )
        for item in results[:MAX_CANDIDATES]
    )
    return AccessionHits(
        accession=accession,
        total=int(payload.get("hitCount") or 0),
        candidates=candidates,
    )


@dataclass
class EuropePmc:
    cache: Cache = field(default_factory=Cache)

    def search_accession(self, accession: str) -> AccessionHits:
        """Full-text search for one data accession, cached.

        Quoted, so `PRJNA1234` does not also match `PRJNA12345`.
        """
        cached = self.cache.get(NAMESPACE, accession)
        if cached is not None:
            return parse_search(accession, json.dumps(cached))
        query = urllib.parse.quote(f'"{accession}"')
        url = (
            f"{SEARCH_URL}?query={query}&format=json&resultType=core"
            f"&pageSize={MAX_CANDIDATES}"
        )
        try:
            text = http.get_text(url)
        except Exception as exc:  # noqa: BLE001 - an outage is not an answer
            return AccessionHits(accession=accession, error=str(exc))
        hits = parse_search(accession, text)
        if hits.error is None:
            try:
                self.cache.set(NAMESPACE, accession, json.loads(text))
            except Exception:  # noqa: BLE001 - caching is best-effort
                pass
        return hits
