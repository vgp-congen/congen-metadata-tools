"""Tests for the validate check catalog.

Contexts are built directly from fixtures, so no network is involved and
each check can be exercised against the exact situation it exists for.
The baseline cases at the bottom encode the findings the corpus actually
produces.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from congen.core.findings import Severity
from congen.core.metadata.discovery import SpeciesRepo
from congen.core.metadata.loaders import parse_sample_sheet
from congen.core.remote.genomeark import AccessionInventory, Listing, S3Object
from congen.core.remote.headers import FileByteSource, read_bam_header, read_vcf_header
from congen.core.remote.ncbi import AssemblyInfo
from congen.tools.validate import checks as _checks  # noqa: F401 - registers catalog
from congen.tools.validate.context import (
    BAM_HEADERS,
    CONFIG,
    NCBI,
    QC_SAMPLES,
    README,
    S3,
    S3_SHEET,
    SHEET,
    VCF_HEADER,
    VGP,
    Context,
)
from congen.tools.validate.registry import registry
from tests.conftest import FIXTURE_WINDOW, METADATA_ROOT, REMOTE

ALL_SLICES = {
    CONFIG,
    SHEET,
    README,
    VGP,
    S3,
    VCF_HEADER,
    BAM_HEADERS,
    QC_SAMPLES,
    S3_SHEET,
    NCBI,
}


@pytest.fixture
def repo() -> SpeciesRepo:
    return SpeciesRepo(METADATA_ROOT)


def make_inventory(
    accession: str,
    *,
    bams: list[str] | None = None,
    vcfs: list[str] | None = None,
    top: list[str] | None = None,
    qc: list[str] | None = None,
    subdirs: tuple[str, ...] = ("bams", "vcfs", "qc", "callable_sites"),
    empty: set[str] = frozenset(),
) -> AccessionInventory:
    prefix = f"downstream_analyses/conservation_genomics/variant_calling/{accession}"

    def listing(sub: str, names: list[str]) -> Listing:
        base = f"{prefix}/{sub}" if sub else prefix
        return Listing(
            prefix=f"{base}/",
            subdirs=subdirs if not sub else (),
            objects=tuple(
                S3Object(
                    key=f"{base}/{name}",
                    size=0 if name in empty else 1000,
                    etag="e",
                    last_modified="2026-01-01T00:00:00.000Z",
                )
                for name in names
            ),
        )

    bam_names: list[str] = []
    for sample in bams or []:
        bam_names += [f"{sample}.bam", f"{sample}.bam.csi"]

    return AccessionInventory(
        accession=accession,
        prefix=prefix,
        exists=True,
        top=listing("", top if top is not None else ["README.txt", "sample_sheet.csv"]),
        bams=listing("bams", bam_names),
        vcfs=listing("vcfs", vcfs if vcfs is not None else ["raw.vcf.gz", "raw.vcf.gz.tbi"]),
        qc=listing("qc", qc if qc is not None else ["individuals.samps.txt"]),
    )


def build(
    repo: SpeciesRepo,
    key: str,
    *,
    available: set[str] | None = None,
    **overrides,
) -> Context:
    species = repo.load(key)
    context = Context(species=species, vgp_list=repo.vgp_list)
    context.vgp_entry = repo.vgp_list.by_slug(species.slug)
    context.resolved_accession = overrides.pop("resolved_accession", species.reference.accession)
    for name, value in overrides.items():
        setattr(context, name, value)
    context.available = set(available) if available is not None else set(ALL_SLICES)
    return context


def run(context: Context, check_id: str):
    check = registry.get(check_id)
    assert check is not None, f"{check_id} is not registered"
    return registry.run(context, [check])


def ids_and_severities(findings):
    return [(f.id, f.severity) for f in findings]


# --------------------------------------------------------------------------
# Tier 0
# --------------------------------------------------------------------------


class TestTier0:
    def test_clean_config_and_sheet_are_silent(self, repo):
        context = build(repo, "reptiles/podarcis-raffonei")
        for check_id in ("R001", "R002", "R010", "R013", "R015", "R016", "R017", "R020"):
            assert run(context, check_id) == [], check_id

    def test_blank_row_is_reported_as_info(self, repo):
        context = build(repo, "birds/anser-albifrons")
        findings = run(context, "R010")
        assert ids_and_severities(findings) == [("R010", Severity.INFO)]
        assert findings[0].location.line == 14

    def test_accession_in_reference_name_warns(self, repo):
        context = build(repo, "birds/sturnus-vulgaris")
        findings = run(context, "R003")
        assert ids_and_severities(findings) == [("R003", Severity.WARN)]
        assert "GCF_001447265.1" in findings[0].message

    def test_missing_readme_is_info(self, repo):
        assert ids_and_severities(run(build(repo, "birds/sturnus-vulgaris"), "R021")) == [
            ("R021", Severity.INFO)
        ]
        assert run(build(repo, "reptiles/podarcis-raffonei"), "R021") == []

    def test_experiment_accessions_warn_but_do_not_error(self, repo, tmp_path):
        """SRX resolves, but an experiment can expand to several runs."""
        sheet = parse_sample_sheet(
            "sample_id,input_type,input\nSAMEA1,srr,ERX2249546\nSAMEA2,srr,ERR123\n",
            tmp_path / "sample_sheet.csv",
        )
        context = build(repo, "reptiles/podarcis-raffonei")
        context.species.sheet.rows = sheet.rows
        assert run(context, "R013") == []
        findings = run(context, "R017")
        assert ids_and_severities(findings) == [("R017", Severity.WARN)]
        assert "experiment" in findings[0].message

    def test_a_non_sra_input_errors(self, repo, tmp_path):
        sheet = parse_sample_sheet(
            "sample_id,input_type,input\nSAMEA1,srr,PRJNA12345\n",
            tmp_path / "sample_sheet.csv",
        )
        context = build(repo, "reptiles/podarcis-raffonei")
        context.species.sheet.rows = sheet.rows
        assert ids_and_severities(run(context, "R013")) == [("R013", Severity.ERROR)]

    def test_local_path_input_warns(self, repo, tmp_path):
        sheet = parse_sample_sheet(
            "sample_id,input_type,input\nSAMEA1,fastq,/n/scratch/a_1.fq.gz\n",
            tmp_path / "sample_sheet.csv",
        )
        context = build(repo, "reptiles/podarcis-raffonei")
        context.species.sheet.rows = sheet.rows
        assert ids_and_severities(run(context, "R015")) == [("R015", Severity.WARN)]


# --------------------------------------------------------------------------
# Tier 1
# --------------------------------------------------------------------------


class TestTier1:
    def test_complete_upload_is_silent(self, repo):
        context = build(
            repo,
            "reptiles/podarcis-raffonei",
            inventory=make_inventory(
                "GCA_027172205.1", bams=sorted(repo.load("reptiles/podarcis-raffonei").sheet.unique_sample_ids)
            ),
        )
        for check_id in ("G001", "G002", "G003", "G010", "G011", "G012", "G013", "G014", "G015"):
            assert run(context, check_id) == [], check_id

    def test_no_published_data_is_a_warning_not_an_error(self, repo):
        """A pending run is a normal state, but not a silent one."""
        context = build(
            repo, "birds/sturnus-vulgaris", resolved_accession=None, available={CONFIG}
        )
        findings = run(context, "G003")
        assert ids_and_severities(findings) == [("G003", Severity.WARN)]
        assert "no data published" in findings[0].message

    def test_downstream_tiers_skip_rather_than_error_without_data(self, repo):
        """The whole point of `needs`: no cascade of false mismatches."""
        context = build(
            repo, "birds/sturnus-vulgaris", resolved_accession=None, available={CONFIG, SHEET}
        )
        for check_id in ("S001", "S002", "S003", "G010", "G012"):
            findings = run(context, check_id)
            assert [f.severity for f in findings] == [Severity.SKIPPED], check_id

    def test_missing_vcf_errors_and_says_why(self, repo):
        context = build(
            repo,
            "birds/grus-americana",
            inventory=make_inventory("GCA_028858705.1", bams=["SAMEA1"], vcfs=[], subdirs=("bams",)),
        )
        findings = run(context, "G010")
        assert ids_and_severities(findings) == [("G010", Severity.ERROR)]
        assert "part-way uploaded" in findings[0].detail

    def test_accession_flip_warns_and_points_at_f021(self, repo):
        context = build(
            repo,
            "birds/grus-americana",
            resolved_accession="GCA_028858705.1",
            inventory=make_inventory("GCA_028858705.1", bams=["SAMEA1"]),
        )
        findings = run(context, "G002")
        assert ids_and_severities(findings) == [("G002", Severity.WARN)]
        assert "F021" in findings[0].detail

    def test_unindexed_bam_errors(self, repo):
        inventory = make_inventory("GCA_027172205.1", bams=["SAMN1"])
        inventory.bams = Listing(
            prefix=inventory.bams.prefix,
            objects=tuple(o for o in inventory.bams.objects if not o.name.endswith(".csi")),
        )
        findings = run(build(repo, "reptiles/podarcis-raffonei", inventory=inventory), "G013")
        assert ids_and_severities(findings) == [("G013", Severity.ERROR)]

    def test_zero_byte_data_object_errors(self, repo):
        inventory = make_inventory(
            "GCA_027172205.1", bams=["SAMN1"], empty={"raw.vcf.gz"}
        )
        findings = run(build(repo, "reptiles/podarcis-raffonei", inventory=inventory), "G014")
        assert ids_and_severities(findings) == [("G014", Severity.ERROR)]

    def test_empty_qc_filter_list_is_not_flagged(self, repo):
        """qc/ind_filter_list.txt is legitimately empty in 19 accessions."""
        inventory = make_inventory(
            "GCA_027172205.1",
            bams=["SAMN1"],
            qc=["individuals.samps.txt", "ind_filter_list.txt"],
            empty={"ind_filter_list.txt"},
        )
        assert run(build(repo, "reptiles/podarcis-raffonei", inventory=inventory), "G014") == []

    def test_filtered_vcf_is_recorded_never_judged(self, repo):
        """A default GATK output; says nothing about the config."""
        absent = build(repo, "reptiles/podarcis-raffonei", inventory=make_inventory("GCA_027172205.1", bams=["S"]))
        present = build(
            repo,
            "reptiles/podarcis-raffonei",
            inventory=make_inventory(
                "GCA_027172205.1",
                bams=["S"],
                vcfs=["raw.vcf.gz", "raw.vcf.gz.tbi", "filtered.vcf.gz"],
            ),
        )
        for context, word in ((absent, "absent"), (present, "present")):
            findings = run(context, "G016")
            assert ids_and_severities(findings) == [("G016", Severity.INFO)]
            assert word in findings[0].message


# --------------------------------------------------------------------------
# Tier 2
# --------------------------------------------------------------------------


class TestTier2:
    def test_matching_samples_are_silent(self, repo):
        species = repo.load("reptiles/podarcis-raffonei")
        samples = sorted(species.sheet.unique_sample_ids)
        context = build(
            repo,
            "reptiles/podarcis-raffonei",
            inventory=make_inventory("GCA_027172205.1", bams=samples),
            vcf_header=read_vcf_header(
                FileByteSource(REMOTE / "podarcis-raffonei.raw.vcf.gz.prefix"),
                initial_window=FIXTURE_WINDOW,
            ),
            qc_samples=samples,
        )
        for check_id in ("S001", "S002", "S003", "S005"):
            assert run(context, check_id) == [], check_id

    def test_repeated_sample_ids_do_not_cause_a_mismatch(self, repo):
        """42 of 79 sheets repeat a sample_id; row counts would lie."""
        species = repo.load("birds/anser-albifrons")
        assert len(species.sheet.rows) == 12
        assert len(species.sheet.unique_sample_ids) == 10
        context = build(
            repo,
            "birds/anser-albifrons",
            inventory=make_inventory(
                "GCA_976913865.1", bams=sorted(species.sheet.unique_sample_ids)
            ),
        )
        assert run(context, "S002") == []

    def test_the_anser_albifrons_mismatch(self, repo):
        """The sheet omits SAMEA112262514; the VCF and BAMs have it."""
        species = repo.load("birds/anser-albifrons")
        samples = sorted(species.sheet.unique_sample_ids | {"SAMEA112262514"})
        context = build(
            repo,
            "birds/anser-albifrons",
            inventory=make_inventory("GCA_976913865.1", bams=samples),
            vcf_header=read_vcf_header(
                FileByteSource(REMOTE / "anser-albifrons.raw.vcf.gz.prefix"),
                initial_window=FIXTURE_WINDOW,
            ),
        )
        for check_id in ("S001", "S002"):
            findings = run(context, check_id)
            assert ids_and_severities(findings) == [(check_id, Severity.ERROR)], check_id
            assert "SAMEA112262514" in findings[0].detail

    def test_mismatch_findings_assert_no_direction(self, repo):
        """Either side may be stale; that is a human's call, not ours."""
        species = repo.load("birds/anser-albifrons")
        context = build(
            repo,
            "birds/anser-albifrons",
            inventory=make_inventory(
                "GCA_976913865.1",
                bams=sorted(species.sheet.unique_sample_ids | {"SAMEA112262514"}),
            ),
        )
        detail = run(context, "S002")[0].detail
        assert "needs human review" in detail
        assert "either side may be the stale one" in detail

    def test_read_group_must_name_its_own_file(self, repo):
        good = read_bam_header(
            FileByteSource(REMOTE / "podarcis-raffonei.SAMN18355762.bam.prefix"),
            initial_window=FIXTURE_WINDOW,
        )
        context = build(repo, "reptiles/podarcis-raffonei", bam_headers={"SAMN18355762": good})
        assert run(context, "S004") == []

        mislabelled = build(repo, "reptiles/podarcis-raffonei", bam_headers={"SAMN99999999": good})
        findings = run(mislabelled, "S004")
        assert ids_and_severities(findings) == [("S004", Severity.ERROR)]

    def test_published_sheet_drift_warns(self, repo, tmp_path):
        published = parse_sample_sheet(
            "sample_id,input_type,input\nSAMN18355762,srr,SRR99999999\n",
            tmp_path / "sample_sheet.csv",
        )
        context = build(repo, "reptiles/podarcis-raffonei", s3_sheet=published)
        findings = run(context, "S006")
        assert ids_and_severities(findings) == [("S006", Severity.WARN)]

    def test_identical_published_sheet_is_silent(self, repo):
        species = repo.load("reptiles/podarcis-raffonei")
        context = build(repo, "reptiles/podarcis-raffonei", s3_sheet=species.sheet)
        assert run(context, "S006") == []


# --------------------------------------------------------------------------
# Tier 3b
# --------------------------------------------------------------------------


class TestTier3bCanonicality:
    def test_canonical_accession_is_silent(self, repo):
        context = build(repo, "reptiles/podarcis-raffonei")
        for check_id in ("F020", "F021", "F022", "F023"):
            assert run(context, check_id) == [], check_id

    def test_a_different_assembly_is_f020(self, repo):
        """sturnus-vulgaris names a 2015 scaffold assembly, not the VGP one."""
        context = build(
            repo,
            "birds/sturnus-vulgaris",
            ncbi_info=AssemblyInfo(
                accession="GCF_001447265.1",
                organism_name="Sturnus vulgaris",
                tax_id=9172,
                assembly_name="Sturnus_vulgaris-1.0",
                assembly_level="Scaffold",
                paired_accession="GCA_001447265.1",
            ),
        )
        findings = run(context, "F020")
        assert ids_and_severities(findings) == [("F020", Severity.ERROR)]
        assert "GCA_052056855.1" in findings[0].message
        assert "Scaffold" in findings[0].message
        assert run(context, "F021") == []

    def test_the_counterpart_accession_is_f021(self, repo):
        """grus-americana names the right assembly in the wrong namespace."""
        context = build(
            repo,
            "birds/grus-americana",
            ncbi_info=AssemblyInfo(
                accession="GCF_028858705.1",
                organism_name="Grus americana",
                tax_id=9117,
                paired_accession="GCA_028858705.1",
            ),
        )
        findings = run(context, "F021")
        assert ids_and_severities(findings) == [("F021", Severity.ERROR)]
        assert "GCA_028858705.1" in findings[0].message
        assert "confirmed by NCBI" in findings[0].detail
        assert run(context, "F020") == []

    def test_a_species_absent_from_the_list_warns(self, repo):
        context = build(repo, "reptiles/podarcis-raffonei")
        context.vgp_entry = None
        context.species.slug = "nonexistent-species"
        findings = run(context, "F022")
        assert ids_and_severities(findings) == [("F022", Severity.WARN)]
        assert "no entry" in findings[0].message

    def test_taxid_disagreement_warns(self, repo):
        context = build(
            repo,
            "reptiles/podarcis-raffonei",
            ncbi_info=AssemblyInfo(
                accession="GCA_027172205.1", organism_name="Podarcis raffonei", tax_id=99999
            ),
        )
        findings = run(context, "F024")
        assert ids_and_severities(findings) == [("F024", Severity.WARN)]
        assert "65483" in findings[0].message

    def test_matching_taxid_is_silent(self, repo):
        context = build(
            repo,
            "reptiles/podarcis-raffonei",
            ncbi_info=AssemblyInfo(
                accession="GCA_027172205.1", organism_name="Podarcis raffonei", tax_id=65483
            ),
        )
        assert run(context, "F024") == []


class TestCatalogHygiene:
    def test_every_check_declares_a_summary_and_tier(self):
        for check in registry.all:
            assert check.summary, check.id
            assert check.tier, check.id

    def test_ids_are_unique_and_well_formed(self):
        ids = [c.id for c in registry.all]
        assert len(ids) == len(set(ids))
        assert all(len(i) == 4 and i[0].isalpha() and i[1:].isdigit() for i in ids)

    def test_declared_needs_are_real_slices(self):
        for check in registry.all:
            for slice_name in check.needs:
                assert slice_name in ALL_SLICES, f"{check.id} needs unknown {slice_name!r}"
