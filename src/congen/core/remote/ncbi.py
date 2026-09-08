"""NCBI Datasets lookups.

Milestone 2 needs only the dataset report: organism, taxonomy ID, and the
paired GCA/GCF accession. The paired accession is what turns a syntactic
GCA-versus-GCF flip into a confirmed statement that two accessions name
the same assembly.

Assembly reports (the per-sequence table behind tier 3a) land in
milestone 3.
"""

from __future__ import annotations

from dataclasses import dataclass

from congen.core import http
from congen.core.cache import Cache

DATASETS_API = "https://api.ncbi.nlm.nih.gov/datasets/v2alpha"

#: Assembly metadata is immutable for a given accession, so entries never
#: need to expire.
NAMESPACE = "ncbi-dataset-report"


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
