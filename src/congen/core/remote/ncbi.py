"""NCBI lookups: dataset reports and assembly reports.

The dataset report gives organism, taxonomy ID and the paired GCA/GCF
accession — the last being what turns a syntactic GCA-versus-GCF flip
into a confirmed statement that two accessions name the same assembly.

The assembly report is the per-sequence table behind tier 3a: every
sequence with its GenBank and RefSeq accessions, its length and its role.
Matching a VCF's contigs against it is what detects a wrong reference.
"""

from __future__ import annotations

import re
import urllib.error
from dataclasses import dataclass, field

from congen.core import http
from congen.core.cache import Cache

DATASETS_API = "https://api.ncbi.nlm.nih.gov/datasets/v2alpha"
FTP_GENOMES = "https://ftp.ncbi.nlm.nih.gov/genomes/all"

#: Assembly metadata is immutable for a given accession, so entries never
#: need to expire.
NAMESPACE = "ncbi-dataset-report"
REPORT_NAMESPACE = "ncbi-assembly-report"

#: Sequence-naming schemes an assembly report exposes. A VCF uses exactly
#: one of them; which one is not knowable in advance.
SCHEMES = ("genbank", "refseq", "name")

#: NCBI's FTP directory name is the accession plus the assembly name with
#: every run of unusual characters collapsed to a single underscore.
#: Verified against the two corpus assemblies whose names need it:
#: "mEubGla1.1.hap2.+ XY" -> "mEubGla1.1.hap2._XY" and
#: "mPanOnc1 haplotype 2" -> "mPanOnc1_haplotype_2".
_UNSAFE_RUN = re.compile(r"[^A-Za-z0-9._-]+")

ASSEMBLED_MOLECULE = "assembled-molecule"


@dataclass(frozen=True)
class AssemblyInfo:
    accession: str
    organism_name: str | None = None
    tax_id: int | None = None
    assembly_name: str | None = None
    assembly_level: str | None = None
    paired_accession: str | None = None

    def names_same_assembly_as(self, other: str | None) -> bool:
        """True if ``other`` is this accession or its confirmed pair."""
        if not other:
            return False
        return other in {self.accession, self.paired_accession}


class Ncbi:
    def __init__(self, cache: Cache | None = None) -> None:
        self.cache = cache or Cache()

    def _report(self, accession: str) -> dict | None:
        def fetch() -> dict:
            url = f"{DATASETS_API}/genome/accession/{accession}/dataset_report"
            payload = http.get_json(url)
            reports = payload.get("reports") or []
            # Cache the miss too: a bad accession stays bad, and a
            # sentinel avoids re-requesting it once per run.
            return reports[0] if reports else {}

        report = self.cache.memoize(NAMESPACE, accession, fetch)
        return report or None

    def assembly_report(self, accession: str) -> AssemblyReport | None:
        """Fetch the per-sequence assembly report.

        The FTP path is built from the assembly name rather than scraped
        from a directory listing: that is one request instead of two and
        has no HTML parsing in it. It works for 77 of the 79 corpus
        accessions directly and for the other two once the name is
        sanitized. A listing scrape remains as a fallback in case the
        naming rule ever shifts.
        """
        cached = self.cache.get(REPORT_NAMESPACE, accession)
        if cached is not None:
            return AssemblyReport.from_dict(cached) if cached else None

        info = self.assembly_info(accession)
        report: AssemblyReport | None = None
        if info and info.assembly_name:
            text = self._fetch_report_text(accession, info.assembly_name)
            if text:
                report = parse_assembly_report(accession, info.assembly_name, text)

        self.cache.set(REPORT_NAMESPACE, accession, report.as_dict() if report else {})
        return report

    def _report_url(self, accession: str, assembly_name: str) -> str:
        digits = accession.split("_")[1].split(".")[0]
        stem = f"{accession}_{sanitize_assembly_name(assembly_name)}"
        return (
            f"{FTP_GENOMES}/{accession[:3]}/{digits[0:3]}/{digits[3:6]}/"
            f"{digits[6:9]}/{stem}/{stem}_assembly_report.txt"
        )

    def _fetch_report_text(self, accession: str, assembly_name: str) -> str | None:
        try:
            return http.get_text(self._report_url(accession, assembly_name))
        except (http.HttpError, urllib.error.URLError, OSError):
            pass
        # Fallback: ask the directory which name it actually used.
        stem = self._discover_directory(accession)
        if not stem:
            return None
        digits = accession.split("_")[1].split(".")[0]
        url = (
            f"{FTP_GENOMES}/{accession[:3]}/{digits[0:3]}/{digits[3:6]}/"
            f"{digits[6:9]}/{stem}/{stem}_assembly_report.txt"
        )
        try:
            return http.get_text(url)
        except (http.HttpError, urllib.error.URLError, OSError):
            return None

    def _discover_directory(self, accession: str) -> str | None:
        digits = accession.split("_")[1].split(".")[0]
        base = (
            f"{FTP_GENOMES}/{accession[:3]}/{digits[0:3]}/{digits[3:6]}/{digits[6:9]}/"
        )
        try:
            html = http.get_text(base)
        except (http.HttpError, urllib.error.URLError, OSError):
            return None
        names = re.findall(rf'href="({re.escape(accession)}_[^"/]+)/"', html)
        return names[0] if names else None

    def assembly_info(self, accession: str) -> AssemblyInfo | None:
        """Look up one accession, or ``None`` if NCBI does not know it."""
        report = self._report(accession)
        if not report:
            return None
        organism = report.get("organism") or {}
        info = report.get("assembly_info") or {}
        paired = info.get("paired_assembly") or {}
        tax_id = organism.get("tax_id")
        return AssemblyInfo(
            accession=report.get("accession") or accession,
            organism_name=organism.get("organism_name"),
            tax_id=int(tax_id) if isinstance(tax_id, (int, str)) and str(tax_id).isdigit() else None,
            assembly_name=info.get("assembly_name"),
            assembly_level=info.get("assembly_level"),
            paired_accession=paired.get("accession"),
        )


def sanitize_assembly_name(name: str) -> str:
    """Apply NCBI's FTP directory naming rule to an assembly name."""
    return _UNSAFE_RUN.sub("_", name)


@dataclass(frozen=True)
class AssemblySequence:
    name: str
    role: str
    genbank: str | None
    refseq: str | None
    length: int | None
    unit: str = ""

    @property
    def is_assembled_molecule(self) -> bool:
        return self.role == ASSEMBLED_MOLECULE

    def accession_for(self, scheme: str) -> str | None:
        if scheme == "genbank":
            return self.genbank
        if scheme == "refseq":
            return self.refseq
        return self.name


@dataclass
class AssemblyReport:
    accession: str
    assembly_name: str
    sequences: tuple[AssemblySequence, ...] = ()

    def lengths(self, scheme: str) -> dict[str, int | None]:
        """Sequence identifier -> length, under one naming scheme."""
        out: dict[str, int | None] = {}
        for sequence in self.sequences:
            key = sequence.accession_for(scheme)
            if key:
                out[key] = sequence.length
        return out

    def roles(self, scheme: str) -> dict[str, str]:
        out: dict[str, str] = {}
        for sequence in self.sequences:
            key = sequence.accession_for(scheme)
            if key:
                out[key] = sequence.role
        return out

    def best_scheme(self, observed: set[str]) -> str:
        """The naming scheme that explains the most observed names."""
        return max(SCHEMES, key=lambda s: len(observed & set(self.lengths(s))))

    def scheme_of(self, name: str) -> str | None:
        """Which scheme, if any, knows ``name``."""
        for scheme in SCHEMES:
            if name in self.lengths(scheme):
                return scheme
        return None

    def total_length(self, scheme: str) -> int:
        return sum(length or 0 for length in self.lengths(scheme).values())

    def as_dict(self) -> dict:
        return {
            "accession": self.accession,
            "assembly_name": self.assembly_name,
            "sequences": [
                {
                    "name": s.name,
                    "role": s.role,
                    "genbank": s.genbank,
                    "refseq": s.refseq,
                    "length": s.length,
                    "unit": s.unit,
                }
                for s in self.sequences
            ],
        }

    @classmethod
    def from_dict(cls, payload: dict) -> AssemblyReport:
        return cls(
            accession=payload["accession"],
            assembly_name=payload.get("assembly_name", ""),
            sequences=tuple(AssemblySequence(**s) for s in payload.get("sequences", [])),
        )


def parse_assembly_report(accession: str, assembly_name: str, text: str) -> AssemblyReport:
    """Parse the tab-separated sequence table.

    Columns: Sequence-Name, Sequence-Role, Assigned-Molecule,
    Assigned-Molecule-Location/Type, GenBank-Accn, Relationship,
    RefSeq-Accn, Assembly-Unit, Sequence-Length, UCSC-style-name.
    """
    sequences: list[AssemblySequence] = []
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        fields = line.split("\t")
        if len(fields) < 9:
            continue

        def cell(index: int) -> str | None:
            value = fields[index].strip() if index < len(fields) else ""
            return None if value in ("", "na") else value

        length = fields[8].strip()
        sequences.append(
            AssemblySequence(
                name=fields[0].strip(),
                role=fields[1].strip(),
                genbank=cell(4),
                refseq=cell(6),
                length=int(length) if length.isdigit() else None,
                unit=fields[7].strip() if len(fields) > 7 else "",
            )
        )
    return AssemblyReport(
        accession=accession, assembly_name=assembly_name, sequences=tuple(sequences)
    )
