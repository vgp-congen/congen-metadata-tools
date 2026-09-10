"""Tests for the README renderer.

Two properties matter more than any individual line of output, and they
are different: **determinism** (same inputs, same existing file, same
bytes) and **idempotence** (rendering twice changes nothing). The second
is what catches marker-handling bugs.

Golden files hold the third: that the document as a whole does not drift
unnoticed. They are generated from synthetic contexts so the suite stays
offline and stable.
"""

from __future__ import annotations

import socket

import pytest

from congen.core.metadata.writers import managed_block_names
from congen.core.validation_record import ReportState
from congen.tools.readme.gate import Mode, Provenance
from congen.tools.readme.render import (
    BLOCK_NAME,
    OUTPUT_NAME,
    render_body,
    render_document,
    write_document,
)

from tests.conftest_readme import context, dataset, validation

@pytest.fixture(scope="module")
def golden_dir():
    from pathlib import Path

    directory = Path(__file__).parent / "fixtures" / "documents"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def assert_golden(golden_dir, name: str, text: str) -> None:
    path = golden_dir / name
    if not path.exists():  # pragma: no cover - first run only, then committed
        path.write_text(text)
        pytest.fail(f"wrote new golden {name}; review and commit it")
    assert text == path.read_text(), f"{name} drifted; review the diff"


def finding(check, severity="warn", message="something"):
    return {"id": check, "severity": severity, "message": message}


def blocked_context():
    return context(record=validation(state=ReportState.PASS_WITH_WARNINGS, findings=[finding("G017")]))


def wrong_context():
    return context(
        record=validation(
            state=ReportState.PASS_WITH_WARNINGS,
            findings=[finding("E002"), finding("E003")],
        )
    )


def truncated_context():
    return context(
        record=validation(
            state=ReportState.PENDING,
            status={
                "state": "absent",
                "accession": None,
                "missing": ["raw_vcf"],
                "counts": {"sheet_samples": 21, "bams": 0, "vcf_samples": None},
            },
        ),
        data=dataset(published=False, objects={}, samples={}, cohort={}, vcf={}, prefix=None),
    )


class TestGolden:
    def test_a_full_cited_document(self, golden_dir):
        assert_golden(golden_dir, "full_cited.md", render_body(context()))

    def test_a_document_with_citations_blocked_as_missing(self, golden_dir):
        assert_golden(golden_dir, "blocked_missing.md", render_body(blocked_context()))

    def test_a_document_with_citations_blocked_as_wrong(self, golden_dir):
        assert_golden(golden_dir, "blocked_wrong.md", render_body(wrong_context()))

    def test_a_truncated_document(self, golden_dir):
        assert_golden(golden_dir, "truncated.md", render_body(truncated_context()))


class TestDeterminism:
    def test_rendering_twice_gives_identical_bytes(self):
        assert render_body(context()) == render_body(context())

    def test_no_timestamp_or_tool_version_appears(self):
        """A rendered version string would diff all 79 files per release."""
        body = render_body(context())
        assert "harvested" not in body
        assert "congen-metadata-tools" not in body
        # the only date shown is the validation date, which is an input
        assert body.count("2026-09-10") == 1

    def test_render_needs_no_network(self, monkeypatch):
        def refuse(*args, **kwargs):
            raise AssertionError("render touched the network")

        monkeypatch.setattr(socket, "socket", refuse)
        monkeypatch.setattr(socket, "create_connection", refuse)
        assert render_body(context())


class TestIdempotence:
    def test_rendering_into_its_own_output_changes_nothing(self):
        ctx = context()
        first = render_document(ctx)
        assert render_document(ctx, first) == first

    def test_exactly_one_managed_region_exists(self):
        text = render_document(context())
        assert managed_block_names(text) == [BLOCK_NAME]

    def test_writing_twice_reports_no_change(self, tmp_path):
        ctx = context()
        path = tmp_path / OUTPUT_NAME
        assert write_document(ctx, path) is True
        assert write_document(ctx, path) is False


class TestHandProse:
    PROSE = "## Notes\n\nSamples 4 and 7 are siblings; see the 2019 field notes.\n"

    def test_prose_below_the_end_marker_survives(self, tmp_path):
        path = tmp_path / OUTPUT_NAME
        write_document(context(), path)
        path.write_text(path.read_text() + "\n" + self.PROSE)

        write_document(context(), path)
        assert "siblings" in path.read_text()

    def test_prose_survives_a_mode_transition(self, tmp_path):
        """Human content is never collateral damage."""
        path = tmp_path / OUTPUT_NAME
        write_document(context(), path)
        path.write_text(path.read_text() + "\n" + self.PROSE)

        write_document(truncated_context(), path)
        text = path.read_text()
        assert "siblings" in text
        assert "Getting the data" not in text


class TestModeTransition:
    def test_full_to_truncated_removes_the_full_content(self, tmp_path):
        """The risk one managed region exists to eliminate.

        With one marker pair per block, a species moving to truncated
        would have kept its download links and coverage statistics under
        a red banner.
        """
        path = tmp_path / OUTPUT_NAME
        write_document(context(), path)
        assert "Getting the data" in path.read_text()

        write_document(truncated_context(), path)
        text = path.read_text()
        for heading in ("Getting the data", "Sample QC", "References", "aws s3 sync"):
            assert heading not in text

    def test_the_round_trip_returns_to_the_original_bytes(self, tmp_path):
        path = tmp_path / OUTPUT_NAME
        write_document(context(), path)
        original = path.read_text()

        write_document(truncated_context(), path)
        write_document(context(), path)
        assert path.read_text() == original


class TestLinks:
    def test_definitions_sit_at_the_foot_and_are_tagged_by_path(self):
        lines = render_body(context()).splitlines()
        definitions = [line for line in lines if line.startswith("[") and "]: http" in line]
        assert definitions
        # every definition is in the trailing run of the document
        first = lines.index(definitions[0])
        assert all(line.startswith("[") for line in lines[first:])
        assert "[qc/qc_report.tsv]: " in "\n".join(definitions)

    def test_only_cited_objects_get_definitions(self):
        """An unused definition is dead weight in every diff."""
        body = render_body(context())
        definitions = {
            line.split("]:")[0][1:]
            for line in body.splitlines()
            if line.startswith("[") and "]: http" in line
        }
        for tag in definitions:
            assert f"][{tag}]" in body

    def test_the_redundant_genomeark_copies_are_not_linked(self):
        """They duplicate files in this directory, and the S3 sheet is not
        ground truth — S006 exists because it can disagree."""
        body = render_body(context())
        assert "[README.txt]: " not in body
        assert "[sample_sheet.csv]: " not in body


class TestBlockedRenderings:
    def test_missing_and_wrong_read_differently(self):
        missing = render_body(blocked_context())
        wrong = render_body(wrong_context())
        assert "[!WARNING]" in missing and "cannot yet be cited" in missing
        assert "[!CAUTION]" in wrong and "known to be incorrect" in wrong

    def test_no_bioproject_list_is_rendered_either_way(self):
        """A caveated wrong list still gets copy-pasted."""
        for body in (render_body(blocked_context()), render_body(wrong_context())):
            assert "PRJ" not in body

    def test_a_blocked_document_still_describes_and_links_the_data(self):
        body = render_body(blocked_context())
        assert "## Getting the data" in body
        assert "## Sample QC" in body

    def test_the_callout_is_never_greener_than_the_document(self):
        """A PASS species with blocked citations must not show a TIP."""
        body = render_body(blocked_context())
        assert "[!TIP]" not in body


class TestTruncated:
    def test_it_keeps_the_pointers_a_reader_needs(self):
        body = render_body(truncated_context())
        for target in ("VALIDATION.md", "README.txt", "CHECKS.md"):
            assert target in body

    def test_it_does_not_mention_citations(self):
        """No References section exists for the anchor to reach."""
        body = render_body(truncated_context())
        assert "#references" not in body

    def test_it_does_not_report_a_sample_disagreement(self):
        """0 BAMs for an unpublished species is absence, not disagreement.

        The same guard the validator puts on S002/S003: during an absent
        or partial upload the comparison measures upload progress.
        """
        assert "disagree" not in render_body(truncated_context())


class TestReferences:
    """The block that turns "please cite them" into actual citations."""

    def _context(self, entries=(), sra=None, declared=None):
        from congen.core.metadata.citations import CitationIndex
        from tests.conftest_readme import dataset as make_dataset

        index = CitationIndex(entries={e.bioproject: e for e in entries})
        data = make_dataset(sra=sra or {})
        ctx = context(data=data)
        object.__setattr__(ctx, "citations", index)
        if declared is not None and ctx.species.readme is not None:
            ctx.species.readme.bioprojects[:] = declared
        return ctx

    def _entry(self, accession, **kw):
        from congen.core.metadata.citations import Entry, Status

        return Entry(bioproject=accession, status=kw.pop("status", Status.CONFIRMED), **kw)

    def test_a_confirmed_citation_is_rendered_with_its_doi(self):
        from congen.tools.readme.blocks import references

        entry = self._entry(
            "PRJNA1",
            dois=["10.a/x"],
            references={"10.a/x": "Someone (2020) A paper. J."},
            title="A project",
        )
        text = "\n".join(references.render(self._context([entry], declared=["PRJNA1"])).lines)
        assert "Someone (2020) A paper. J." in text
        assert "https://doi.org/10.a/x" in text
        assert "1 of 1 BioProjects" in text

    def test_two_citations_both_render(self):
        from congen.tools.readme.blocks import references

        entry = self._entry(
            "PRJNA1",
            dois=["10.a/x", "10.b/y"],
            references={"10.a/x": "First paper", "10.b/y": "Second paper"},
        )
        text = "\n".join(references.render(self._context([entry], declared=["PRJNA1"])).lines)
        assert "First paper" in text and "Second paper" in text

    def test_an_unreviewed_bioproject_still_gets_a_row(self):
        """The gap is what gets it filled, so it must be visible."""
        from congen.tools.readme.blocks import references

        text = "\n".join(references.render(self._context(declared=["PRJNA_NEW"])).lines)
        assert "PRJNA_NEW" in text
        assert "citation not yet reviewed" in text
        assert "0 of 1 BioProjects" in text

    def test_a_not_found_bioproject_says_so(self):
        from congen.core.metadata.citations import Status
        from congen.tools.readme.blocks import references

        entry = self._entry("PRJNA1", status=Status.NOT_FOUND)
        text = "\n".join(references.render(self._context([entry], declared=["PRJNA1"])).lines)
        assert "no publication found" in text

    def test_coverage_counts_samples_when_the_sra_mapping_is_present(self):
        """One uncited project carrying forty samples matters more than one
        carrying a single sample, so coverage is measured in samples."""
        from congen.tools.readme.blocks import references

        sheet_input = None
        ctx = self._context(declared=[])
        sheet_input = ctx.species.sheet.rows[0].input
        cited = self._entry(
            "PRJ_CITED", dois=["10.a/x"], references={"10.a/x": "Paper"}
        )
        ctx = self._context(
            [cited],
            sra={sheet_input: [{"run": "SRR1", "biosample": "S1", "bioproject": "PRJ_CITED"}]},
            declared=["PRJ_CITED", "PRJ_UNCITED"],
        )
        text = "\n".join(references.render(ctx).lines)
        assert "Citations are recorded for 1 of 1 samples" in text
        assert "1 of 2 BioProjects" in text

    def test_the_pipeline_line_names_what_is_missing(self):
        """Silence would let the tool citations stay missing forever."""
        from congen.tools.readme.blocks import references

        text = "\n".join(references.render(self._context(declared=["PRJNA1"])).lines)
        assert "not recorded yet" in text
        assert "tool_citations.yaml" in text

    def test_a_blocked_block_never_prints_a_bioproject(self):
        """A wrong list with a warning above it still gets copy-pasted."""
        from congen.tools.readme.blocks import references

        blocked = blocked_context()
        object.__setattr__(blocked, "citations", None)
        text = "\n".join(references.render(blocked).lines)
        assert "PRJ" not in text
        assert "cannot yet be cited" in text
