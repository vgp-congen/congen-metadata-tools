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
from congen.core.status import PublicationState, build_upload_status
from congen.tools.validate import checks as _checks  # noqa: F401 - registers catalog
from congen.tools.validate.context import (
    ASSEMBLY_REPORT,
    BAM_HEADERS,
    CONFIG,
    CONTIG_MAP,
    NCBI,
    SRA,
    STATUS,
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
    ASSEMBLY_REPORT,
    CONTIG_MAP,
    SRA,
    STATUS,
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
    context.upload_status = build_upload_status(
        subject=context.subject,
        declared_accession=context.declared_accession,
        resolved_accession=context.resolved_accession,
        inventory=context.inventory,
        sheet_sample_count=len(species.sheet.unique_sample_ids),
        vcf_sample_count=len(context.vcf_header.samples) if context.vcf_header else None,
        repo_readme=species.readme is not None,
    )
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
        for check_id in ("R001", "R002", "R010", "R013", "R015", "R016", "R020"):
            assert run(context, check_id) == [], check_id

    def test_a_blank_row_is_not_reported(self, repo):
        """The loader skips trailing blank lines; that is the right answer."""
        context = build(repo, "birds/anser-albifrons")
        assert any(i.code == "blank_row" for i in context.sheet.issues)
        assert run(context, "R010") == []

    def test_accession_in_reference_name_warns(self, repo):
        context = build(repo, "birds/sturnus-vulgaris")
        findings = run(context, "R003")
        assert ids_and_severities(findings) == [("R003", Severity.WARN)]
        assert "GCF_001447265.1" in findings[0].message

    def test_a_missing_readme_is_a_status_artifact_not_a_finding(self, repo):
        """R021 is retired: an absent optional file is not a defect."""
        assert "R021" not in registry
        assert "R021" in registry.retired
        assert not build(repo, "birds/sturnus-vulgaris").upload_status.has("repo_readme")
        assert build(repo, "reptiles/podarcis-raffonei").upload_status.has("repo_readme")

    def test_experiment_accessions_are_valid_sra_accessions(self, repo, tmp_path):
        """R013 accepts them; whether they are ambiguous is R017's job now.

        R017 moved to tier 5, because answering it needs SRA: offline it
        could only report the form of an accession and guess at the
        consequence. See tests/test_tier5.py.
        """
        sheet = parse_sample_sheet(
            "sample_id,input_type,input\nSAMEA1,srr,ERX2249546\nSAMEA2,srr,ERR123\n",
            tmp_path / "sample_sheet.csv",
        )
        context = build(repo, "reptiles/podarcis-raffonei")
        context.species.sheet.rows = sheet.rows
        assert run(context, "R013") == []
        assert SRA in registry.get("R017").needs

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
        for check_id in ("G001", "G010", "G011", "G012", "G013", "G014"):
            assert run(context, check_id) == [], check_id
        assert context.upload_status.state is PublicationState.COMPLETE

    def test_no_published_data_is_a_state_not_a_finding(self, repo):
        """G003 is retired: a pending run is a state, and states are not findings."""
        assert "G003" in registry.retired
        context = build(
            repo, "birds/sturnus-vulgaris", resolved_accession=None, available={CONFIG}
        )
        status = context.upload_status
        assert status.state is PublicationState.ABSENT
        assert status.missing  # every required artifact
        assert status.n_bams == 0

    def test_downstream_tiers_skip_rather_than_error_without_data(self, repo):
        """The whole point of `needs`: no cascade of false mismatches."""
        context = build(
            repo, "birds/sturnus-vulgaris", resolved_accession=None, available={CONFIG, SHEET}
        )
        for check_id in ("S001", "S002", "S003", "G010", "G012"):
            findings = run(context, check_id)
            assert [f.severity for f in findings] == [Severity.SKIPPED], check_id

    def test_missing_vcf_warns_and_says_what_is_there(self, repo):
        context = build(
            repo,
            "birds/grus-americana",
            inventory=make_inventory("GCA_028858705.1", bams=["SAMEA1"], vcfs=[], subdirs=("bams",)),
        )
        findings = run(context, "G010")
        # A warning, not an error: an incomplete upload may be mid-flight.
        assert ids_and_severities(findings) == [("G010", Severity.WARN)]
        # Facts only: no inference about why the VCF is absent.
        assert findings[0].detail == "1 BAM(s) present, no VCF"
        assert context.upload_status.state is PublicationState.PARTIAL

    def test_an_accession_flip_is_recorded_in_the_status(self, repo):
        """G002 is retired: where data was found is a fact, F021 is the claim."""
        assert "G002" in registry.retired
        context = build(
            repo,
            "birds/grus-americana",
            resolved_accession="GCA_028858705.1",
            inventory=make_inventory("GCA_028858705.1", bams=["SAMEA1"]),
        )
        status = context.upload_status
        assert status.accession == "GCA_028858705.1"
        assert status.declared_accession == "GCF_028858705.1"
        assert status.accession_differs

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

    def test_filtered_vcf_is_an_optional_status_artifact(self, repo):
        """G016 is retired: an optional file's presence is not a finding."""
        assert "G016" in registry.retired
        absent = build(
            repo,
            "reptiles/podarcis-raffonei",
            inventory=make_inventory("GCA_027172205.1", bams=["S"]),
        )
        present = build(
            repo,
            "reptiles/podarcis-raffonei",
            inventory=make_inventory(
                "GCA_027172205.1",
                bams=["S"],
                vcfs=["raw.vcf.gz", "raw.vcf.gz.tbi", "filtered.vcf.gz"],
            ),
        )
        assert not absent.upload_status.has("filtered_vcf")
        assert present.upload_status.has("filtered_vcf")
        # Neither state affects completeness.
        assert absent.upload_status.state is PublicationState.COMPLETE
        assert present.upload_status.state is PublicationState.COMPLETE


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
        assert context.upload_status.state is PublicationState.COMPLETE
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
        assert ids_and_severities(findings) == [("F022", Severity.ERROR)]
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

    def test_findings_use_the_severity_the_check_declares(self):
        """A decorator saying warn while the Finding says error is a bug.

        G010 shipped that way for one commit: demoting the check did not
        demote the finding it constructs.
        """
        import inspect
        import re

        for check in registry.all:
            source = inspect.getsource(check.func)
            declared = check.severity.name
            constructed = set(re.findall(r"severity=Severity\.(\w+)", source))
            # A check may legitimately vary severity (F009 warns or informs);
            # what it must not do is construct a severity it never declares
            # while declaring one it never constructs.
            if len(constructed) == 1:
                assert constructed == {declared}, (
                    f"{check.id} declares {declared} but constructs {constructed}"
                )

    def test_declared_needs_are_real_slices(self):
        for check in registry.all:
            for slice_name in check.needs:
                assert slice_name in ALL_SLICES, f"{check.id} needs unknown {slice_name!r}"


class TestStatusModel:
    """Publication state is a description, not a defect."""

    def test_absent_when_nothing_is_published(self):
        from congen.core.status import REQUIRED_ARTIFACTS, PublicationState, build_upload_status

        status = build_upload_status(
            subject="birds/x",
            declared_accession="GCA_1.1",
            resolved_accession=None,
            sheet_sample_count=12,
        )
        assert status.state is PublicationState.ABSENT
        assert status.missing == REQUIRED_ARTIFACTS
        assert status.n_bams == 0
        assert not status.is_complete

    def test_complete_when_every_required_artifact_exists(self, metadata_root):
        from congen.core.status import PublicationState, build_upload_status
        from tests.test_validate_checks import make_inventory

        status = build_upload_status(
            subject="birds/x",
            declared_accession="GCA_1.1",
            resolved_accession="GCA_1.1",
            inventory=make_inventory("GCA_1.1", bams=["S1", "S2"]),
            sheet_sample_count=2,
            vcf_sample_count=2,
        )
        assert status.state is PublicationState.COMPLETE
        assert status.missing == ()
        assert status.n_bams == 2

    def test_partial_when_a_required_artifact_is_missing(self):
        from congen.core.status import PublicationState, build_upload_status
        from tests.test_validate_checks import make_inventory

        status = build_upload_status(
            subject="birds/x",
            declared_accession="GCA_1.1",
            resolved_accession="GCA_1.1",
            inventory=make_inventory("GCA_1.1", bams=["S1"], vcfs=[], subdirs=("bams",)),
            sheet_sample_count=1,
        )
        assert status.state is PublicationState.PARTIAL
        assert "raw_vcf" in status.missing

    def test_optional_artifacts_never_affect_completeness(self):
        from congen.core.status import PublicationState, build_upload_status
        from tests.test_validate_checks import make_inventory

        status = build_upload_status(
            subject="birds/x",
            declared_accession="GCA_1.1",
            resolved_accession="GCA_1.1",
            inventory=make_inventory("GCA_1.1", bams=["S1"], top=[]),
            sheet_sample_count=1,
            repo_readme=False,
        )
        assert status.state is PublicationState.COMPLETE
        assert not status.has("filtered_vcf")
        assert not status.has("published_readme")
        assert not status.has("repo_readme")

    def test_records_a_differing_accession(self):
        from congen.core.status import build_upload_status
        from tests.test_validate_checks import make_inventory

        status = build_upload_status(
            subject="birds/grus-americana",
            declared_accession="GCF_028858705.1",
            resolved_accession="GCA_028858705.1",
            inventory=make_inventory("GCA_028858705.1", bams=["S1"]),
        )
        assert status.accession_differs
        assert status.as_dict()["accession_differs"] is True

    def test_matching_accessions_do_not_differ(self):
        from congen.core.status import build_upload_status

        status = build_upload_status(
            subject="x", declared_accession="GCA_1.1", resolved_accession="GCA_1.1"
        )
        assert not status.accession_differs


class TestPartialUploadGuards:
    """A partial upload's BAM set is not final, so comparing it measures nothing."""

    def _grus_like(self, repo, *, complete: bool):
        from tests.test_validate_checks import build, make_inventory

        species = repo.load("birds/anser-albifrons")
        published = sorted(species.sheet.unique_sample_ids)[:5]
        inventory = (
            make_inventory("GCA_976913865.1", bams=published)
            if complete
            else make_inventory(
                "GCA_976913865.1", bams=published, vcfs=[], subdirs=("bams",)
            )
        )
        return build(repo, "birds/anser-albifrons", inventory=inventory)

    def test_s002_is_silent_during_a_partial_upload(self, repo):
        from congen.core.status import PublicationState

        context = self._grus_like(repo, complete=False)
        assert context.upload_status.state is PublicationState.PARTIAL
        assert run(context, "S002") == []

    def test_s002_fires_once_the_upload_is_complete(self, repo):
        from congen.core.status import PublicationState

        context = self._grus_like(repo, complete=True)
        assert context.upload_status.state is PublicationState.COMPLETE
        assert ids_and_severities(run(context, "S002")) == [("S002", Severity.ERROR)]

    def test_s003_is_silent_during_a_partial_upload(self, repo, metadata_root):
        from congen.core.remote.headers import FileByteSource, read_vcf_header
        from tests.conftest import FIXTURE_WINDOW, REMOTE
        from tests.test_validate_checks import build, make_inventory

        header = read_vcf_header(
            FileByteSource(REMOTE / "anser-albifrons.raw.vcf.gz.prefix"),
            initial_window=FIXTURE_WINDOW,
        )
        context = build(
            repo,
            "birds/anser-albifrons",
            inventory=make_inventory(
                "GCA_976913865.1", bams=["SAMEA112262509"], vcfs=[], subdirs=("bams",)
            ),
            vcf_header=header,
        )
        assert run(context, "S003") == []


class TestRetirement:
    def test_retired_ids_are_not_registered(self):
        for check_id in ("G002", "G003", "G015", "G016", "R021", "P004", "P005"):
            assert check_id not in registry, check_id
            assert check_id in registry.retired, check_id

    def test_reusing_a_retired_id_is_refused(self):
        from congen.core.findings import Severity as Sev

        with pytest.raises(ValueError, match="retired"):

            @registry.register(id="G003", tier="G", severity=Sev.INFO, summary="x")
            def resurrect(context):
                return []


class TestStatusIsOnlyClaimedWhenChecked:
    """Reporting "absent" because nobody looked would be a lie."""

    def test_no_status_when_s3_was_not_consulted(self, repo):
        from congen.core.cache import Cache
        from congen.tools.validate.context import ContextGatherer

        gatherer = ContextGatherer(cache=Cache(enabled=False))
        context = gatherer.gather(repo.load("reptiles/podarcis-raffonei"), {"config", "sheet"})
        assert context.upload_status is None

    def test_status_is_claimed_when_s3_was_consulted(self, repo, monkeypatch):
        from congen.core.cache import Cache
        from congen.core.status import PublicationState
        from congen.tools.validate.context import ContextGatherer

        gatherer = ContextGatherer(cache=Cache(enabled=False))
        monkeypatch.setattr(gatherer.genomeark, "accessions", lambda: [])
        context = gatherer.gather(repo.load("reptiles/podarcis-raffonei"), {"s3"})
        assert context.upload_status is not None
        assert context.upload_status.state is PublicationState.ABSENT


class TestReadmeChecks:
    """G017 and G018 split what was a bare "no README" note."""

    def _context(self, repo, key, *, published: str | None, available=None):
        context = build(
            repo,
            key,
            inventory=make_inventory("GCA_1.1", bams=["S1"]),
            published_readme=published,
            available=available if available is not None else {S3, STATUS, CONFIG},
        )
        return context

    def test_matching_readmes_are_silent(self, repo):
        species = repo.load("reptiles/podarcis-raffonei")
        context = self._context(
            repo, "reptiles/podarcis-raffonei", published=species.readme.text
        )
        assert run(context, "G017") == []
        assert run(context, "G018") == []

    def test_published_but_not_in_the_repo(self, repo):
        """Eleven species today: the file exists, nobody copied it back."""
        context = self._context(
            repo, "birds/sturnus-vulgaris", published="Species: Sturnus vulgaris\n"
        )
        findings = run(context, "G017")
        assert ids_and_severities(findings) == [("G017", Severity.WARN)]
        assert "not in the repo" in findings[0].message
        assert "copy it" in findings[0].detail

    def test_in_the_repo_but_not_published(self, repo):
        context = self._context(repo, "reptiles/podarcis-raffonei", published=None)
        findings = run(context, "G017")
        assert ids_and_severities(findings) == [("G017", Severity.WARN)]
        assert "does not publish" in findings[0].message

    def test_both_present_but_differing(self, repo):
        context = self._context(
            repo, "reptiles/podarcis-raffonei", published="Species: Something else\n"
        )
        findings = run(context, "G017")
        assert ids_and_severities(findings) == [("G017", Severity.WARN)]
        assert "differ" in findings[0].message

    def test_trailing_whitespace_is_not_a_difference(self, repo):
        species = repo.load("reptiles/podarcis-raffonei")
        context = self._context(
            repo, "reptiles/podarcis-raffonei", published=species.readme.text + "\n\n"
        )
        assert run(context, "G017") == []

    def test_neither_side_is_g018_not_g017(self, repo):
        """G017 would read "no copies" as trivially in sync."""
        context = self._context(repo, "birds/sturnus-vulgaris", published=None)
        assert run(context, "G017") == []
        findings = run(context, "G018")
        assert ids_and_severities(findings) == [("G018", Severity.WARN)]
        assert "which bioprojects" in findings[0].detail

    def test_g018_fires_for_an_unpublished_species(self, repo):
        """Writing one does not need the data: the bioprojects are in the sheet."""
        context = build(
            repo,
            "birds/sturnus-vulgaris",
            resolved_accession=None,
            available={CONFIG, STATUS},
        )
        assert ids_and_severities(run(context, "G018")) == [("G018", Severity.WARN)]

    def test_g018_is_skipped_when_genomeark_was_not_consulted(self, repo):
        """Otherwise it would claim "nowhere" having looked in one place."""
        context = build(repo, "birds/sturnus-vulgaris", available={CONFIG})
        findings = registry.run(context, [registry.get("G018")])
        assert [f.severity for f in findings] == [Severity.SKIPPED]
