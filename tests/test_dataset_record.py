"""Tests for `dataset.json` and the harvester that fills it.

The harvester is exercised against stub remotes rather than recorded HTTP
fixtures: what needs proving here is the *shape* of the record and that a
partial or absent publication produces an honest thin record rather than
a crash or a silently clean-looking one. The underlying readers have their
own tests.
"""

from __future__ import annotations

import json

import pytest

from congen.core.metadata.discovery import SpeciesRepo
from congen.core.remote.qc import (
    HET_FILE,
    IDEPTH_FILE,
    IMISS_FILE,
    QC_REPORT_FILE,
)
from congen.tools.readme.harvest import CALLABLE_SITES_DIR, RAW_VCF, Harvester
from congen.tools.readme.record import (
    RECORD_JSON,
    SCHEMA_VERSION,
    DatasetRecord,
    load_record,
)

from tests.conftest import METADATA_ROOT, REMOTE

PREFIX = "downstream_analyses/conservation_genomics/variant_calling/GCA_027172205.1"
ACCESSION = "GCA_027172205.1"


def qc_fixture(name: str) -> str:
    return (REMOTE / f"podarcis-raffonei.{name}").read_text()


class StubObject:
    def __init__(self, key, size=1, etag="etag"):
        self.key = key
        self.size = size
        self.etag = etag


class StubListing:
    def __init__(self, objects=(), subdirs=()):
        self.objects = tuple(objects)
        self.subdirs = tuple(subdirs)


class StubInventory:
    def __init__(self, top, bams, vcfs, qc):
        self.top = top
        self.bams = bams
        self.vcfs = vcfs
        self.qc = qc


class StubGenomeArk:
    """Enough of `GenomeArk` for the harvester, with no network."""

    def __init__(self, *, published=(ACCESSION,), objects=None, zarr=("depths.zarr",)):
        self.published = set(published)
        self.zarr = zarr
        self.objects = objects if objects is not None else self.default_objects()

    @staticmethod
    def default_objects():
        return [
            "README.txt",
            "sample_sheet.csv",
            "bams/SAMN18355762.bam",
            "bams/SAMN18355762.bam.csi",
            RAW_VCF,
            f"{RAW_VCF}.tbi",
            f"qc/{QC_REPORT_FILE}",
            f"qc/{IDEPTH_FILE}",
            f"qc/{HET_FILE}",
            f"qc/{IMISS_FILE}",
            f"{CALLABLE_SITES_DIR}/callable_sites.bed",
            f"{CALLABLE_SITES_DIR}/coverage_thresholds.tsv",
        ]

    def accessions(self):
        return sorted(self.published)

    def resolve_accession(self, declared, counterpart=None):
        if declared in self.published:
            return declared
        if counterpart and counterpart in self.published:
            return counterpart
        return None

    def accession_prefix(self, accession):
        return f"downstream_analyses/conservation_genomics/variant_calling/{accession}"

    def _listing(self, subdir):
        out = []
        for rel in self.objects:
            head, _, tail = rel.rpartition("/")
            if head == subdir and tail:
                out.append(StubObject(f"{self.accession_prefix(ACCESSION)}/{rel}", size=10))
        return StubListing(out)

    def inventory(self, accession):
        return StubInventory(
            top=self._listing(""),
            bams=self._listing("bams"),
            vcfs=self._listing("vcfs"),
            qc=self._listing("qc"),
        )

    def list(self, prefix, *, delimiter="/"):
        return StubListing(self._listing(CALLABLE_SITES_DIR).objects, subdirs=self.zarr)


class StubNcbi:
    class Info:
        organism_name = "Podarcis raffonei"
        tax_id = 65483
        assembly_name = "rPodRaf1.hap1"
        assembly_level = "Chromosome"
        paired_accession = "GCF_027172205.1"

    def assembly_info(self, accession):
        return self.Info()


class StubHeader:
    samples = ["SAMN18355762", "SAMN40540456"]
    contigs = {"chr1": 100, "chr2": 200}

    def callers(self):
        return {"gatk"}

    def tool_versions(self):
        return {"gatk": "4.6.2.0", "bcftools": "1.23.1"}

    def gatk_argument(self, name):
        return {"sample-ploidy": "2", "heterozygosity": "0.005"}.get(name)


@pytest.fixture
def species():
    return SpeciesRepo(METADATA_ROOT).load("reptiles/podarcis-raffonei")


@pytest.fixture
def harvester(monkeypatch):
    def fake_get_text(url):
        for name in (QC_REPORT_FILE, IDEPTH_FILE, HET_FILE, IMISS_FILE):
            if url.endswith(name):
                return qc_fixture(name)
        if url.endswith("coverage_thresholds.tsv"):
            return qc_fixture("coverage_thresholds.tsv")
        raise AssertionError(f"unexpected fetch: {url}")

    monkeypatch.setattr("congen.tools.readme.harvest.http.get_text", fake_get_text)
    monkeypatch.setattr(
        "congen.tools.readme.harvest.read_vcf_header", lambda url, **kw: StubHeader()
    )
    return Harvester(
        genomeark=StubGenomeArk(), ncbi=StubNcbi(), tool_version="test"
    )


class TestHarvestComplete:
    def test_it_records_the_resolved_accession_and_prefix(self, harvester, species):
        record = harvester.harvest(species)
        assert record.published
        assert record.accession == ACCESSION
        assert record.declared_accession == ACCESSION
        assert record.accession_matches
        assert record.prefix == PREFIX
        assert record.schema == SCHEMA_VERSION

    def test_it_merges_the_four_per_sample_tables(self, harvester, species):
        record = harvester.harvest(species)
        assert len(record.samples) == 21
        one = record.samples["SAMN18355762"]
        # from qc_report.tsv, individuals.idepth, .het and .imiss respectively
        assert one["mean_depth"] == pytest.approx(15.99)
        assert one["depth_at_called_sites"] == pytest.approx(13.9664)
        assert "f_inbreeding" in one
        assert "f_missing" in one

    def test_the_two_depths_are_stored_under_different_names(self, harvester, species):
        """Storing both under one name would make the disagreement invisible."""
        record = harvester.harvest(species)
        depths = record.metric("mean_depth")
        called = record.metric("depth_at_called_sites")
        assert set(depths) == set(called)
        assert depths["SAMN18355762"] != called["SAMN18355762"]

    def test_it_records_the_cohort_thresholds_and_site_count(self, harvester, species):
        record = harvester.harvest(species)
        assert record.cohort["mean_coverage"] == pytest.approx(17.390952)
        assert record.cohort["min_coverage"] == 8
        assert record.cohort["n_sites"] == 9439207

    def test_it_records_recorded_provenance_not_declared(self, harvester, species):
        record = harvester.harvest(species)
        assert record.vcf["callers"] == ["gatk"]
        assert record.vcf["ploidy"] == "2"
        assert record.vcf["n_contigs"] == 2
        # bcftools is in the header but is not a caller
        assert "bcftools" in record.vcf["tool_versions"]
        assert "bcftools" not in record.vcf["callers"]

    def test_it_records_the_opaque_prefixes(self, harvester, species):
        """Without these, any size derived from `objects` looks complete."""
        record = harvester.harvest(species)
        assert record.opaque_subdirs[CALLABLE_SITES_DIR] == ["depths.zarr"]

    def test_no_notes_on_a_clean_harvest(self, harvester, species):
        assert harvester.harvest(species).notes == []


class TestHarvestDegraded:
    def test_an_unpublished_species_yields_a_thin_record_that_says_why(self, species):
        harvester = Harvester(
            genomeark=StubGenomeArk(published=()), ncbi=StubNcbi(), tool_version="test"
        )
        record = harvester.harvest(species)
        assert not record.published
        assert record.objects == {}
        assert record.samples == {}
        assert record.notes and "no data published" in record.notes[0]

    def test_a_missing_vcf_is_noted_rather_than_silently_absent(self, harvester, species):
        harvester.genomeark.objects = [
            o for o in harvester.genomeark.objects if not o.startswith("vcfs/")
        ]
        record = harvester.harvest(species)
        assert record.vcf == {}
        assert any("raw.vcf.gz" in note for note in record.notes)

    def test_absent_qc_tables_produce_no_notes(self, harvester, species):
        """Absence here is normal, so it must not read as a problem."""
        harvester.genomeark.objects = [
            o for o in harvester.genomeark.objects if not o.startswith("qc/")
        ]
        record = harvester.harvest(species)
        assert record.samples == {}
        assert not [n for n in record.notes if "qc/" in n]

    def test_a_listing_failure_is_a_note_not_a_crash(self, harvester, species):
        def boom(prefix, **kw):
            raise OSError("S3 said no")

        harvester.genomeark.list = boom
        record = harvester.harvest(species)
        assert record.published
        assert any("callable_sites listing failed" in n for n in record.notes)

    def test_a_counterpart_accession_resolves_and_is_flagged(self, harvester, species):
        """The grus-americana case: config says GCF, GenomeArk says GCA."""
        harvester.genomeark.published = {"GCF_027172205.1"}
        record = harvester.harvest(species)
        assert record.accession == "GCF_027172205.1"
        assert record.declared_accession == ACCESSION
        assert not record.accession_matches


class TestRecordRoundTrip:
    def test_it_survives_json(self, harvester, species, tmp_path):
        record = harvester.harvest(species)
        path = tmp_path / RECORD_JSON
        assert record.write_json(path) is True
        again = load_record(path)
        assert again is not None
        assert again.as_dict() == record.as_dict()

    def test_writing_the_same_record_twice_changes_nothing(self, harvester, species, tmp_path):
        record = harvester.harvest(species)
        path = tmp_path / RECORD_JSON
        record.write_json(path)
        assert record.write_json(path) is False

    def test_a_later_harvest_of_unchanged_data_does_not_rewrite(
        self, harvester, species, tmp_path
    ):
        """The whole point of `substance()`.

        Without it, `harvested_at` alone makes every one of 79 records
        differ on every refresh, and the one species that actually moved
        is invisible in the diff.
        """
        path = tmp_path / RECORD_JSON
        first = harvester.harvest(species)
        first.write_json(path)

        later = harvester.harvest(species)
        later.harvested_at = "2099-01-01T00:00:00Z"
        later.tool_version = "99.0.0"
        assert later.write_json(path) is False
        # and the record keeps the timestamp of when the content was
        # first observed, not of when we last looked
        assert load_record(path).harvested_at == first.harvested_at

    def test_a_real_change_does_rewrite_and_restamps(self, harvester, species, tmp_path):
        path = tmp_path / RECORD_JSON
        first = harvester.harvest(species)
        first.write_json(path)

        later = harvester.harvest(species)
        later.harvested_at = "2099-01-01T00:00:00Z"
        later.objects["vcfs/filtered.vcf.gz"] = {"size": 1, "etag": "new"}
        assert later.write_json(path) is True
        assert load_record(path).harvested_at == "2099-01-01T00:00:00Z"

    def test_a_new_note_counts_as_a_change(self, harvester, species, tmp_path):
        path = tmp_path / RECORD_JSON
        harvester.harvest(species).write_json(path)
        later = harvester.harvest(species)
        later.notes = ["something went wrong this time"]
        assert later.write_json(path) is True

    def test_the_json_is_sorted_so_diffs_are_reviewable(self, harvester, species, tmp_path):
        record = harvester.harvest(species)
        path = tmp_path / RECORD_JSON
        record.write_json(path)
        payload = json.loads(path.read_text())
        assert list(payload) == sorted(payload)

    def test_an_unknown_field_is_ignored_rather_than_fatal(self):
        """Forward compatibility: a newer writer must not break an older reader."""
        record = DatasetRecord.from_dict(
            {"subject": "birds/x", "something_new": 1, "schema": 99}
        )
        assert record.subject == "birds/x"
        assert record.schema == 99

    def test_a_corrupt_file_loads_as_none(self, tmp_path):
        path = tmp_path / RECORD_JSON
        path.write_text("{not json")
        assert load_record(path) is None

    def test_a_missing_file_loads_as_none(self, tmp_path):
        assert load_record(tmp_path / "absent.json") is None


class TestDerivedSizes:
    def test_sizes_and_counts_filter_by_prefix(self, harvester, species):
        record = harvester.harvest(species)
        assert record.count_of("bams/") == 2
        assert record.size_of("bams/") == 20
        assert record.count_of() == len(record.objects)

    def test_metric_omits_samples_lacking_the_value(self, harvester, species):
        record = harvester.harvest(species)
        record.samples["ghost"] = {}
        assert "ghost" not in record.metric("mean_depth")
        assert len(record.samples) == 22
