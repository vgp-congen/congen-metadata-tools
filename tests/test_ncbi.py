"""Tests for the NCBI Datasets client, from recorded reports."""

from __future__ import annotations

import json

import pytest

from congen.core.cache import Cache
from congen.core.remote import ncbi as ncbi_module
from congen.core.remote.ncbi import AssemblyInfo, Ncbi
from tests.conftest import REMOTE

RECORDED = {
    "GCA_027172205.1": "ncbi_GCA_027172205_1.json",
    "GCF_028858705.1": "ncbi_GCF_028858705_1.json",
    "GCF_001447265.1": "ncbi_GCF_001447265_1.json",
}


@pytest.fixture
def offline(monkeypatch, tmp_path):
    requested: list[str] = []

    def fake_get_json(url: str, **kwargs):
        requested.append(url)
        for accession, name in RECORDED.items():
            if accession in url:
                return json.loads((REMOTE / name).read_text())
        return {"reports": []}

    monkeypatch.setattr(ncbi_module.http, "get_json", fake_get_json)
    client = Ncbi(Cache(directory=tmp_path))
    client.requested = requested  # type: ignore[attr-defined]
    return client


class TestAssemblyInfo:
    def test_parses_organism_and_assembly(self, offline):
        info = offline.assembly_info("GCA_027172205.1")
        assert info == AssemblyInfo(
            accession="GCA_027172205.1",
            organism_name="Podarcis raffonei",
            tax_id=65483,
            assembly_name="rPodRaf1.pri",
            assembly_level="Chromosome",
            paired_accession="GCF_027172205.1",
        )

    def test_paired_accession_confirms_the_gca_gcf_pairing(self, offline):
        """This is what turns a syntactic flip into a confirmed one."""
        info = offline.assembly_info("GCF_028858705.1")
        assert info.paired_accession == "GCA_028858705.1"
        assert info.names_same_assembly_as("GCA_028858705.1")
        assert info.names_same_assembly_as("GCF_028858705.1")
        assert not info.names_same_assembly_as("GCA_052056855.1")
        assert not info.names_same_assembly_as(None)

    def test_reports_assembly_level(self, offline):
        """sturnus-vulgaris declares a 2015 scaffold-level assembly."""
        info = offline.assembly_info("GCF_001447265.1")
        assert info.organism_name == "Sturnus vulgaris"
        assert info.assembly_level == "Scaffold"
        assert info.assembly_name == "Sturnus_vulgaris-1.0"

    def test_unknown_accession_is_none(self, offline):
        assert offline.assembly_info("GCA_000000000.9") is None

    def test_lookups_are_cached_including_misses(self, offline):
        offline.assembly_info("GCA_027172205.1")
        offline.assembly_info("GCA_000000000.9")
        before = len(offline.requested)
        offline.assembly_info("GCA_027172205.1")
        offline.assembly_info("GCA_000000000.9")
        assert len(offline.requested) == before

    def test_taxid_survives_being_a_string(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            ncbi_module.http,
            "get_json",
            lambda url, **kw: {
                "reports": [
                    {"accession": "GCA_1.1", "organism": {"tax_id": "1234"}, "assembly_info": {}}
                ]
            },
        )
        assert Ncbi(Cache(directory=tmp_path)).assembly_info("GCA_1.1").tax_id == 1234

    def test_a_nonsense_taxid_becomes_none(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            ncbi_module.http,
            "get_json",
            lambda url, **kw: {
                "reports": [
                    {"accession": "GCA_1.1", "organism": {"tax_id": "n/a"}, "assembly_info": {}}
                ]
            },
        )
        assert Ncbi(Cache(directory=tmp_path)).assembly_info("GCA_1.1").tax_id is None


class TestAssemblyReportFetching:
    """The FTP path is constructed, not scraped."""

    def test_sanitizes_assembly_names_the_way_ncbi_does(self):
        from congen.core.remote.ncbi import sanitize_assembly_name

        # Verified against the real FTP directory names for the two
        # corpus assemblies whose names need it.
        assert sanitize_assembly_name("mEubGla1.1.hap2.+ XY") == "mEubGla1.1.hap2._XY"
        assert sanitize_assembly_name("mPanOnc1 haplotype 2") == "mPanOnc1_haplotype_2"
        assert sanitize_assembly_name("rPodRaf1.pri") == "rPodRaf1.pri"

    def test_builds_the_sharded_ftp_path(self, offline):
        url = offline._report_url("GCA_027172205.1", "rPodRaf1.pri")
        assert url == (
            "https://ftp.ncbi.nlm.nih.gov/genomes/all/GCA/027/172/205/"
            "GCA_027172205.1_rPodRaf1.pri/GCA_027172205.1_rPodRaf1.pri_assembly_report.txt"
        )

    def test_falls_back_to_the_directory_listing(self, monkeypatch, tmp_path):
        """If the naming rule ever shifts, ask the directory."""
        from congen.core.cache import Cache

        listing = (
            '<html><a href="GCA_027172205.1_someOtherName/">x</a></html>'
        )
        report_text = (REMOTE / "assembly_report_GCA_027172205_1.txt").read_text()
        calls: list[str] = []

        def fake_get_text(url: str, **kwargs) -> str:
            calls.append(url)
            if url.endswith("_assembly_report.txt"):
                if "someOtherName" in url:
                    return report_text
                raise ncbi_module.http.HttpError(url, 404, "Not Found")
            return listing

        monkeypatch.setattr(ncbi_module.http, "get_text", fake_get_text)
        monkeypatch.setattr(
            ncbi_module.http,
            "get_json",
            lambda url, **kw: json.loads((REMOTE / "ncbi_GCA_027172205_1.json").read_text()),
        )
        report = Ncbi(Cache(directory=tmp_path)).assembly_report("GCA_027172205.1")
        assert report is not None
        assert len(report.sequences) == 27
        assert any("someOtherName" in c for c in calls)

    def test_a_missing_report_caches_the_miss(self, monkeypatch, tmp_path):
        from congen.core.cache import Cache

        calls: list[str] = []

        def boom(url: str, **kwargs):
            calls.append(url)
            raise ncbi_module.http.HttpError(url, 404, "Not Found")

        monkeypatch.setattr(ncbi_module.http, "get_text", boom)
        monkeypatch.setattr(
            ncbi_module.http,
            "get_json",
            lambda url, **kw: json.loads((REMOTE / "ncbi_GCA_027172205_1.json").read_text()),
        )
        client = Ncbi(Cache(directory=tmp_path))
        assert client.assembly_report("GCA_027172205.1") is None
        before = len(calls)
        assert client.assembly_report("GCA_027172205.1") is None
        assert len(calls) == before
