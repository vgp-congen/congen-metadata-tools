"""Tests for the NCBI SRA client, from recorded runinfo."""

from __future__ import annotations

import json

import pytest

from congen.core.cache import Cache
from congen.core.remote import sra as sra_module
from congen.core.remote.sra import RunInfo, Sra, parse_runinfo
from tests.conftest import REMOTE

RUNINFO = (REMOTE / "sra_runinfo.csv").read_text()
ESEARCH = (REMOTE / "sra_esearch.json").read_text()


@pytest.fixture
def offline(monkeypatch, tmp_path):
    """Serve recorded eutils responses; count the round trips."""
    calls: list[tuple[str, dict]] = []

    def fake_post_text(url: str, fields: dict, **kwargs) -> str:
        calls.append((url, fields))
        if "esearch" in url:
            return ESEARCH
        return RUNINFO

    monkeypatch.setattr(sra_module.http, "post_text", fake_post_text)
    client = Sra(Cache(directory=tmp_path))
    client.calls = calls  # type: ignore[attr-defined]
    return client


class TestParseRuninfo:
    def test_reads_columns_by_name(self):
        """47 columns; only the names are a stable contract."""
        rows = {r.run: r for r in parse_runinfo(RUNINFO)}
        assert rows["SRR28065797"].biosample == "SAMN39984924"
        assert rows["SRR28065797"].bioproject == "PRJNA1077913"
        assert rows["SRR28065797"].experiment == "SRX23715436"

    def test_covers_all_three_insdc_namespaces(self):
        rows = {r.run: r for r in parse_runinfo(RUNINFO)}
        assert rows["SRR28065797"].biosample.startswith("SAMN")
        assert rows["ERR519283"].biosample.startswith("SAMEA")
        assert rows["DRR191146"].biosample.startswith("SAMD")

    def test_blank_and_headerless_input(self):
        assert parse_runinfo("") == []
        assert parse_runinfo("Run,BioSample\n,\n") == []

    def test_empty_cells_become_none(self):
        rows = parse_runinfo("Run,Experiment,BioSample,BioProject\nSRR1,,,\n")
        assert rows == [RunInfo(run="SRR1")]


class TestLookup:
    def test_resolves_a_run_to_itself(self, offline):
        index = offline.lookup(["SRR28065797"])
        runs = index.runs_for("SRR28065797")
        assert [r.run for r in runs] == ["SRR28065797"]
        assert index.biosamples_for("SRR28065797") == {"SAMN39984924"}

    def test_resolves_an_experiment_to_its_runs(self, offline):
        """ERX2249545 holds ERR2193522, under a different accession prefix."""
        index = offline.lookup(["ERX2249545"])
        assert [r.run for r in index.runs_for("ERX2249545")] == ["ERR2193522"]
        assert index.biosamples_for("ERX2249545") == {"SAMEA104378306"}

    def test_reports_what_sra_does_not_know(self, offline):
        index = offline.lookup(["SRR99999999999"])
        assert index.runs_for("SRR99999999999") == ()
        assert index.missing == ("SRR99999999999",)

    def test_one_batch_is_two_requests(self, offline):
        """esearch then efetch: batching is what makes the corpus feasible."""
        offline.lookup(["SRR28065797", "ERR519283", "DRR191146"])
        assert len(offline.calls) == 2
        assert "esearch" in offline.calls[0][0]
        assert "efetch" in offline.calls[1][0]

    def test_accessions_are_ord_into_one_term(self, offline):
        offline.lookup(["SRR28065797", "ERR519283"])
        term = offline.calls[0][1]["term"]
        assert " OR " in term
        assert "SRR28065797" in term and "ERR519283" in term

    def test_batches_respect_the_size_limit(self, monkeypatch, tmp_path):
        calls: list[dict] = []

        def fake_post_text(url: str, fields: dict, **kwargs) -> str:
            calls.append(fields)
            return ESEARCH if "esearch" in url else RUNINFO

        monkeypatch.setattr(sra_module.http, "post_text", fake_post_text)
        client = Sra(Cache(directory=tmp_path), batch_size=2)
        client.lookup([f"SRR{i}" for i in range(5)])
        searches = [c for c in calls if "term" in c]
        assert len(searches) == 3  # 5 accessions in batches of 2

    def test_results_are_cached_per_accession(self, offline):
        offline.lookup(["SRR28065797", "ERR519283"])
        before = len(offline.calls)
        offline.lookup(["SRR28065797"])
        assert len(offline.calls) == before

    def test_a_miss_is_cached_too(self, offline):
        offline.lookup(["SRR99999999999"])
        before = len(offline.calls)
        offline.lookup(["SRR99999999999"])
        assert len(offline.calls) == before

    def test_blank_accessions_are_ignored(self, offline):
        assert offline.lookup(["", "   ", None]).resolved == {}
        assert offline.calls == []

    def test_bioprojects_are_collected(self, offline):
        index = offline.lookup(["SRR28065797", "ERR519283", "DRR191146"])
        assert index.bioprojects == {"PRJNA1077913", "PRJEB6383", "PRJDB7806"}

    def test_all_runs_deduplicates(self, offline):
        index = offline.lookup(["SRR28065797", "SRX23715436"])
        runs = [r.run for r in index.all_runs]
        assert runs.count("SRR28065797") == 1


class TestApiKey:
    def test_the_key_is_passed_and_raises_the_cap(self, offline, monkeypatch):
        from congen.core import http

        monkeypatch.setenv("NCBI_API_KEY", "secret123")
        offline.lookup(["SRR28065797"])
        assert offline.calls[0][1]["api_key"] == "secret123"
        assert http._limiter_for("https://eutils.ncbi.nlm.nih.gov/x").min_interval == 0.1

    def test_no_key_means_no_parameter(self, offline, monkeypatch):
        monkeypatch.delenv("NCBI_API_KEY", raising=False)
        offline.lookup(["SRR28065797"])
        assert "api_key" not in offline.calls[0][1]
