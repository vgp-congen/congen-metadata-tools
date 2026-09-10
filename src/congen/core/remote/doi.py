"""Resolve a DOI through doi.org content negotiation.

Two jobs, one request.

**Does it exist?** A reviewer types DOIs by hand — that is the whole
point of the review mechanic — and a typo is invisible to everything
else in this toolchain. `10.1111/mec.17O63`, with a capital O for a
zero, looks perfectly plausible in a diff and would put a dead link in a
generated document. doi.org answers 404 for it.

**What does it say?** A DOI accepted by hand has no reference prose,
because a typed line carries nothing to parse. The same request that
proves the DOI exists returns its title, authors and journal, so the
reference can be filled in rather than rendered as a bare DOI.

The absence/outage distinction is load-bearing here as everywhere else:
404 means the DOI does not resolve, while a timeout or a 5xx means
nobody managed to ask. Recording the second as the first would tell a
reviewer their correct DOI was wrong.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from congen.core import http
from congen.core.cache import Cache

BASE_URL = "https://doi.org/"

#: Crossref's content-negotiation type for citation metadata.
CSL_JSON = "application/vnd.citationstyles.csl+json"

NAMESPACE = "doi-csl"


class DoiNotFound(LookupError):
    """The DOI does not resolve. A typo, or a fabrication."""

    def __init__(self, doi: str) -> None:
        super().__init__(f"{doi} does not resolve")
        self.doi = doi


@dataclass(frozen=True)
class Work:
    doi: str
    title: str = ""
    journal: str = ""
    year: str = ""
    authors: tuple[str, ...] = ()

    @property
    def url(self) -> str:
        return f"{BASE_URL}{self.doi}"

    @property
    def reference(self) -> str:
        """A one-line reference, in the same shape Europe PMC's produces.

        Matching that shape deliberately: a citation found by search and
        one typed by hand should not be distinguishable in the rendered
        document.
        """
        parts = []
        if self.authors:
            first = self.authors[0]
            parts.append(f"{first} et al." if len(self.authors) > 1 else first)
        if self.year:
            parts.append(f"({self.year})")
        if self.title:
            parts.append(self.title)
        line = " ".join(parts)
        if self.journal:
            line = f"{line} {self.journal}." if line else f"{self.journal}."
        return line.strip()


def _name(author: dict) -> str:
    """`Schield DR` — family name plus initials, as journals cite."""
    literal = (author.get("literal") or "").strip()
    if literal:
        return literal
    family = (author.get("family") or "").strip()
    given = (author.get("given") or "").strip()
    initials = "".join(part[0] for part in given.replace(".", " ").split() if part)
    return f"{family} {initials}".strip() if family else initials


def _year(payload: dict) -> str:
    for key in ("issued", "published-print", "published-online", "created"):
        parts = (payload.get(key) or {}).get("date-parts") or []
        if parts and parts[0] and parts[0][0]:
            return str(parts[0][0])
    return ""


def _text(value) -> str:
    """CSL sometimes gives a list where the schema says string."""
    if isinstance(value, list):
        return str(value[0]).strip() if value else ""
    return str(value or "").strip()


def parse_work(doi: str, text: str) -> Work:
    payload = json.loads(text)
    return Work(
        doi=doi,
        title=_text(payload.get("title")).rstrip("."),
        journal=_text(payload.get("container-title")),
        year=_year(payload),
        authors=tuple(
            name for name in (_name(a) for a in payload.get("author") or []) if name
        ),
    )


@dataclass
class Doi:
    cache: Cache = field(default_factory=Cache)

    def resolve(self, doi: str) -> Work:
        """Metadata for `doi`, or `DoiNotFound` if it does not resolve.

        Anything other than a 404 propagates: a caller must be able to
        tell "this DOI is wrong" from "Crossref was unreachable", because
        only the first is a fact about the DOI.
        """
        cached = self.cache.get(NAMESPACE, doi)
        if cached is not None:
            return Work(
                doi=doi,
                title=cached.get("title", ""),
                journal=cached.get("journal", ""),
                year=cached.get("year", ""),
                authors=tuple(cached.get("authors") or ()),
            )
        try:
            response = http.request(
                f"{BASE_URL}{doi}", headers={"Accept": CSL_JSON}
            )
        except http.HttpError as exc:
            if exc.status == 404:
                raise DoiNotFound(doi) from exc
            raise
        work = parse_work(doi, response.body.decode("utf-8", "replace"))
        self.cache.set(
            NAMESPACE,
            doi,
            {
                "title": work.title,
                "journal": work.journal,
                "year": work.year,
                "authors": list(work.authors),
            },
        )
        return work
