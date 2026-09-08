"""The prefetched context every validate check reads.

All network access happens here, before any check runs. Checks are then
pure functions over this object, which is what lets the whole catalog be
tested offline against fixtures.

Each attribute a check can depend on is a named *slice*. ``Context.available``
holds the slices that were gathered successfully; a check declaring a slice
that is absent reports SKIPPED instead of failing.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Iterable

from congen.core import http
from congen.core.cache import Cache
from pathlib import Path

from congen.core.metadata.loaders import parse_sample_sheet_bytes
from congen.core.metadata.models import SampleSheet, SpeciesMetadata
from congen.core.metadata.vgp import VgpEntry, VgpReferenceList
from congen.core.remote.genomeark import AccessionInventory, GenomeArk
from congen.core.remote.headers import BamHeader, VcfHeader, read_bam_header, read_vcf_header
from congen.core.remote.ncbi import AssemblyInfo, AssemblyReport, Ncbi
from congen.core.remote.qc import ContigMap, parse_contig_map

# Slice names. Checks reference these in `needs=(...)`.
CONFIG = "config"
SHEET = "sheet"
README = "readme"
VGP = "vgp"
S3 = "s3"
VCF_HEADER = "vcf_header"
BAM_HEADERS = "bam_headers"
QC_SAMPLES = "qc_samples"
S3_SHEET = "s3_sheet"
NCBI = "ncbi"
ASSEMBLY_REPORT = "assembly_report"
CONTIG_MAP = "contig_map"

RAW_VCF = "raw.vcf.gz"
FILTERED_VCF = "filtered.vcf.gz"
QC_SAMPLES_FILE = "individuals.samps.txt"
CONTIG_MAP_FILE = "contig_map.tsv"

#: How many BAM headers to read when not reading all of them. One is
#: enough for @RG checks; a few more make the "all BAMs agree" check
#: meaningful without paying for 150 requests per species.
DEFAULT_BAM_SAMPLE = 3

#: Percentage of assembly bases that may be absent from a VCF before F009
#: escalates from informational to a warning. Measured across the corpus,
#: nothing is missing at all, so this is headroom rather than a filter.
DEFAULT_MISSING_THRESHOLD = 1.0


@dataclass
class Context:
    """Everything the checks for one species need."""

    species: SpeciesMetadata
    vgp_list: VgpReferenceList | None = None
    vgp_entry: VgpEntry | None = None

    inventory: AccessionInventory | None = None
    #: The accession the data was actually found under, which may be the
    #: GCA/GCF counterpart of what the config declares.
    resolved_accession: str | None = None

    vcf_header: VcfHeader | None = None
    bam_headers: dict[str, BamHeader] = field(default_factory=dict)
    #: True when `bam_headers` covers every BAM rather than a sample.
    bam_headers_complete: bool = False
    qc_samples: list[str] | None = None
    s3_sheet: SampleSheet | None = None
    ncbi_info: AssemblyInfo | None = None
    assembly_report: AssemblyReport | None = None
    contig_map: ContigMap | None = None

    #: Tunable read by F009.
    missing_contig_threshold: float = DEFAULT_MISSING_THRESHOLD

    available: set[str] = field(default_factory=set)
    gather_notes: list[str] = field(default_factory=list)

    @property
    def subject(self) -> str:
        return self.species.key

    @property
    def config(self):
        return self.species.config

    @property
    def sheet(self) -> SampleSheet:
        return self.species.sheet

    @property
    def readme(self):
        return self.species.readme

    @property
    def declared_accession(self) -> str | None:
        return self.species.reference.accession

    @property
    def accession_was_flipped(self) -> bool:
        declared = self.declared_accession
        return bool(
            self.resolved_accession and declared and self.resolved_accession != declared
        )


class ContextGatherer:
    """Builds a :class:`Context`, fetching only the slices asked for."""

    def __init__(
        self,
        *,
        cache: Cache | None = None,
        genomeark: GenomeArk | None = None,
        ncbi: Ncbi | None = None,
        bam_sample: int = DEFAULT_BAM_SAMPLE,
        all_bams: bool = False,
        missing_contig_threshold: float = DEFAULT_MISSING_THRESHOLD,
        seed: int = 0,
    ) -> None:
        self.cache = cache or Cache()
        self.genomeark = genomeark or GenomeArk(self.cache)
        self.ncbi = ncbi or Ncbi(self.cache)
        self.bam_sample = bam_sample
        self.all_bams = all_bams
        self.missing_contig_threshold = missing_contig_threshold
        self.seed = seed

    def gather(
        self,
        species: SpeciesMetadata,
        required: Iterable[str],
        *,
        vgp_list: VgpReferenceList | None = None,
    ) -> Context:
        required = set(required)
        context = Context(
            species=species,
            vgp_list=vgp_list,
            missing_contig_threshold=self.missing_contig_threshold,
        )

        if species.config.data:
            context.available.add(CONFIG)
        if species.sheet.columns:
            context.available.add(SHEET)
        if species.readme is not None:
            context.available.add(README)

        if VGP in required and vgp_list is not None and len(vgp_list):
            context.vgp_entry = vgp_list.by_slug(species.slug)
            context.available.add(VGP)

        if required & {NCBI, ASSEMBLY_REPORT}:
            self._gather_ncbi(context)
        if ASSEMBLY_REPORT in required:
            self._gather_assembly_report(context)

        needs_s3 = required & {
            S3,
            VCF_HEADER,
            BAM_HEADERS,
            QC_SAMPLES,
            S3_SHEET,
            CONTIG_MAP,
        }
        if needs_s3:
            self._gather_s3(context, required)

        return context

    def _gather_ncbi(self, context: Context) -> None:
        accession = context.declared_accession
        if not accession:
            return
        try:
            info = self.ncbi.assembly_info(accession)
        except Exception as exc:  # noqa: BLE001 - reported, not fatal
            context.gather_notes.append(f"NCBI lookup failed for {accession}: {exc}")
            return
        if info:
            context.ncbi_info = info
            context.available.add(NCBI)

    def _gather_assembly_report(self, context: Context) -> None:
        accession = context.declared_accession
        if not accession:
            return
        try:
            report = self.ncbi.assembly_report(accession)
        except Exception as exc:  # noqa: BLE001 - reported, not fatal
            context.gather_notes.append(f"assembly report failed for {accession}: {exc}")
            return
        if report and report.sequences:
            context.assembly_report = report
            context.available.add(ASSEMBLY_REPORT)

    def _gather_contig_map(self, context: Context) -> None:
        assert context.inventory
        obj = context.inventory.object("qc", CONTIG_MAP_FILE)
        if not obj or obj.is_empty:
            return
        try:
            text = http.get_text(obj.url)
        except Exception as exc:  # noqa: BLE001
            context.gather_notes.append(f"could not read {CONTIG_MAP_FILE}: {exc}")
            return
        contig_map = parse_contig_map(text)
        if contig_map.rows:
            context.contig_map = contig_map
            context.available.add(CONTIG_MAP)

    def _resolve_accession(self, context: Context) -> str | None:
        """Find the accession the data actually sits under.

        Tries what the config declares, then its GCA/GCF counterpart —
        the case `grus-americana` presents, where the config says GCF and
        GenomeArk publishes under GCA.
        """
        declared = context.declared_accession
        if not declared:
            return None
        published = set(self.genomeark.accessions())
        if declared in published:
            return declared
        counterpart = context.species.reference.counterpart()
        if counterpart and counterpart in published:
            return counterpart
        return None

    def _gather_s3(self, context: Context, required: set[str]) -> None:
        try:
            resolved = self._resolve_accession(context)
        except Exception as exc:  # noqa: BLE001
            context.gather_notes.append(f"GenomeArk listing failed: {exc}")
            return
        if not resolved:
            return

        context.resolved_accession = resolved
        try:
            inventory = self.genomeark.inventory(resolved)
        except Exception as exc:  # noqa: BLE001
            context.gather_notes.append(f"GenomeArk inventory failed: {exc}")
            return
        if not inventory.exists:
            return

        context.inventory = inventory
        context.available.add(S3)

        if VCF_HEADER in required:
            self._gather_vcf_header(context)
        if BAM_HEADERS in required:
            self._gather_bam_headers(context)
        if QC_SAMPLES in required:
            self._gather_qc_samples(context)
        if S3_SHEET in required:
            self._gather_s3_sheet(context)
        if CONTIG_MAP in required:
            self._gather_contig_map(context)

    def _gather_vcf_header(self, context: Context) -> None:
        assert context.inventory
        obj = context.inventory.object("vcfs", RAW_VCF)
        if not obj or obj.is_empty:
            return
        try:
            context.vcf_header = read_vcf_header(obj.url)
        except Exception as exc:  # noqa: BLE001
            context.gather_notes.append(f"could not read {RAW_VCF} header: {exc}")
            return
        context.available.add(VCF_HEADER)

    def _gather_bam_headers(self, context: Context) -> None:
        assert context.inventory
        objects = context.inventory.bam_objects
        if not objects:
            return

        names = sorted(objects)
        if self.all_bams or len(names) <= self.bam_sample:
            chosen = names
            complete = True
        else:
            # Always include the first for stability, then sample the rest
            # deterministically so repeated runs agree.
            rest = names[1:]
            rng = random.Random(f"{self.seed}:{context.subject}")
            chosen = [names[0]] + rng.sample(rest, min(self.bam_sample - 1, len(rest)))
            complete = False

        for name in chosen:
            try:
                context.bam_headers[name] = read_bam_header(objects[name].url)
            except Exception as exc:  # noqa: BLE001
                context.gather_notes.append(f"could not read {name}.bam header: {exc}")
        if context.bam_headers:
            context.bam_headers_complete = complete and len(context.bam_headers) == len(names)
            context.available.add(BAM_HEADERS)

    def _gather_qc_samples(self, context: Context) -> None:
        assert context.inventory
        obj = context.inventory.object("qc", QC_SAMPLES_FILE)
        if not obj or obj.is_empty:
            return
        try:
            text = http.get_text(obj.url)
        except Exception as exc:  # noqa: BLE001
            context.gather_notes.append(f"could not read {QC_SAMPLES_FILE}: {exc}")
            return
        context.qc_samples = [line.strip() for line in text.splitlines() if line.strip()]
        context.available.add(QC_SAMPLES)

    def _gather_s3_sheet(self, context: Context) -> None:
        assert context.inventory
        obj = context.inventory.object("sample_sheet.csv")
        if not obj or obj.is_empty:
            return
        try:
            body = http.get_bytes(obj.url)
        except Exception as exc:  # noqa: BLE001
            context.gather_notes.append(f"could not read published sample_sheet.csv: {exc}")
            return
        context.s3_sheet = parse_sample_sheet_bytes(body, Path(obj.key))
        context.available.add(S3_SHEET)
