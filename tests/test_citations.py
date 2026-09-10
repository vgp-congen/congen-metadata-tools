"""Tests for the citation queue, the record, and the lookup clients.

The lookups run against recorded payloads: the suite is offline, and
Europe PMC's answer for an accession is not a stable thing to assert.
"""

from __future__ import annotations

import pytest

from congen.core.metadata.citations import (
    MATCH_STAR,
    QUEUE_FILE,
    RECORD_FILE,
    NOT_FOUND_MARKER,
    UNREVIEWED_MARKER,
    Entry,
    Status,
    load_citations,
    load_tool_citations,
    merge_proposals,
    parse_queue,
    parse_record,
    parse_tool_citations,
    render_queue,
    render_record,
    reopen,
)
from congen.core.remote.bioproject import parse_summaries
from congen.core.remote.literature import parse_search


def block(*lines: str, accession: str = "PRJNA1", samples: int = 5) -> str:
    head = [
        f"## {accession} — {samples} samples · birds/x",
        "",
        "A project title",
        "Some Institute · [BioProject](https://example/x)",
        "",
    ]
    return "\n".join(head + list(lines)) + "\n"


def doi_line(doi: str, text: str = "Someone (2020) A paper") -> str:
    return f"- [{doi}](https://doi.org/{doi}) — {text}"


def one(text: str, accession: str = "PRJNA1") -> Entry:
    entries, _ = parse_queue(text)
    return entries[accession]


class TestReviewByDeletion:
    """One rule: every line is a claim, so delete the false ones."""

    def test_an_untouched_block_is_unreviewed(self):
        entry = one(block(f"- {UNREVIEWED_MARKER}", doi_line("10.a/x"), f"- {NOT_FOUND_MARKER}"))
        assert entry.status is Status.UNREVIEWED

    def test_keeping_one_doi_confirms_it(self):
        entry = one(block(doi_line("10.a/x")))
        assert entry.status is Status.CONFIRMED
        assert entry.dois == ["10.a/x"]

    def test_keeping_two_dois_confirms_both(self):
        """The single-column CSV could not express this at all."""
        entry = one(block(doi_line("10.a/x"), doi_line("10.b/y")))
        assert entry.dois == ["10.a/x", "10.b/y"]
        assert entry.status is Status.CONFIRMED

    def test_keeping_only_the_marker_records_nothing_found(self):
        """One not-found state, not two.

        Whether a paper is *coming* is a claim about the submitter's
        intentions, which a reviewer who is not the submitter cannot make.
        """
        entry = one(block(f"- {NOT_FOUND_MARKER}"))
        assert entry.status is Status.NOT_FOUND
        assert entry.status.is_reviewed
        assert entry.status.reopens_on_new_evidence

    def test_a_doi_outranks_a_leftover_marker(self):
        """A reviewer who keeps a paper but forgets the marker still meant
        to accept the paper."""
        entry = one(block(doi_line("10.a/x"), f"- {NOT_FOUND_MARKER}"))
        assert entry.status is Status.CONFIRMED

    def test_deleting_every_line_asserts_nothing(self):
        """Silence is not a decision, and must never read as a citation."""
        entries, issues = parse_queue(block())
        assert entries["PRJNA1"].status is Status.UNREVIEWED
        assert [issue.code for issue in issues] == ["C010"]

    def test_a_bare_doi_is_accepted(self):
        """So a reviewer can paste one in without writing markdown."""
        assert one(block("- 10.a/pasted")).dois == ["10.a/pasted"]

    def test_the_heading_carries_weight_and_species(self):
        entry = one(block(f"- {UNREVIEWED_MARKER}", samples=147))
        assert entry.samples == 147
        assert entry.species == ["birds/x"]


class TestRejectionMemory:
    """`considered` is what makes "a candidate nobody has seen" decidable."""

    def test_deleted_candidates_are_remembered_as_rejected(self):
        text = block(
            "<!-- considered: 10.a/x 10.b/y 10.c/z -->",
            "",
            doi_line("10.b/y"),
        )
        entry = one(text)
        assert entry.dois == ["10.b/y"]
        assert entry.rejected == ["10.a/x", "10.c/z"]

    def test_rejections_survive_the_record_round_trip(self):
        entry = Entry(
            bioproject="PRJNA1",
            status=Status.NOT_FOUND,
            rejected=["10.a/x", "10.b/y"],
            title="T",
        )
        again = parse_record(render_record([entry]))["PRJNA1"]
        assert again.status is Status.NOT_FOUND
        assert again.rejected == ["10.a/x", "10.b/y"]

    def test_the_considered_comment_survives_rendering(self):
        entry = Entry(bioproject="PRJNA1", dois=["10.a/x"], rejected=["10.b/y"])
        assert "considered: 10.a/x 10.b/y" in render_queue([entry])


class TestReopen:
    """Data is routinely released before its paper, so `not_found` is not
    the end — and the trigger for looking again is evidence, not a date."""

    def _not_found(self):
        return Entry(
            bioproject="PRJNA1",
            status=Status.NOT_FOUND,
            rejected=["10.a/old"],
            title="T",
        )

    def test_the_same_candidates_do_not_reopen_it(self):
        assert reopen(self._not_found(), ["10.a/old"]) is None

    def test_nothing_found_does_not_reopen_it(self):
        assert reopen(self._not_found(), []) is None

    def test_a_new_candidate_reopens_it(self):
        revived = reopen(self._not_found(), ["10.a/old", "10.b/new"])
        assert revived is not None
        assert revived.status is Status.UNREVIEWED
        # only the unseen one is offered; the rejected one is not re-asked
        assert revived.dois == ["10.b/new"]
        assert revived.rejected == ["10.a/old"]
        # and a second rejection would still be remembered
        assert revived.considered == ["10.a/old", "10.b/new"]

    def test_confirmed_never_reopens(self):
        """A later hit on the same BioProject is usually data reuse, and
        reuse needs no credit — so reopening would offer a citation
        nobody should add."""
        entry = Entry(bioproject="PRJNA1", status=Status.CONFIRMED, dois=["10.a/x"])
        assert reopen(entry, ["10.z/new"]) is None

    def test_only_not_found_reopens(self):
        assert Status.NOT_FOUND.reopens_on_new_evidence
        assert not Status.CONFIRMED.reopens_on_new_evidence
        assert not Status.UNREVIEWED.reopens_on_new_evidence
        assert not Status.UNREVIEWED.is_reviewed


class TestMerge:
    def test_a_new_bioproject_is_added(self):
        merged, changed = merge_proposals({}, [Entry(bioproject="PRJNA9", dois=["10.a/x"])])
        assert changed == 1 and "PRJNA9" in merged

    def test_candidates_are_added_to_an_unreviewed_block(self):
        queued = {"PRJNA1": Entry(bioproject="PRJNA1", dois=["10.a/x"])}
        merged, _ = merge_proposals(queued, [Entry(bioproject="PRJNA1", dois=["10.b/y"])])
        assert merged["PRJNA1"].dois == ["10.a/x", "10.b/y"]

    def test_a_reviewed_block_keeps_its_dois(self):
        """A search is not entitled to reopen a decision."""
        queued = {
            "PRJNA1": Entry(bioproject="PRJNA1", status=Status.CONFIRMED, dois=["10.a/mine"])
        }
        merged, _ = merge_proposals(queued, [Entry(bioproject="PRJNA1", dois=["10.b/other"])])
        assert merged["PRJNA1"].dois == ["10.a/mine"]

    def test_weight_refreshes_even_on_a_reviewed_block(self):
        """It describes the corpus, not the decision, and a stale count
        misorders the queue."""
        queued = {"PRJNA1": Entry(bioproject="PRJNA1", status=Status.NOT_FOUND, samples=1)}
        merged, changed = merge_proposals(
            queued, [Entry(bioproject="PRJNA1", samples=40, species=["birds/y"])]
        )
        assert merged["PRJNA1"].samples == 40 and changed == 1


class TestQueueRendering:
    def test_it_orders_by_samples_worst_first(self):
        text = render_queue(
            [
                Entry(bioproject="PRJNA_SMALL", samples=1),
                Entry(bioproject="PRJNA_BIG", samples=150),
            ]
        )
        assert text.index("PRJNA_BIG") < text.index("PRJNA_SMALL")

    def test_an_empty_queue_says_so(self):
        assert "Nothing awaiting review" in render_queue([])

    def test_a_rendered_block_round_trips(self):
        entry = Entry(
            bioproject="PRJNA1",
            dois=["10.a/x", "10.b/starred"],
            references={"10.a/x": "Someone (2020) A paper"},
            starred={"10.b/starred"},
            title="A title",
            submitter="An institute",
            samples=7,
            species=["birds/x"],
        )
        again = one(render_queue([entry]))
        assert set(again.dois) == {"10.a/x", "10.b/starred"}
        assert again.samples == 7
        assert again.title == "A title"
        assert again.reference("10.a/x") == "Someone (2020) A paper"
        # The star has to survive, because every rewrite of the queue goes
        # through parse -> render. Without this assertion a single
        # `--collect` silently stripped all 166 stars from the committed
        # queue, and the test above still passed.
        assert again.starred == {"10.b/starred"}

    def test_a_collect_style_rewrite_keeps_every_star(self):
        """The specific regression: rewriting the queue must preserve stars."""
        entries = [
            Entry(bioproject="PRJNA1", dois=["10.a/x"], starred={"10.a/x"}),
            Entry(bioproject="PRJNA2", dois=["10.b/y"], starred={"10.b/y"}),
        ]
        once = render_queue(entries)
        parsed, _ = parse_queue(once)
        twice = render_queue(parsed.values())
        assert once.count(f"- {MATCH_STAR} ") == 2
        assert twice.count(f"- {MATCH_STAR} ") == 2


class TestLoadingBothFiles:
    def test_the_queue_overrides_the_record(self, tmp_path):
        """A decision made in the queue counts before anyone runs the tool."""
        root = tmp_path
        (root / "references").mkdir(exist_ok=True)
        (root / RECORD_FILE).write_text(
            render_record([Entry(bioproject="PRJNA1", status=Status.NOT_FOUND)])
        )
        (root / QUEUE_FILE).write_text(
            render_queue(
                [Entry(bioproject="PRJNA1", status=Status.CONFIRMED, dois=["10.a/x"])]
            )
        )
        index = load_citations(tmp_path)
        assert index.status_of("PRJNA1") is Status.CONFIRMED
        assert index.unfiled == ["PRJNA1"]

    def test_a_stale_queue_block_cannot_undecide_the_record(self, tmp_path):
        root = tmp_path
        (root / "references").mkdir(exist_ok=True)
        (root / RECORD_FILE).write_text(
            render_record([Entry(bioproject="PRJNA1", status=Status.CONFIRMED, dois=["10.a/x"])])
        )
        (root / QUEUE_FILE).write_text(
            render_queue([Entry(bioproject="PRJNA1", status=Status.UNREVIEWED)])
        )
        assert load_citations(tmp_path).status_of("PRJNA1") is Status.CONFIRMED

    def test_absent_files_load_as_empty(self, tmp_path):
        index = load_citations(tmp_path)
        assert len(index) == 0
        assert index.status_of("PRJNA1") is Status.UNREVIEWED

    def test_needs_review_excludes_every_reviewed_state(self, tmp_path):
        root = tmp_path
        (root / "references").mkdir(exist_ok=True)
        (root / RECORD_FILE).write_text(
            render_record(
                [
                    Entry(bioproject="PRJNA1", status=Status.CONFIRMED, dois=["10.a/x"]),
                    Entry(bioproject="PRJNA2", status=Status.NOT_FOUND),
                    Entry(bioproject="PRJNA3", status=Status.NOT_FOUND),
                ]
            )
        )
        index = load_citations(tmp_path)
        assert index.needs_review(["PRJNA1", "PRJNA2", "PRJNA3", "PRJNA4"]) == ["PRJNA4"]
        assert index.citable(["PRJNA1", "PRJNA2", "PRJNA3"]) == ["PRJNA1"]


class TestToolCitations:
    def test_pending_entries_are_not_citable(self):
        tools = parse_tool_citations("snparcher:\n  name: snpArcher\n  status: pending\n")
        assert not tools["snparcher"].is_citable

    def test_a_confirmed_entry_with_a_doi_is_citable(self):
        tools = parse_tool_citations("gatk:\n  status: confirmed\n  doi: 10.1/gatk\n")
        assert tools["gatk"].is_citable

    def test_a_broken_file_yields_nothing(self):
        assert parse_tool_citations("{{{not yaml") == {}

    def test_an_absent_file_yields_nothing(self, tmp_path):
        assert load_tool_citations(tmp_path) == {}


class TestEuropePmcParsing:
    PAYLOAD = """{"hitCount": 2, "resultList": {"result": [
        {"doi": "10.1098/rspb.2018.1246", "title": "Signatures of human-commensalism.",
         "journalTitle": "Proc Biol Sci", "pubYear": "2018",
         "authorString": "Ravinet M, Elgvin TO, Trier C.", "pmid": "30185642"}]}}"""

    def test_it_builds_a_one_line_reference(self):
        top = parse_search("PRJEB27649", self.PAYLOAD).candidates[0]
        assert top.doi == "10.1098/rspb.2018.1246"
        assert top.citation.startswith("Ravinet M et al. (2018) Signatures")

    def test_no_results_is_absence_not_error(self):
        hits = parse_search("PRJNA1", '{"hitCount": 0, "resultList": {"result": []}}')
        assert not hits.found and hits.error is None

    def test_an_unparseable_response_is_an_error_not_absence(self):
        """An outage must never be filed as "no publication"."""
        hits = parse_search("PRJNA1", "<html>502</html>")
        assert hits.error is not None and not hits.found


class TestBioProjectParsing:
    PAYLOAD = """{"result": {"uids": ["1462765"], "1462765": {
        "project_acc": "PRJNA1462765",
        "project_title": "Dryobates pubescens genome, bDryPub1, seq",
        "submitter_organization": "Vertebrate Genomes Project"}}}"""

    def test_it_keys_on_the_accession_not_the_uid(self):
        found = parse_summaries(self.PAYLOAD)
        assert set(found) == {"PRJNA1462765"}
        assert found["PRJNA1462765"].submitter == "Vertebrate Genomes Project"

    def test_an_unparseable_response_yields_nothing(self):
        assert parse_summaries("not json") == {}


class TestAffiliationStar:
    """The submitter's own affiliation on a paper is the best free signal
    that the data was generated for it."""

    def test_a_match_is_starred_and_listed_first(self):
        entry = Entry(
            bioproject="PRJNA1",
            dois=["10.a/other", "10.b/match"],
            starred={"10.b/match"},
        )
        # Only the list items; the `considered` comment keeps its own order.
        items = [l for l in render_queue([entry]).splitlines() if l.startswith("- [") or l.startswith(f"- {MATCH_STAR}")]
        assert items[0] == f"- {MATCH_STAR} [10.b/match](https://doi.org/10.b/match)"
        assert "10.a/other" in items[1]

    def test_a_starred_line_still_parses_as_a_doi(self):
        entry = one(
            block(f"- {MATCH_STAR} [10.b/match](https://doi.org/10.b/match) — Someone (2020) X")
        )
        assert entry.dois == ["10.b/match"]

    def test_the_matcher_discriminates_on_real_affiliations(self):
        from congen.core.remote.literature import affiliation_matches

        helsinki = "Institute of Biotechnology, HiLIFE, University of Helsinki, Finland."
        petersburg = "European University at St. Petersburg, Russian Federation."
        assert affiliation_matches("UNIVERSITY OF HELSINKI", [petersburg, helsinki])
        assert not affiliation_matches("UNIVERSITY OF HELSINKI", [petersburg])

    def test_generic_words_alone_never_match(self):
        """Otherwise every "Department of Biology" matches every other."""
        from congen.core.remote.literature import affiliation_matches

        assert not affiliation_matches("The University", ["University of Anywhere"])

    def test_affiliations_come_from_every_author_not_just_the_first(self):
        """For PRJEB39599 the submitter is Helsinki and the first author is
        in St Petersburg, with Helsinki further down the list."""
        payload = """{"hitCount":1,"resultList":{"result":[{
            "doi":"10.a/x",
            "affiliation":"European University at St. Petersburg",
            "authorList":{"author":[
                {"affiliation":"European University at St. Petersburg"},
                {"authorAffiliationDetailsList":{"authorAffiliation":[
                    {"affiliation":"University of Helsinki, Finland"}]}}]}}]}}"""
        candidate = parse_search("PRJEB39599", payload).candidates[0]
        assert len(candidate.affiliations) == 2
        assert candidate.matches_submitter("UNIVERSITY OF HELSINKI")


class TestVerifyCli:
    """`--verify` guards against the typo the review mechanic invites.

    A reviewer types DOIs by hand, and nothing else in this toolchain can
    see a wrong one: a capital O for a zero looks fine in a diff and
    would put a dead link in 79 documents.
    """

    def _repo(self, tmp_path, entries):
        import shutil

        from congen.core.metadata.citations import RECORD_FILE
        from tests.conftest import METADATA_ROOT

        root = tmp_path / "metadata"
        shutil.copytree(METADATA_ROOT, root)
        (root / RECORD_FILE).parent.mkdir(parents=True, exist_ok=True)
        (root / RECORD_FILE).write_text(render_record(entries))
        return root

    def _run(self, root, monkeypatch, responder):
        from click.testing import CliRunner

        from congen.core import http
        from congen.core.cli import main

        monkeypatch.setattr(http, "request", responder)
        return CliRunner().invoke(
            main, ["citations", "--verify", "--metadata-root", str(root)]
        )

    PAYLOAD = b'{"title": "A paper", "container-title": "J", "issued": {"date-parts": [[2020]]}, "author": [{"given": "A B", "family": "Author"}]}'

    def test_a_resolving_doi_is_marked_verified_and_gets_its_reference(
        self, tmp_path, monkeypatch
    ):
        from congen.core.metadata.citations import RECORD_FILE, parse_record

        root = self._repo(
            tmp_path,
            [Entry(bioproject="PRJNA1", status=Status.CONFIRMED, dois=["10.a/x"])],
        )

        def ok(url, **kwargs):
            from congen.core import http

            return http.Response(url=url, status=200, body=self.PAYLOAD, headers={})

        result = self._run(root, monkeypatch, ok)
        assert result.exit_code == 0
        entry = parse_record((root / RECORD_FILE).read_text())["PRJNA1"]
        assert entry.verified == {"10.a/x"}
        assert entry.reference("10.a/x") == "Author AB (2020) A paper J."

    def test_a_bad_doi_exits_non_zero_and_names_it(self, tmp_path, monkeypatch):
        root = self._repo(
            tmp_path,
            [Entry(bioproject="PRJNA1", status=Status.CONFIRMED, dois=["10.a/typo"])],
        )

        def not_found(url, **kwargs):
            from congen.core import http

            raise http.HttpError(url, 404, "Not Found")

        result = self._run(root, monkeypatch, not_found)
        assert result.exit_code == 1
        assert "do not resolve" in result.output
        assert "10.a/typo" in result.output

    def test_an_outage_writes_nothing(self, tmp_path, monkeypatch):
        """A verdict nobody managed to reach must not be recorded."""
        from congen.core.metadata.citations import RECORD_FILE, parse_record

        root = self._repo(
            tmp_path,
            [Entry(bioproject="PRJNA1", status=Status.CONFIRMED, dois=["10.a/x"])],
        )
        before = (root / RECORD_FILE).read_text()

        def unreachable(url, **kwargs):
            raise TimeoutError("doi.org is down")

        result = self._run(root, monkeypatch, unreachable)
        assert result.exit_code == 1
        assert "not written" in result.output
        assert (root / RECORD_FILE).read_text() == before
        assert parse_record(before)["PRJNA1"].verified == set()

    def test_an_already_verified_doi_is_not_checked_again(self, tmp_path, monkeypatch):
        """One request per new citation, not 300 per run."""
        root = self._repo(
            tmp_path,
            [
                Entry(
                    bioproject="PRJNA1",
                    status=Status.CONFIRMED,
                    dois=["10.a/x"],
                    references={"10.a/x": "Already known"},
                    verified={"10.a/x"},
                )
            ],
        )

        def refuse(url, **kwargs):
            raise AssertionError("should not have been asked")

        result = self._run(root, monkeypatch, refuse)
        assert result.exit_code == 0
        assert "already verified" in result.output

    def test_an_existing_reference_is_not_overwritten(self, tmp_path, monkeypatch):
        """A reference a human wrote is not ours to replace."""
        from congen.core.metadata.citations import RECORD_FILE, parse_record

        root = self._repo(
            tmp_path,
            [
                Entry(
                    bioproject="PRJNA1",
                    status=Status.CONFIRMED,
                    dois=["10.a/x"],
                    references={"10.a/x": "Hand-written, and better"},
                )
            ],
        )

        def ok(url, **kwargs):
            from congen.core import http

            return http.Response(url=url, status=200, body=self.PAYLOAD, headers={})

        self._run(root, monkeypatch, ok)
        entry = parse_record((root / RECORD_FILE).read_text())["PRJNA1"]
        assert entry.reference("10.a/x") == "Hand-written, and better"
        assert entry.verified == {"10.a/x"}


class TestFilingTwice:
    """The record knows things the queue cannot, and must not lose them."""

    def _filed(self):
        from congen.core.metadata.citations import merge_into_record

        return merge_into_record, Entry(
            bioproject="PRJNA1",
            status=Status.CONFIRMED,
            dois=["10.a/x", "10.b/y"],
            references={"10.a/x": "Fetched reference", "10.b/y": "Another"},
            verified={"10.a/x", "10.b/y"},
            rejected=["10.old/one"],
            title="From the record",
        )

    def test_verification_survives_a_second_filing(self):
        merge, existing = self._filed()
        again = Entry(bioproject="PRJNA1", status=Status.CONFIRMED, dois=["10.a/x", "10.b/y"])
        merged = merge(existing, again)
        assert merged.verified == {"10.a/x", "10.b/y"}
        assert merged.reference("10.a/x") == "Fetched reference"

    def test_a_doi_no_longer_accepted_loses_its_verification(self):
        """The queue owns the decision, so a removed DOI is simply gone."""
        merge, existing = self._filed()
        again = Entry(bioproject="PRJNA1", status=Status.CONFIRMED, dois=["10.a/x"])
        merged = merge(existing, again)
        assert merged.dois == ["10.a/x"]
        assert merged.verified == {"10.a/x"}

    def test_a_new_doi_arrives_unverified(self):
        merge, existing = self._filed()
        again = Entry(bioproject="PRJNA1", status=Status.CONFIRMED, dois=["10.a/x", "10.c/new"])
        merged = merge(existing, again)
        assert merged.verified == {"10.a/x"}
        assert "10.c/new" in merged.dois

    def test_rejections_accumulate_across_filings(self):
        merge, existing = self._filed()
        again = Entry(
            bioproject="PRJNA1", status=Status.CONFIRMED, dois=["10.a/x"], rejected=["10.new/two"]
        )
        assert merge(existing, again).rejected == ["10.new/two", "10.old/one"]

    def test_the_queue_wins_on_status(self):
        merge, existing = self._filed()
        again = Entry(bioproject="PRJNA1", status=Status.NOT_FOUND)
        assert merge(existing, again).status is Status.NOT_FOUND

    def test_a_first_filing_is_unchanged(self):
        merge, _ = self._filed()
        fresh = Entry(bioproject="PRJNA9", status=Status.CONFIRMED, dois=["10.z/z"])
        assert merge(None, fresh) is fresh


class TestSearchCacheExpiry:
    """A permanently cached search makes the reopen mechanism inert.

    `NO PUBLICATION FOUND` promises the BioProject comes back when a
    paper appears. If the search answer never expires, "nothing found"
    is replayed forever and nothing can ever be unseen.
    """

    def test_a_search_result_is_not_cached_forever(self, tmp_path, monkeypatch):
        import time

        from congen.core.cache import Cache
        from congen.core.remote.literature import NAMESPACE, SEARCH_TTL, EuropePmc

        cache = Cache(directory=tmp_path)
        cache.set(NAMESPACE, "PRJNA1", {"hitCount": 0, "resultList": {"result": []}})

        # Pretend the entry was written just over the TTL ago.
        stored = tmp_path / NAMESPACE
        for path in stored.rglob("*.json"):
            import json

            payload = json.loads(path.read_text())
            payload["stored_at"] = time.time() - SEARCH_TTL - 1
            path.write_text(json.dumps(payload))

        calls = []

        def fresh(url, **kwargs):
            calls.append(url)
            return '{"hitCount": 1, "resultList": {"result": [{"doi": "10.new/paper"}]}}'

        monkeypatch.setattr("congen.core.remote.literature.http.get_text", fresh)
        hits = EuropePmc(cache).search_accession("PRJNA1")
        assert calls, "an expired search must be re-asked"
        assert hits.candidates[0].doi == "10.new/paper"

    def test_a_recent_result_is_still_served_from_the_cache(self, tmp_path, monkeypatch):
        from congen.core.cache import Cache
        from congen.core.remote.literature import NAMESPACE, EuropePmc

        cache = Cache(directory=tmp_path)
        cache.set(NAMESPACE, "PRJNA1", {"hitCount": 0, "resultList": {"result": []}})

        def refuse(url, **kwargs):
            raise AssertionError("should not have been asked")

        monkeypatch.setattr("congen.core.remote.literature.http.get_text", refuse)
        assert not EuropePmc(cache).search_accession("PRJNA1").found
