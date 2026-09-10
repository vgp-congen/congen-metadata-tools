"""The network pass: everything `dataset.json` holds, fetched once.

Split from rendering at the CLI, not just internally, so `--refresh`
(network) and a bare render (offline) are separate invocations. That is
what lets CI wire them as two jobs on different triggers with different
permissions and no code change.

Harvesting is independent of the validation verdict. A species that fails
validation is harvested anyway: the numbers should be ready the moment it
is fixed, and nothing about fetching should depend on validation having
run first.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from congen.core import http
from congen.core.cache import Cache
from congen.core.metadata.models import SpeciesMetadata
from congen.core.remote.genomeark import BASE_URL, GenomeArk
from congen.core.remote.headers import HeaderError, read_vcf_header
from congen.core.remote.ncbi import Ncbi
from congen.core.remote.qc import (
    COVERAGE_THRESHOLDS_FILE,
    HET_FILE,
    IDEPTH_FILE,
    IMISS_FILE,
    QC_REPORT_FILE,
    parse_coverage_thresholds,
    parse_indv_table,
    parse_qc_report,
)
from congen.core.validation_record import utc_now

from .record import Assembly, Cohort, DatasetRecord, VcfProvenance

RAW_VCF = "vcfs/raw.vcf.gz"
QC_DIR = "qc"
CALLABLE_SITES_DIR = "callable_sites"

#: Which QC columns become which record field, stated explicitly rather
#: than dumping every column. Two tables report a "mean depth" over
#: different denominators, so the names have to disambiguate: `mean_depth`
#: is over mapped reads (what the dashboard plots, and what the document
#: reports) and `depth_at_called_sites` is vcftools' figure.
#:
#: A column absent from this map is not stored. Adding one is a one-line
#: change plus a re-harvest, which is cheaper than the alternative of
#: storing everything and having to decide later what any of it meant.
SAMPLE_METRICS: dict[str, dict[str, tuple[str, type]]] = {
    QC_REPORT_FILE: {
        "mean_depth": ("mean_depth", float),
        "percent_mapped": ("percent_mapped", float),
        "percent_duplicates": ("percent_duplicates", float),
        "percent_properly_paired": ("percent_properly_paired", float),
        "fraction_passed": ("fraction_passed", float),
        "total_reads": ("total_reads", int),
        "reads_before_filtering": ("reads_before_filtering", int),
        "reads_after_filtering": ("reads_after_filtering", int),
        "covered_bases": ("covered_bases", int),
    },
    IDEPTH_FILE: {
        "MEAN_DEPTH": ("depth_at_called_sites", float),
        "N_SITES": ("n_sites_called", int),
    },
    HET_FILE: {"F": ("f_inbreeding", float)},
    IMISS_FILE: {"F_MISS": ("f_missing", float), "N_MISS": ("n_missing", int)},
}

#: The per-sample tables, and where each lives relative to the accession.
QC_SOURCES = (
    (QC_REPORT_FILE, f"{QC_DIR}/{QC_REPORT_FILE}", parse_qc_report),
    (IDEPTH_FILE, f"{QC_DIR}/{IDEPTH_FILE}", parse_indv_table),
    (HET_FILE, f"{QC_DIR}/{HET_FILE}", parse_indv_table),
    (IMISS_FILE, f"{QC_DIR}/{IMISS_FILE}", parse_indv_table),
)

THRESHOLDS_PATH = f"{CALLABLE_SITES_DIR}/{COVERAGE_THRESHOLDS_FILE}"


@dataclass
class Harvester:
    genomeark: GenomeArk
    ncbi: Ncbi
    tool_version: str = ""
    #: Collected per species and copied onto the record, so a thin record
    #: says why it is thin instead of looking like a clean one.
    notes: list[str] = field(default_factory=list)

    @classmethod
    def build(cls, cache: Cache | None = None, *, tool_version: str = "") -> Harvester:
        shared = cache or Cache()
        return cls(
            genomeark=GenomeArk(shared), ncbi=Ncbi(shared), tool_version=tool_version
        )

    def harvest(self, species: SpeciesMetadata) -> DatasetRecord:
        self.notes = []
        declared = species.reference.accession
        record = DatasetRecord(
            subject=species.key,
            harvested_at=utc_now(),
            tool_version=self.tool_version,
            declared_accession=declared,
        )

        try:
            resolved = self.genomeark.resolve_accession(
                declared, species.reference.counterpart()
            )
        except Exception as exc:  # noqa: BLE001 - a listing failure is not fatal
            self.notes.append(f"GenomeArk listing failed: {exc}")
            resolved = None

        record.accession = resolved or declared
        if resolved is None:
            self.notes.append(
                "no data published on GenomeArk under "
                f"{declared or 'any declared accession'}"
            )
            record.notes = list(self.notes)
            return record

        record.prefix = self.genomeark.accession_prefix(resolved)
        record.published = True
        self._objects(record, resolved)
        self._qc(record)
        self._assembly(record, resolved)
        self._vcf(record)
        record.notes = list(self.notes)
        return record

    # -- object inventory ---------------------------------------------------

    def _objects(self, record: DatasetRecord, accession: str) -> None:
        prefix = record.prefix or ""
        try:
            inventory = self.genomeark.inventory(accession)
        except Exception as exc:  # noqa: BLE001
            self.notes.append(f"GenomeArk inventory failed: {exc}")
            return

        listings = [inventory.top, inventory.bams, inventory.vcfs, inventory.qc]

        # `inventory()` deliberately skips callable_sites, because it holds
        # zarr stores. List it delimited — the stores come back as prefixes
        # and are never descended into, which is why every size derived
        # from `objects` is a lower bound.
        try:
            callable_sites = self.genomeark.list(f"{prefix}/{CALLABLE_SITES_DIR}/")
        except Exception as exc:  # noqa: BLE001
            self.notes.append(f"callable_sites listing failed: {exc}")
        else:
            listings.append(callable_sites)
            if callable_sites.subdirs:
                record.opaque_subdirs[CALLABLE_SITES_DIR] = list(callable_sites.subdirs)

        for listing in listings:
            if listing is None:
                continue
            for obj in listing.objects:
                record.objects[obj.key[len(prefix) + 1 :]] = {
                    "size": obj.size,
                    "etag": obj.etag,
                }

    # -- QC tables ----------------------------------------------------------

    def _qc(self, record: DatasetRecord) -> None:
        cohort = Cohort()
        for name, path, parse in QC_SOURCES:
            text = self._fetch(record, path)
            if text is None:
                continue
            table = parse(text)
            if table.unkeyed:
                self.notes.append(f"{path}: {len(table.unkeyed)} rows without a sample id")
            for column, (field_name, kind) in SAMPLE_METRICS[name].items():
                values = table.ints(column) if kind is int else table.floats(column)
                for sample, value in values.items():
                    record.samples.setdefault(sample, {})[field_name] = value
            # N_DATA repeats the cohort site count on every row.
            if name == IMISS_FILE:
                sites = set(table.ints("N_DATA").values())
                if len(sites) == 1:
                    cohort.n_sites = sites.pop()
                elif sites:
                    self.notes.append(
                        f"{path}: N_DATA is not constant across samples ({len(sites)} values)"
                    )

        text = self._fetch(record, THRESHOLDS_PATH)
        if text is not None:
            thresholds = parse_coverage_thresholds(text)
            cohort.mean_coverage = thresholds.cohort_mean_coverage
            cohort.min_coverage = thresholds.min_coverage
            cohort.max_coverage = thresholds.max_coverage
        record.cohort = _asdict(cohort)

    def _fetch(self, record: DatasetRecord, path: str) -> str | None:
        """GET a small table, but only if the listing says it exists.

        Checking the listing first means an absent optional table costs
        nothing and produces no note — absence is normal here, not an
        error worth reporting.
        """
        if path not in record.objects:
            return None
        try:
            return http.get_text(f"{BASE_URL}/{record.prefix}/{path}")
        except Exception as exc:  # noqa: BLE001
            self.notes.append(f"{path}: fetch failed: {exc}")
            return None

    # -- NCBI and the VCF header -------------------------------------------

    def _assembly(self, record: DatasetRecord, accession: str) -> None:
        try:
            info = self.ncbi.assembly_info(accession)
        except Exception as exc:  # noqa: BLE001
            self.notes.append(f"NCBI assembly lookup failed: {exc}")
            return
        if info is None:
            self.notes.append(f"NCBI knows no assembly {accession}")
            return
        record.assembly = _asdict(
            Assembly(
                organism_name=info.organism_name,
                common_name=getattr(info, "common_name", None),
                tax_id=info.tax_id,
                assembly_name=info.assembly_name,
                assembly_level=info.assembly_level,
                paired_accession=info.paired_accession,
            )
        )

    def _vcf(self, record: DatasetRecord) -> None:
        if RAW_VCF not in record.objects:
            self.notes.append("no raw.vcf.gz published, so no recorded provenance")
            return
        try:
            header = read_vcf_header(f"{BASE_URL}/{record.prefix}/{RAW_VCF}")
        except (HeaderError, OSError) as exc:
            self.notes.append(f"{RAW_VCF}: header read failed: {exc}")
            return
        record.vcf = _asdict(
            VcfProvenance(
                samples=list(header.samples),
                n_contigs=len(header.contigs),
                callers=sorted(header.callers()),
                tool_versions=dict(sorted(header.tool_versions().items())),
                ploidy=header.gatk_argument("sample-ploidy"),
                het_prior=header.gatk_argument("heterozygosity"),
            )
        )


def _asdict(obj) -> dict:
    from dataclasses import asdict as _std

    return _std(obj)
