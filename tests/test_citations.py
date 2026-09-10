"""Tests for the curated citation files and the lookup clients.

The lookups are exercised against recorded payloads, not the live
services: the suite runs offline, and Europe PMC's answer for a given
accession is not a stable thing to assert against anyway.
"""

from __future__ import annotations

import pytest

from congen.core.metadata.citations import (
    COLUMNS,
    Citation,
    Status,
    load_citations,
    load_tool_citations,
    merge_proposals,
    parse_citations,
    parse_tool_citations,
    render_citations,
)
from congen.core.remote.bioproject import parse_summaries
from congen.core.remote.literature import parse_search

HEADER = ",".join(COLUMNS)


def csv_text(*rows: str) -> str:
    return HEADER + "\n" + "\n".join(rows) + "\n"


class TestParsing:
    def test_it_reads_the_three_states(self):
        index = parse_citations(
            csv_text(
                "PRJNA1,confirmed,T,S,10.1/x,Someone (2020) A paper,tim,2026-09-11,",
                "PRJNA2,none,T2,S2,,,tim,2026-09-11,no paper exists",
                "PRJNA3,unreviewed,T3,S3,,,,,",
            )
        )
        assert len(index) == 3
        assert index.status_of("PRJNA1") is Status.CONFIRMED
        assert index.status_of("PRJNA2") is Status.NONE
        assert index.status_of("PRJNA3") is Status.UNREVIEWED
        assert index.issues == []

    def test_an_unknown_bioproject_defaults_to_unreviewed(self):
        """A default rather than None, so no call site needs a special case."""
        index = parse_citations(csv_text("PRJNA1,confirmed,,,10.1/x,C,,,"))
        assert index.status_of("PRJNA_ABSENT") is Status.UNREVIEWED
        assert index.get("PRJNA_ABSENT").bioproject == "PRJNA_ABSENT"

    def test_only_confirmed_with_a_reference_is_citable(self):
        rows = parse_citations(
            csv_text(
                "PRJNA1,confirmed,,,10.1/x,,,,",
                "PRJNA2,confirmed,,,,,,,",
                "PRJNA3,none,,,10.1/y,C,,,",
                "PRJNA4,unreviewed,,,10.1/z,C,,,",
            )
        )
        assert rows.get("PRJNA1").is_citable
        # confirmed but with nothing to cite is not citable
        assert not rows.get("PRJNA2").is_citable
        # a `none` ruling outranks a stale doi left in the row
        assert not rows.get("PRJNA3").is_citable
        # a candidate nobody has accepted is not a citation
        assert not rows.get("PRJNA4").is_citable

    def test_needs_review_excludes_both_reviewed_states(self):
        index = parse_citations(
            csv_text("PRJNA1,confirmed,,,10.1/x,C,,,", "PRJNA2,none,,,,,,,")
        )
        assert index.needs_review(["PRJNA1", "PRJNA2", "PRJNA3"]) == ["PRJNA3"]

    def test_an_unknown_status_is_reported_and_treated_as_unreviewed(self):
        index = parse_citations(csv_text("PRJNA1,probably?,,,,,,,"))
        assert index.status_of("PRJNA1") is Status.UNREVIEWED
        assert [issue.code for issue in index.issues] == ["C003"]

    def test_a_missing_required_column_is_reported_not_raised(self):
        index = parse_citations("title,doi\nsomething,10.1/x\n")
        assert len(index) == 0
        assert [issue.code for issue in index.issues] == ["C001"]

    def test_a_row_with_no_accession_is_reported(self):
        index = parse_citations(csv_text(",confirmed,,,,,,,"))
        assert [issue.code for issue in index.issues] == ["C002"]

    def test_a_duplicate_keeps_the_last_and_says_so(self):
        index = parse_citations(
            csv_text("PRJNA1,unreviewed,,,,,,,", "PRJNA1,confirmed,,,10.1/x,C,,,")
        )
        assert index.status_of("PRJNA1") is Status.CONFIRMED
        assert [issue.code for issue in index.issues] == ["C004"]

    def test_an_empty_or_absent_file_is_an_empty_index(self, tmp_path):
        assert len(parse_citations("")) == 0
        assert len(load_citations(tmp_path)) == 0


class TestRoundTrip:
    def test_render_then_parse_preserves_every_field(self):
        original = Citation(
            bioproject="PRJNA1",
            status=Status.CONFIRMED,
            title="A title, with a comma",
            submitter="Some Institute",
            doi="10.1/x",
            citation='Someone (2020) "Quoted" title',
            reviewed_by="tim",
            reviewed_on="2026-09-11",
            notes="checked by hand",
        )
        again = parse_citations(render_citations([original])).get("PRJNA1")
        for name in COLUMNS:
            assert getattr(again, name) == getattr(original, name), name

    def test_output_is_sorted_so_a_hand_edit_and_a_tool_write_agree(self):
        text = render_citations(
            [Citation(bioproject="PRJNB2"), Citation(bioproject="PRJNA1")]
        )
        assert text.splitlines()[1].startswith("PRJNA1")


class TestMerge:
    """The two rules that make it safe to write into a file a human edits."""

    def test_a_confirmed_row_is_never_touched(self):
        index = parse_citations(csv_text("PRJNA1,confirmed,T,S,10.1/mine,Mine,tim,2026-09-11,"))
        rows, touched = merge_proposals(
            index, [Citation(bioproject="PRJNA1", doi="10.9/other", citation="Other")]
        )
        assert touched == 0
        assert rows[0].doi == "10.1/mine"

    def test_a_none_row_is_never_touched(self):
        """Otherwise a lookup reopens a decision someone already made."""
        index = parse_citations(csv_text("PRJNA1,none,,,,,tim,2026-09-11,nothing published"))
        rows, touched = merge_proposals(
            index, [Citation(bioproject="PRJNA1", doi="10.9/found", citation="Found")]
        )
        assert touched == 0
        assert rows[0].doi == ""
        assert rows[0].status is Status.NONE

    def test_an_unreviewed_row_gets_its_blanks_filled(self):
        index = parse_citations(csv_text("PRJNA1,unreviewed,,,,,,,"))
        rows, touched = merge_proposals(
            index, [Citation(bioproject="PRJNA1", title="T", doi="10.1/x")]
        )
        assert touched == 1
        assert (rows[0].title, rows[0].doi) == ("T", "10.1/x")

    def test_a_non_empty_field_survives_on_an_unreviewed_row(self):
        """A half-finished hand edit must survive a refresh."""
        index = parse_citations(csv_text("PRJNA1,unreviewed,,,10.5/handwritten,,,,"))
        rows, _ = merge_proposals(
            index, [Citation(bioproject="PRJNA1", doi="10.9/proposed", title="T")]
        )
        assert rows[0].doi == "10.5/handwritten"
        assert rows[0].title == "T"

    def test_a_proposal_never_sets_a_status(self):
        index = parse_citations(csv_text("PRJNA1,unreviewed,,,,,,,"))
        rows, _ = merge_proposals(
            index, [Citation(bioproject="PRJNA1", status=Status.CONFIRMED, doi="10.1/x")]
        )
        assert rows[0].status is Status.UNREVIEWED

    def test_an_unseen_bioproject_is_added(self):
        rows, touched = merge_proposals(
            parse_citations(""), [Citation(bioproject="PRJNA9", title="T")]
        )
        assert touched == 1
        assert rows[0].bioproject == "PRJNA9"


class TestToolCitations:
    def test_pending_entries_are_not_citable(self, tmp_path):
        text = "snparcher:\n  name: snpArcher\n  status: pending\n  doi: ''\n"
        tools = parse_tool_citations(text)
        assert tools["snparcher"].status == "pending"
        assert not tools["snparcher"].is_citable

    def test_a_confirmed_entry_with_a_reference_is_citable(self):
        text = "gatk:\n  name: GATK\n  status: confirmed\n  doi: 10.1/gatk\n"
        assert parse_tool_citations(text)["gatk"].is_citable

    def test_a_broken_file_yields_nothing_rather_than_raising(self):
        assert parse_tool_citations("{{{not yaml") == {}

    def test_an_absent_file_yields_nothing(self, tmp_path):
        assert load_tool_citations(tmp_path) == {}


class TestEuropePmcParsing:
    PAYLOAD = """{"hitCount": 2, "resultList": {"result": [
        {"doi": "10.1098/rspb.2018.1246", "title": "Signatures of human-commensalism.",
         "journalTitle": "Proc Biol Sci", "pubYear": "2018",
         "authorString": "Ravinet M, Elgvin TO, Trier C.", "pmid": "30185642"}]}}"""

    def test_it_builds_a_one_line_citation(self):
        hits = parse_search("PRJEB27649", self.PAYLOAD)
        assert hits.total == 2
        top = hits.candidates[0]
        assert top.doi == "10.1098/rspb.2018.1246"
        assert top.citation == (
            "Ravinet M et al. (2018) Signatures of human-commensalism Proc Biol Sci."
        )

    def test_no_results_is_found_false_and_not_an_error(self):
        hits = parse_search("PRJNA1", '{"hitCount": 0, "resultList": {"result": []}}')
        assert not hits.found
        assert hits.error is None

    def test_an_unparseable_response_is_an_error_not_an_absence(self):
        """An outage must never be recorded as `none`."""
        hits = parse_search("PRJNA1", "<html>502</html>")
        assert hits.error is not None
        assert not hits.found

    def test_a_single_author_is_not_given_et_al(self):
        payload = '{"hitCount":1,"resultList":{"result":[{"authorString":"Solo A."}]}}'
        assert parse_search("X", payload).candidates[0].authors == "Solo A"


class TestBioProjectParsing:
    PAYLOAD = """{"result": {"uids": ["1462765"], "1462765": {
        "project_acc": "PRJNA1462765",
        "project_title": "Dryobates pubescens genome, bDryPub1, seq",
        "submitter_organization": "Vertebrate Genomes Project"}}}"""

    def test_it_keys_on_the_accession_not_the_uid(self):
        found = parse_summaries(self.PAYLOAD)
        assert set(found) == {"PRJNA1462765"}
        assert found["PRJNA1462765"].submitter == "Vertebrate Genomes Project"

    def test_a_document_without_an_accession_is_skipped(self):
        payload = '{"result": {"uids": ["1"], "1": {"project_title": "no accession"}}}'
        assert parse_summaries(payload) == {}

    def test_an_unparseable_response_yields_nothing(self):
        assert parse_summaries("not json") == {}


class TestSurvey:
    """The review queue must be built from both sources, not either one.

    `README.txt` says what a species claims to draw on; the SRA mapping
    says what actually contributed runs. Those disagree — `E002` and
    `E003` exist because of it — so taking either alone builds the wrong
    queue.
    """

    def test_it_unions_declared_and_contributing_projects(self, tmp_path):
        import json
        import shutil

        from congen.core.metadata.discovery import SpeciesRepo
        from congen.tools.citations.cli import survey

        from tests.conftest import METADATA_ROOT

        root = tmp_path / "metadata"
        shutil.copytree(METADATA_ROOT, root)
        species = SpeciesRepo(root).load("reptiles/podarcis-raffonei")
        sheet_input = species.sheet.rows[0].input
        sample = species.sheet.rows[0].sample_id
        (species.path / "dataset.json").write_text(
            json.dumps(
                {
                    "subject": species.key,
                    "sra": {
                        sheet_input: [
                            {"run": "SRR1", "biosample": "SAMN1", "bioproject": "PRJ_FROM_SRA"}
                        ]
                    },
                }
            )
        )
        samples, species_of = survey(SpeciesRepo(root))
        assert "PRJ_FROM_SRA" in samples
        assert samples["PRJ_FROM_SRA"][species.key] == 1
        # and whatever README.txt declares is in the queue too, even
        # though no run was attributed to it
        for declared in species.readme.bioprojects:
            assert declared in samples
            assert sum(samples[declared].values()) == 0
        assert species.key in species_of["PRJ_FROM_SRA"]

    def test_a_species_without_a_dataset_record_is_not_fatal(self, tmp_path):
        import shutil

        from congen.core.metadata.discovery import SpeciesRepo
        from congen.tools.citations.cli import survey

        from tests.conftest import METADATA_ROOT

        root = tmp_path / "metadata"
        shutil.copytree(METADATA_ROOT, root)
        samples, _ = survey(SpeciesRepo(root))
        # the fixtures carry no dataset.json, so everything comes from
        # README.txt and nothing raises
        assert samples
