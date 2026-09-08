"""Tests for report rendering and the four writing modes.

Offline throughout: a scratch copy of the fixture metadata stands in for
congen-metadata, and nothing touches the network.
"""

from __future__ import annotations

import json
import shutil

import pytest
from click.testing import CliRunner

from congen.core.findings import Finding, Report, Severity
from congen.core.metadata.discovery import SpeciesRepo
from congen.core.report.markdown import render_corpus_report, render_species_report
from congen.core.status import build_upload_status
from congen.core.validation_record import (
    REPORT_JSON,
    REPORT_MARKDOWN,
    ReportState,
    ValidationRecord,
    load_record,
    mark_stale,
    utc_now,
)
from congen.tools.validate.cli import validate
from congen.tools.validate.reports import (
    check_stale,
    mark_species_stale,
    species_display_name,
    write_corpus_report,
    write_species_report,
)
from tests.conftest import METADATA_ROOT


@pytest.fixture
def sandbox(tmp_path):
    """A writable copy of the fixture metadata repo."""
    root = tmp_path / "congen-metadata"
    shutil.copytree(METADATA_ROOT, root)
    return SpeciesRepo(root)


def record_for_state(state: ReportState, **kwargs) -> ValidationRecord:
    defaults = dict(
        subject="birds/example",
        state=state,
        validated_at="2026-09-08T12:00:00Z",
        tool_version="0.1.0",
        catalog="sha256:cat",
        checks_run=[f"C{i:03d}" for i in range(45)],
        checks_available=45,
        inputs={"config.yaml": "sha256:abc"},
        data={"published": True, "accession": "GCA_1.1"},
    )
    defaults.update(kwargs)
    return ValidationRecord(**defaults)


class TestSpeciesMarkdown:
    def test_the_verdict_is_in_the_first_three_lines(self):
        """All most readers will get to."""
        out = render_species_report(record_for_state(ReportState.PASS), "Podarcis raffonei")
        head = out.split("\n")[:3]
        assert head[0] == "# Validation — Podarcis raffonei"
        assert "**PASS**" in head[2]
        assert "2026-09-08" in head[2]
        assert "GCA_1.1" in head[2]

    def test_pending_explains_that_it_is_expected(self):
        out = render_species_report(record_for_state(ReportState.PENDING), "X")
        assert "**PENDING**" in out
        assert "expected state" in out
        assert "committed before a run is published" in out

    def test_fail_lists_the_errors(self):
        record = record_for_state(
            ReportState.FAIL,
            findings=[
                {
                    "id": "F020",
                    "severity": "error",
                    "message": "wrong assembly",
                    "detail": "not a namespace variant",
                    "path": "species/birds/x/config.yaml",
                    "line": None,
                }
            ],
        )
        out = render_species_report(record, "X")
        assert "## Errors" in out
        assert "**F020** wrong assembly" in out
        assert "not a namespace variant" in out
        assert "`species/birds/x/config.yaml`" in out

    def test_counts_appear_when_known(self):
        status = build_upload_status(
            subject="x",
            declared_accession="GCA_1.1",
            resolved_accession="GCA_1.1",
            sheet_sample_count=21,
        )
        out = render_species_report(
            record_for_state(ReportState.PASS, status=status.as_dict()), "X"
        )
        assert "21 samples in the sheet" in out

    def test_a_partial_selection_is_called_out(self):
        record = record_for_state(ReportState.PASS, checks_run=["C001"], checks_available=45)
        out = render_species_report(record, "X")
        assert "1 of 45 checks" in out
        assert "not a full validation" in out

    def test_provenance_records_the_digests(self):
        out = render_species_report(record_for_state(ReportState.PASS), "X")
        assert "`config.yaml` `sha256:abc`" in out
        assert "congen-metadata-tools 0.1.0" in out

    def test_it_says_it_is_generated(self):
        assert "do not edit by hand" in render_species_report(
            record_for_state(ReportState.PASS), "X"
        )

    def test_skipped_checks_are_summarized_by_reason(self):
        record = record_for_state(
            ReportState.PENDING,
            findings=[
                {"id": "S001", "severity": "skipped", "message": "s", "detail": "needs vcf_header"},
                {"id": "S003", "severity": "skipped", "message": "s", "detail": "needs vcf_header"},
            ],
        )
        out = render_species_report(record, "X")
        assert "2 skipped: needs vcf_header" in out


class TestStaleMarkdown:
    def _stale(self):
        original = record_for_state(
            ReportState.PASS,
            findings=[
                {"id": "P010", "severity": "info", "message": "versions", "detail": None}
            ],
        )
        return mark_stale(original, "config.yaml changed since validation")

    def test_the_banner_replaces_the_verdict(self):
        out = render_species_report(self._stale(), "X")
        assert "**STALE**" in out.split("\n")[2]
        assert "config.yaml changed since validation" in out
        assert "no longer describes these files" in out

    def test_superseded_findings_are_not_presented_as_current(self):
        """The whole point of stamping: the old verdict must not read as live."""
        out = render_species_report(self._stale(), "X")
        assert "## Notes" not in out
        assert "<details>" in out
        assert "Superseded result — PASS" in out
        assert "**P010** versions" in out.split("<details>")[1]


class TestCorpusMarkdown:
    def test_tabulates_states_and_links_each_species(self):
        records = [
            ("species/birds/a", record_for_state(ReportState.PASS, subject="birds/a")),
            ("species/birds/b", record_for_state(ReportState.FAIL, subject="birds/b",
                                                 findings=[{"id": "E", "severity": "error",
                                                            "message": "m", "detail": None}])),
        ]
        out = render_corpus_report(
            records, validated_at=utc_now(), tool_version="0.1.0",
            corpus_findings=[{"id": "G020", "message": "orphan accession"}],
        )
        assert "| **PASS** | 1 |" in out
        assert "| **FAIL** | 1 |" in out
        assert "[birds/a](species/birds/a/VALIDATION.md)" in out
        assert "1 error" in out
        assert "**G020** orphan accession" in out


class TestWriting:
    def test_writes_both_files_into_the_species_directory(self, sandbox):
        species = sandbox.load("reptiles/podarcis-raffonei")
        result = write_species_report(species, record_for_state(ReportState.PASS))
        assert result.changed
        assert (species.path / REPORT_MARKDOWN).exists()
        assert (species.path / REPORT_JSON).exists()
        assert load_record(species.path / REPORT_JSON).state is ReportState.PASS

    def test_rewriting_identical_content_is_a_no_op(self, sandbox):
        species = sandbox.load("reptiles/podarcis-raffonei")
        record = record_for_state(ReportState.PASS)
        assert write_species_report(species, record).changed
        assert not write_species_report(species, record).changed

    def test_finding_paths_are_repo_relative(self, sandbox):
        """These files are committed; an absolute path bakes in whoever ran it."""
        from congen.core.findings import Location
        from congen.core.validation_record import build_record

        species = sandbox.load("reptiles/podarcis-raffonei")
        record = build_record(
            subject=species.key,
            findings=[
                Finding(
                    "F020",
                    Severity.ERROR,
                    species.key,
                    "wrong assembly",
                    location=Location(species.path / "config.yaml", 7),
                )
            ],
            status=None,
            species_dir=species.path,
            inventory=None,
            accession=None,
            checks_run=["F020"],
            checks_available=45,
            catalog="c",
            tool_version="0.1.0",
            root=sandbox.root,
        )
        assert record.findings[0]["path"] == (
            "species/reptiles/podarcis-raffonei/config.yaml"
        )
        assert str(sandbox.root) not in render_species_report(record, "X")

    def test_display_name_prefers_the_reference_name(self, sandbox):
        assert species_display_name(sandbox.load("reptiles/podarcis-raffonei")) == (
            "Podarcis raffonei"
        )

    def test_display_name_falls_back_when_the_name_is_an_accession(self, sandbox):
        """sturnus-vulgaris has an accession in reference.name."""
        assert species_display_name(sandbox.load("birds/sturnus-vulgaris")) == (
            "Sturnus Vulgaris"
        )

    def test_corpus_report_covers_every_species_with_a_record(self, sandbox):
        for key in ("reptiles/podarcis-raffonei", "birds/anser-albifrons"):
            write_species_report(
                sandbox.load(key), record_for_state(ReportState.PASS, subject=key)
            )
        result = write_corpus_report(sandbox, Report(subjects=[]))
        assert result.changed
        text = (sandbox.root / "VALIDATION.md").read_text()
        assert "podarcis-raffonei" in text
        assert "anser-albifrons" in text
        payload = json.loads((sandbox.root / "validation.json").read_text())
        assert payload["counts"]["PASS"] == 2


class TestOfflineStalenessModes:
    def _validated(self, sandbox, key="reptiles/podarcis-raffonei"):
        from congen.core.validation_record import input_digests

        species = sandbox.load(key)
        record = record_for_state(
            ReportState.PASS, subject=key, inputs=input_digests(species.path)
        )
        write_species_report(species, record)
        return species

    def test_check_stale_is_quiet_when_nothing_changed(self, sandbox):
        self._validated(sandbox)
        assert check_stale(sandbox) == []

    def test_check_stale_names_the_changed_file(self, sandbox):
        species = self._validated(sandbox)
        (species.path / "config.yaml").write_text("reference:\n  source: GCA_9.9\n")
        outstanding = check_stale(sandbox)
        assert len(outstanding) == 1
        assert "config.yaml changed" in outstanding[0][1]

    def test_a_species_never_validated_is_not_stale(self, sandbox):
        """Absent is not the same as out of date."""
        assert check_stale(sandbox) == []

    def test_mark_stale_stamps_and_then_settles(self, sandbox):
        species = self._validated(sandbox)
        (species.path / "config.yaml").write_text("reference:\n  source: GCA_9.9\n")

        written = mark_species_stale(sandbox)
        assert [w.subject for w in written] == ["reptiles/podarcis-raffonei"]
        assert load_record(species.path / REPORT_JSON).state is ReportState.STALE

        # Stamping is idempotent, and the check is satisfied afterwards.
        assert mark_species_stale(sandbox) == []
        assert check_stale(sandbox) == []

    def test_the_offline_modes_ignore_the_catalog(self, sandbox):
        """Otherwise --check-stale would depend on whether --check-sra was passed."""
        self._validated(sandbox)
        assert check_stale(sandbox, catalog="sha256:completely-different") == []


class TestCliModes:
    def _run(self, sandbox, *args):
        return CliRunner().invoke(
            validate, ["--metadata-root", str(sandbox.root), *args]
        )

    def test_write_reports_needs_a_selection(self, sandbox):
        result = self._run(sandbox, "--write-reports")
        assert result.exit_code == 2
        assert "--all, or --stale" in result.output

    def test_mark_stale_runs_offline_and_exits_zero(self, sandbox):
        result = self._run(sandbox, "--mark-stale")
        assert result.exit_code == 0
        assert "stamped STALE" in result.output

    def test_check_stale_exits_zero_when_clean(self, sandbox):
        result = self._run(sandbox, "--check-stale")
        assert result.exit_code == 0
        assert "describe their current inputs" in result.output

    def test_check_stale_fails_and_says_what_to_run(self, sandbox):
        from congen.core.validation_record import input_digests

        species = sandbox.load("reptiles/podarcis-raffonei")
        write_species_report(
            species,
            record_for_state(
                ReportState.PASS,
                subject=species.key,
                inputs=input_digests(species.path),
            ),
        )
        (species.path / "config.yaml").write_text("reference:\n  source: GCA_9.9\n")

        result = self._run(sandbox, "--check-stale")
        assert result.exit_code == 1
        assert "config.yaml changed" in result.output
        assert "congen validate --mark-stale" in result.output


class TestChecksReference:
    """The lookup table for the IDs that appear in reports."""

    def _render(self):
        import congen.tools.validate.checks  # noqa: F401 - registers catalog
        from congen.core.report.markdown import render_checks_reference
        from congen.tools.validate.registry import registry

        return render_checks_reference(registry.all, tool_version="0.1.0")

    def test_every_registered_check_has_an_entry(self):
        import congen.tools.validate.checks  # noqa: F401
        from congen.tools.validate.registry import registry

        out = self._render()
        for check in registry.all:
            assert f"### {check.id}" in out, check.id

    def test_entries_carry_severity_and_summary(self):
        out = self._render()
        assert "**error** — reference.source is the VGP main-haplotype assembly" in out

    def test_docstrings_become_the_explanation(self):
        out = self._render()
        assert "The config names an assembly that is not the VGP reference." in out

    def test_every_docstring_paragraph_is_kept(self):
        """The headline is paragraph one; why it matters is paragraph two."""
        out = self._render()
        assert "Not a GCA/GCF namespace variant of the right one" in out

    def test_it_explains_what_skipped_means(self):
        """A skipped check is not a pass, and a reader must not read it as one."""
        out = self._render()
        assert "skipped" in out
        assert "not a pass" in out

    def test_grouped_by_tier(self):
        out = self._render()
        for title in ("Sample identity", "External accessions"):
            assert f"## {title}" in out

    def test_it_says_it_is_generated(self):
        assert "do not edit by hand" in self._render()


class TestChecksLinking:
    def test_finding_ids_link_to_the_reference(self, sandbox):
        from congen.tools.validate.reports import checks_href, write_species_report

        species = sandbox.load("reptiles/podarcis-raffonei")
        assert checks_href(species, sandbox.root) == "../../../CHECKS.md"

        record = record_for_state(
            ReportState.FAIL,
            findings=[
                {"id": "F020", "severity": "error", "message": "m", "detail": None}
            ],
        )
        write_species_report(species, record, root=sandbox.root)
        text = (species.path / REPORT_MARKDOWN).read_text()
        assert "[F020](../../../CHECKS.md#f020)" in text

    def test_no_link_without_a_root(self, sandbox):
        from congen.tools.validate.reports import write_species_report

        species = sandbox.load("reptiles/podarcis-raffonei")
        record = record_for_state(
            ReportState.FAIL,
            findings=[
                {"id": "F020", "severity": "error", "message": "m", "detail": None}
            ],
        )
        write_species_report(species, record)
        assert "**F020**" in (species.path / REPORT_MARKDOWN).read_text()

    def test_the_reference_is_written_beside_the_reports(self, sandbox):
        import congen.tools.validate.checks  # noqa: F401
        from congen.tools.validate.reports import write_checks_reference
        from congen.tools.validate.registry import registry

        assert write_checks_reference(sandbox, registry.all).changed
        assert (sandbox.root / "CHECKS.md").exists()
