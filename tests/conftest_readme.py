"""Synthetic render contexts, shared by the renderer tests.

Synthetic rather than the real corpus: the suite must run offline, and
the corpus legitimately changes whenever a pipeline run finishes. What
these prove is that each document shape is reachable and stable.
"""

from __future__ import annotations

from pathlib import Path

from congen.core.metadata.discovery import SpeciesRepo
from congen.core.validation_record import ReportState, ValidationRecord
from congen.tools.readme.gate import evaluate
from congen.tools.readme.harvest import CALLABLE_SITES_DIR
from congen.tools.readme.record import DatasetRecord
from congen.tools.readme.render import build_context

from tests.conftest import METADATA_ROOT, REMOTE

ACCESSION = "GCA_027172205.1"
PREFIX = f"downstream_analyses/conservation_genomics/variant_calling/{ACCESSION}"

OBJECTS = {
    "README.txt": 202,
    "sample_sheet.csv": 574,
    "vcfs/raw.vcf.gz": 4_244_983_182,
    "vcfs/raw.vcf.gz.tbi": 1_248_114,
    "qc/qc_dashboard.html": 8_998_944,
    "qc/qc_report.tsv": 2_149,
    "qc/individuals.idepth": 630,
    "qc/individuals.het": 1_016,
    "qc/individuals.imiss": 886,
    "qc/individuals.samps.txt": 273,
    "qc/contig_map.tsv": 806,
    "qc/plink.bed": 603_603,
    f"{CALLABLE_SITES_DIR}/callable_sites.bed": 9_831_310,
    f"{CALLABLE_SITES_DIR}/coverage.bed": 35_015_877,
    f"{CALLABLE_SITES_DIR}/mappability.bed": 5_031_869,
    f"{CALLABLE_SITES_DIR}/coverage_thresholds.tsv": 62,
}
for i in range(3):
    OBJECTS[f"bams/SAMN1835576{i}.bam"] = 60_000_000_000
    OBJECTS[f"bams/SAMN1835576{i}.bam.csi"] = 1_000


def _samples() -> dict[str, dict]:
    """Real QC numbers, from the recorded podarcis-raffonei tables."""
    from congen.core.remote.qc import parse_indv_table, parse_qc_report

    report = parse_qc_report((REMOTE / "podarcis-raffonei.qc_report.tsv").read_text())
    het = parse_indv_table((REMOTE / "podarcis-raffonei.individuals.het").read_text())
    imiss = parse_indv_table((REMOTE / "podarcis-raffonei.individuals.imiss").read_text())
    out: dict[str, dict] = {}
    for sample, value in report.floats("mean_depth").items():
        out.setdefault(sample, {})["mean_depth"] = value
    for column, name in (("percent_mapped", "percent_mapped"), ("percent_duplicates", "percent_duplicates")):
        for sample, value in report.floats(column).items():
            out.setdefault(sample, {})[name] = value
    for sample, value in het.floats("F").items():
        out.setdefault(sample, {})["f_inbreeding"] = value
    for sample, value in imiss.floats("F_MISS").items():
        out.setdefault(sample, {})["f_missing"] = value
    return out


def dataset(**overrides) -> DatasetRecord:
    payload = dict(
        subject="reptiles/podarcis-raffonei",
        harvested_at="2026-09-10T00:00:00Z",
        tool_version="test",
        accession=ACCESSION,
        declared_accession=ACCESSION,
        prefix=PREFIX,
        published=True,
        objects={path: {"size": size, "etag": f"etag-{path}"} for path, size in OBJECTS.items()},
        opaque_subdirs={CALLABLE_SITES_DIR: ["callable_loci.zarr", "depths.zarr"]},
        assembly={
            "organism_name": "Podarcis raffonei",
            "common_name": None,
            "tax_id": 65483,
            "assembly_name": "rPodRaf1.hap1",
            "assembly_level": "Chromosome",
            "paired_accession": "GCF_027172205.1",
        },
        vcf={
            "samples": sorted(_samples()),
            "n_contigs": 24,
            "callers": ["gatk"],
            "tool_versions": {"bcftools": "1.23.1", "gatk": "4.6.2.0"},
            "ploidy": "2",
            "het_prior": "0.005",
        },
        cohort={
            "mean_coverage": 17.390952,
            "min_coverage": 8.0,
            "max_coverage": 35.0,
            "n_sites": 9_439_207,
        },
        samples=_samples(),
    )
    payload.update(overrides)
    return DatasetRecord(**payload)


def validation(**overrides) -> ValidationRecord:
    payload = dict(
        subject="reptiles/podarcis-raffonei",
        state=ReportState.PASS,
        validated_at="2026-09-10T00:00:00Z",
        tool_version="test",
        catalog="sha256:deadbeef",
        checks_run=["G017", "G018", "R020", "E001", "E002", "E003", "S001"],
        checks_available=7,
        inputs={},
        data={"accession": ACCESSION, "published": True},
        findings=[],
        status={
            "state": "complete",
            "accession": ACCESSION,
            "missing": [],
            "counts": {"sheet_samples": 21, "bams": 21, "vcf_samples": 21},
        },
    )
    payload.update(overrides)
    return ValidationRecord(**payload)


def context(species_dir: Path | None = None, *, record=None, data=None):
    repo = SpeciesRepo(METADATA_ROOT)
    species = repo.load("reptiles/podarcis-raffonei")
    record = validation() if record is None else record
    data = dataset() if data is None else data
    verdict = evaluate(
        species_dir or species.path, record, harvested_accession=data.accession
    )
    return build_context(species, data, verdict, record, vgp=repo.vgp_list)
