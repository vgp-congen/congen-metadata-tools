"""The VGP reference-genome list.

``references/vgp_reference_genomes.csv`` in congen-metadata is the
authority on which assembly a species should be called against. It is what
makes reference validation normative rather than merely self-consistent.

The file is kept byte-identical to the source export, so this loader
normalizes the R-style header rather than the data being tidied in place.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from pathlib import Path

from congen.core.metadata.models import LoadIssue

DEFAULT_RELATIVE_PATH = Path("references/vgp_reference_genomes.csv")

#: Source column name -> model field. The export uses R's ``make.names``
#: style, and ``QID`` is a misnomer: the values are NCBI taxonomy IDs, not
#: Wikidata QIDs. Verified against the NCBI datasets API (65483 -> Podarcis
#: raffonei, 91951 -> Catharus ustulatus, 9117 -> Grus americana).
COLUMN_ALIASES = {
    "scientificname": "scientific_name",
    "qid": "ncbi_taxid",
    "accession.for.main.haplotype": "accession",
    "accession_for_main_haplotype": "accession",
    "accession": "accession",
}


def slugify(name: str) -> str:
    """Turn a scientific name into the repo's directory-slug form."""
    cleaned = re.sub(r"[’'.]", "", name.strip().lower())
    return re.sub(r"[^a-z0-9]+", "-", cleaned).strip("-")


def binomial_slug(name: str) -> str:
    """The genus-species slug, dropping any subspecies or qualifier.

    Repo directories are inconsistent about this: ``sus-scrofa-domesticus``
    keeps the trinomial while ``fringilla-coelebs`` drops the ``palmae``
    of *Fringilla coelebs palmae*. Lookups therefore try the full slug
    first and fall back to the binomial.
    """
    parts = slugify(name).split("-")
    return "-".join(parts[:2])


@dataclass(frozen=True)
class VgpEntry:
    scientific_name: str
    ncbi_taxid: int | None
    accession: str
    line: int | None = None

    @property
    def slug(self) -> str:
        return slugify(self.scientific_name)

    @property
    def binomial_slug(self) -> str:
        return binomial_slug(self.scientific_name)


@dataclass
class VgpReferenceList:
    path: Path | None
    entries: list[VgpEntry] = field(default_factory=list)
    issues: list[LoadIssue] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._by_accession: dict[str, VgpEntry] = {}
        self._by_slug: dict[str, list[VgpEntry]] = {}
        self._by_binomial: dict[str, list[VgpEntry]] = {}
        self._by_taxid: dict[int, VgpEntry] = {}
        for entry in self.entries:
            self._by_accession[entry.accession] = entry
            self._by_slug.setdefault(entry.slug, []).append(entry)
            self._by_binomial.setdefault(entry.binomial_slug, []).append(entry)
            if entry.ncbi_taxid is not None:
                self._by_taxid[entry.ncbi_taxid] = entry

    def __len__(self) -> int:
        return len(self.entries)

    @property
    def accessions(self) -> set[str]:
        return set(self._by_accession)

    def by_accession(self, accession: str) -> VgpEntry | None:
        return self._by_accession.get(accession)

    def by_taxid(self, taxid: int) -> VgpEntry | None:
        return self._by_taxid.get(taxid)

    def by_slug(self, slug: str) -> VgpEntry | None:
        """Resolve a repo directory slug to its list entry.

        Returns ``None`` both when nothing matches and when the slug is
        ambiguous; use :meth:`candidates_for_slug` to tell those apart.
        """
        matches = self.candidates_for_slug(slug)
        return matches[0] if len(matches) == 1 else None

    def candidates_for_slug(self, slug: str) -> list[VgpEntry]:
        exact = self._by_slug.get(slug)
        if exact:
            return list(exact)
        return list(self._by_binomial.get(slug, []))

    def accession_for_slug(self, slug: str) -> str | None:
        entry = self.by_slug(slug)
        return entry.accession if entry else None


def load_vgp_list(path: Path) -> VgpReferenceList:
    """Load the VGP list, tolerating the export's blank trailing row."""
    try:
        text = path.read_bytes().decode("utf-8-sig", "replace")
    except FileNotFoundError:
        return VgpReferenceList(
            path=path,
            issues=[LoadIssue("missing_file", f"{path} not found", path)],
        )
    except OSError as exc:
        return VgpReferenceList(
            path=path,
            issues=[LoadIssue("unreadable", f"cannot read {path}: {exc}", path)],
        )

    reader = csv.reader(io.StringIO(text.replace("\r\n", "\n")))
    issues: list[LoadIssue] = []
    try:
        header = next(reader)
    except StopIteration:
        return VgpReferenceList(
            path=path, issues=[LoadIssue("parse_error", "file is empty", path, 1)]
        )

    index: dict[str, int] = {}
    for position, raw in enumerate(header):
        field_name = COLUMN_ALIASES.get(raw.strip().lower())
        if field_name:
            index[field_name] = position
    for required in ("scientific_name", "accession"):
        if required not in index:
            issues.append(
                LoadIssue(
                    "missing_column",
                    f"cannot find a column for {required!r} in {header}",
                    path,
                    1,
                )
            )
    if "scientific_name" not in index or "accession" not in index:
        return VgpReferenceList(path=path, issues=issues)

    entries: list[VgpEntry] = []
    for offset, row in enumerate(reader, start=2):
        if not row or all(not cell.strip() for cell in row):
            continue  # the export ends with a blank line; not worth reporting
        try:
            name = row[index["scientific_name"]].strip()
            accession = row[index["accession"]].strip()
        except IndexError:
            issues.append(LoadIssue("short_row", f"row has {len(row)} field(s)", path, offset))
            continue
        if not name or not accession:
            issues.append(
                LoadIssue("incomplete_row", "scientific name or accession is empty", path, offset)
            )
            continue

        taxid: int | None = None
        if "ncbi_taxid" in index and index["ncbi_taxid"] < len(row):
            raw_taxid = row[index["ncbi_taxid"]].strip()
            if raw_taxid.isdigit():
                taxid = int(raw_taxid)
            elif raw_taxid:
                issues.append(
                    LoadIssue("bad_taxid", f"taxid {raw_taxid!r} is not an integer", path, offset)
                )

        entries.append(
            VgpEntry(
                scientific_name=name, ncbi_taxid=taxid, accession=accession, line=offset
            )
        )

    duplicates = {a for a in (e.accession for e in entries) if
                  sum(1 for e in entries if e.accession == a) > 1}
    for accession in sorted(duplicates):
        issues.append(
            LoadIssue("duplicate_accession", f"{accession} appears more than once", path)
        )

    return VgpReferenceList(path=path, entries=entries, issues=issues)
