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
import re
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


#: Words too common in an institution name to carry any signal. Dropping
#: them is what lets "The University of Colorado" match "Department of
#: Ecology and Evolutionary Biology, University of Colorado, Boulder".
GENERIC_TOKENS = frozenset(
    """a an and the of for at de del della di und der des
    university universite universitat universidad universita college school
    faculty department dept division institute institut instituto center
    centre centro laboratory laboratories lab unit group programme program
    research sciences science studies national state federal royal
    academy academia foundation trust hospital museum ltd inc gmbh
    """.split()
)

#: A token has to be at least this long to count as distinctive.
MIN_TOKEN = 4


def _tokens(text: str) -> set[str]:
    words = re.split(r"[^a-z]+", text.lower())
    return {w for w in words if len(w) >= MIN_TOKEN and w not in GENERIC_TOKENS}


def affiliation_matches(submitter: str, affiliations) -> bool:
    """Whether any affiliation looks like the BioProject's submitter.

    Every distinctive token of the submitter must appear. Deliberately a
    weak hint rather than a verdict: "genetics" survives the stoplist and
    would match any genetics department, so the queue labels this as
    *the affiliation matches the submitter* — which is exactly what was
    checked — and never as *this is the right paper*. A false positive
    then costs a reviewer one glance, not a wrong citation.
    """
    wanted = _tokens(submitter)
    if not wanted:
        return False
    return any(wanted <= _tokens(affiliation) for affiliation in affiliations)


@dataclass(frozen=True)
class Candidate:
    doi: str = ""
    title: str = ""
    journal: str = ""
    year: str = ""
    authors: str = ""
    pmid: str = ""
    #: Every author's affiliation, not just the first author's. For
    #: `PRJEB39599` the submitter is Helsinki and the first author is in
    #: St Petersburg, with Helsinki further down the list — so reading
    #: only the top-level `affiliation` field would miss the match.
    affiliations: tuple[str, ...] = ()

    def matches_submitter(self, submitter: str) -> bool:
        return affiliation_matches(submitter, self.affiliations)

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


def _affiliations(payload: dict) -> tuple[str, ...]:
    found: list[str] = []
    if payload.get("affiliation"):
        found.append(str(payload["affiliation"]))
    for author in (payload.get("authorList") or {}).get("author") or []:
        if author.get("affiliation"):
            found.append(str(author["affiliation"]))
        details = (author.get("authorAffiliationDetailsList") or {}).get(
            "authorAffiliation"
        ) or []
        for detail in details:
            if detail.get("affiliation"):
                found.append(str(detail["affiliation"]))
    return tuple(dict.fromkeys(found))


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
            affiliations=_affiliations(item),
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
