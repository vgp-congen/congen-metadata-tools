"""The curated citation files in `congen-metadata/references/`.

**Why curated rather than looked up.** Measured on 25 sampled
bioprojects, NCBI `elink` resolves 2 to a publication and Europe PMC's
full-text accession search resolves 17. Neither is good enough to trust
unattended, and several accessions return more than one candidate with
nothing to distinguish the paper that *generated* the data from one that
*reused* it. So a tool proposes and a human decides, and the decision has
to live somewhere a human can edit — which a live API is not.

**Why the three states matter.** `unreviewed` and `none` are different
facts: nobody has looked, versus somebody looked and there is nothing to
find. Without the distinction every refresh re-proposes the same hopeless
cases forever — on the probe's hit rate, roughly a third of the corpus —
and the review queue never empties. It is the same distinction `G017` and
`G018` draw, for the same reason.

**The corpus needs 298 rows, not the 208 the READMEs suggest.** The
`README.txt` files cite 208 distinct bioprojects between them; the SRA
mapping in `dataset.json` attributes runs to 296; the union is 298. Of
the 88-project difference, 87 are reachable only through the 22 species
that have no `README.txt` at all, and the remaining 3 are exactly the
`E002` cases. So the undocumented species are not merely missing a file —
they conceal 87 bioprojects' worth of uncredited data generators, which
is the real weight behind `G017`.

Bioprojects barely recur: only six are cited by more than one species, so
this is essentially one row per bioproject. Its value is not reuse; it is
that a human can correct it, that rendering stays deterministic and
offline, and that the gaps are visible and countable.
"""

from __future__ import annotations

import csv
import enum
import io
from dataclasses import dataclass, field, replace
from pathlib import Path

from congen.core.metadata.models import LoadIssue

#: Relative to the congen-metadata root.
CITATIONS_FILE = Path("references") / "bioproject_citations.csv"
TOOLS_FILE = Path("references") / "tool_citations.yaml"

#: `--propose` writes into the curated file itself, not a staging file
#: beside it.
#:
#: The design doc specified a staging file so a tool could never touch a
#: human decision. Reversed once the corpus was measured: there are 298
#: bioprojects, so a staging file means a reviewer copying rows between
#: two 298-row CSVs, and the guard that actually matters is enforceable
#: directly. `merge_proposals` never alters a reviewed row and never
#: overwrites a non-empty field, so a proposal can only ever fill a
#: blank. Status stays `unreviewed` until a human changes it.

COLUMNS = (
    "bioproject",
    "status",
    "title",
    "submitter",
    "doi",
    "citation",
    "reviewed_by",
    "reviewed_on",
    "notes",
)


class Status(enum.Enum):
    #: A human accepted this DOI.
    CONFIRMED = "confirmed"
    #: A human looked and there is no citable publication. Never
    #: re-proposed — this is the state that lets the queue empty.
    NONE = "none"
    #: Nobody has looked yet.
    UNREVIEWED = "unreviewed"

    @property
    def is_reviewed(self) -> bool:
        return self is not Status.UNREVIEWED

    @property
    def is_citable(self) -> bool:
        return self is Status.CONFIRMED

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class Citation:
    bioproject: str
    status: Status = Status.UNREVIEWED
    title: str = ""
    submitter: str = ""
    doi: str = ""
    citation: str = ""
    reviewed_by: str = ""
    reviewed_on: str = ""
    notes: str = ""
    line: int | None = None

    @property
    def is_citable(self) -> bool:
        return self.status.is_citable and bool(self.citation or self.doi)

    @property
    def doi_url(self) -> str | None:
        return f"https://doi.org/{self.doi}" if self.doi else None

    def as_row(self) -> dict[str, str]:
        return {
            "bioproject": self.bioproject,
            "status": self.status.value,
            "title": self.title,
            "submitter": self.submitter,
            "doi": self.doi,
            "citation": self.citation,
            "reviewed_by": self.reviewed_by,
            "reviewed_on": self.reviewed_on,
            "notes": self.notes,
        }


@dataclass
class CitationIndex:
    path: Path | None = None
    entries: dict[str, Citation] = field(default_factory=dict)
    issues: list[LoadIssue] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.entries)

    def get(self, bioproject: str) -> Citation:
        """Always a `Citation`. An unknown bioproject is `unreviewed`.

        Returning a default rather than `None` is what lets a renderer
        treat "nobody has looked" as an ordinary state instead of a
        special case at every call site.
        """
        return self.entries.get(bioproject) or Citation(bioproject=bioproject)

    def status_of(self, bioproject: str) -> Status:
        return self.get(bioproject).status

    def needs_review(self, bioprojects) -> list[str]:
        return sorted(
            {
                accession
                for accession in bioprojects
                if not self.status_of(accession).is_reviewed
            }
        )

    def citable(self, bioprojects) -> list[str]:
        return sorted({a for a in bioprojects if self.get(a).is_citable})


def parse_citations(text: str, path: Path | None = None) -> CitationIndex:
    """Tolerant, like every other loader here.

    A malformed row comes back described rather than raising: this file is
    hand-edited, so a stray comma must not take down every document that
    depends on it.
    """
    index = CitationIndex(path=path)
    if not text.strip():
        return index
    reader = csv.DictReader(io.StringIO(text))
    missing = [column for column in ("bioproject", "status") if column not in (reader.fieldnames or ())]
    if missing:
        index.issues.append(
            LoadIssue(
                code="C001",
                message=f"missing required column(s): {', '.join(missing)}",
                path=path,
            )
        )
        return index

    for number, row in enumerate(reader, start=2):
        accession = (row.get("bioproject") or "").strip()
        if not accession:
            index.issues.append(
                LoadIssue(code="C002", message="row with no bioproject", path=path, line=number)
            )
            continue
        raw_status = (row.get("status") or "").strip().lower()
        try:
            status = Status(raw_status) if raw_status else Status.UNREVIEWED
        except ValueError:
            index.issues.append(
                LoadIssue(
                    code="C003",
                    message=f"{accession}: unknown status {raw_status!r}, treating as unreviewed",
                    path=path,
                    line=number,
                )
            )
            status = Status.UNREVIEWED
        if accession in index.entries:
            index.issues.append(
                LoadIssue(
                    code="C004",
                    message=f"{accession}: duplicate row, keeping the last",
                    path=path,
                    line=number,
                )
            )
        index.entries[accession] = Citation(
            bioproject=accession,
            status=status,
            title=(row.get("title") or "").strip(),
            submitter=(row.get("submitter") or "").strip(),
            doi=(row.get("doi") or "").strip(),
            citation=(row.get("citation") or "").strip(),
            reviewed_by=(row.get("reviewed_by") or "").strip(),
            reviewed_on=(row.get("reviewed_on") or "").strip(),
            notes=(row.get("notes") or "").strip(),
            line=number,
        )
    return index


def load_citations(root: Path) -> CitationIndex:
    path = root / CITATIONS_FILE
    try:
        return parse_citations(path.read_text("utf-8"), path)
    except (FileNotFoundError, OSError):
        return CitationIndex(path=path)


def render_citations(entries) -> str:
    """Sorted by accession, so a hand edit and a tool write agree."""
    from congen.core.metadata.writers import render_csv

    rows = sorted((entry.as_row() for entry in entries), key=lambda row: row["bioproject"])
    return render_csv(COLUMNS, rows)


# -- tool and assembly citations -------------------------------------------

#: Deliberately not generated. These are bibliographic claims about
#: someone else's work, and a tool that guesses them would put a wrong
#: reference in 79 documents at once. The file ships with `pending`
#: entries so the gap is visible and fillable.
TOOL_KEYS = ("snparcher", "gatk", "genomeark", "assembly")


@dataclass(frozen=True)
class ToolCitation:
    key: str
    name: str = ""
    status: str = "pending"
    doi: str = ""
    citation: str = ""
    url: str = ""

    @property
    def is_citable(self) -> bool:
        return self.status == "confirmed" and bool(self.citation or self.doi)


def parse_tool_citations(text: str, path: Path | None = None) -> dict[str, ToolCitation]:
    from congen.core.metadata.writers import load_yaml_roundtrip

    try:
        data = load_yaml_roundtrip(text) or {}
    except Exception:  # noqa: BLE001 - a broken file must not break rendering
        return {}
    out: dict[str, ToolCitation] = {}
    for key, value in (data.items() if hasattr(data, "items") else ()):
        if not hasattr(value, "get"):
            continue
        out[str(key)] = ToolCitation(
            key=str(key),
            name=str(value.get("name") or ""),
            status=str(value.get("status") or "pending"),
            doi=str(value.get("doi") or ""),
            citation=str(value.get("citation") or ""),
            url=str(value.get("url") or ""),
        )
    return out


def load_tool_citations(root: Path) -> dict[str, ToolCitation]:
    path = root / TOOLS_FILE
    try:
        return parse_tool_citations(path.read_text("utf-8"), path)
    except (FileNotFoundError, OSError):
        return {}


def merge_proposals(index: CitationIndex, proposals) -> tuple[list[Citation], int]:
    """Fold proposals into the curated entries, conservatively.

    Two rules, and they are what make it safe to write into the file a
    human is editing:

    1. A row whose status is `confirmed` or `none` is never touched. A
       human has ruled on it, and a lookup is not entitled to reopen that.
    2. On an `unreviewed` row, a proposal fills **empty** fields only. It
       never overwrites something already there, so a half-finished hand
       edit survives a refresh.

    Returns the full set of rows to write, and how many were changed.
    """
    merged = dict(index.entries)
    changed = 0
    for proposal in proposals:
        existing = merged.get(proposal.bioproject)
        if existing is None:
            merged[proposal.bioproject] = proposal
            changed += 1
            continue
        if existing.status.is_reviewed:
            continue
        fields = {}
        for name in ("title", "submitter", "doi", "citation", "notes"):
            current = getattr(existing, name)
            offered = getattr(proposal, name)
            if not current and offered:
                fields[name] = offered
        if fields:
            merged[proposal.bioproject] = replace(existing, **fields)
            changed += 1
    return sorted(merged.values(), key=lambda entry: entry.bioproject), changed
