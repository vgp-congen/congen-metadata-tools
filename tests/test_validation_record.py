"""Tests for the validation record and the staleness predicate."""

from __future__ import annotations

import json

import pytest

from congen.core.findings import Finding, Severity
from congen.core.status import PublicationState, UploadStatus, build_upload_status
from congen.core.validation_record import (
    FRESH,
    ReportState,
    ValidationRecord,
    assess,
    assess_data,
    build_record,
    catalog_digest,
    data_digests,
    derive_state,
    file_digest,
    input_digests,
    load_record,
    mark_stale,
    utc_now,
)


@pytest.fixture
def species_dir(tmp_path):
    directory = tmp_path / "foo"
    directory.mkdir()
    (directory / "config.yaml").write_text("reference:\n  source: GCA_1.1\n")
    (directory / "sample_sheet.csv").write_text("sample_id,input_type,input\nS1,srr,SRR1\n")
    return directory


class TestDigests:
    def test_file_digest_is_content_based(self, tmp_path):
        a, b = tmp_path / "a", tmp_path / "b"
        a.write_text("same")
        b.write_text("same")
        assert file_digest(a) == file_digest(b)
        b.write_text("different")
        assert file_digest(a) != file_digest(b)

    def test_a_missing_file_has_no_digest(self, tmp_path):
        assert file_digest(tmp_path / "absent") is None

    def test_input_digests_skip_absent_files(self, species_dir):
        digests = input_digests(species_dir)
        assert set(digests) == {"config.yaml", "sample_sheet.csv"}
        assert all(v.startswith("sha256:") for v in digests.values())

    def test_digests_survive_a_touch(self, species_dir):
        """The whole point: mtime changes, content does not."""
        before = input_digests(species_dir)
        (species_dir / "config.yaml").touch()
        assert input_digests(species_dir) == before


class TestCatalogDigest:
    def _check(self, check_id, severity):
        from types import SimpleNamespace

        return SimpleNamespace(id=check_id, severity=severity)

    def test_order_does_not_matter(self):
        a = [self._check("A001", Severity.ERROR), self._check("B001", Severity.WARN)]
        assert catalog_digest(a) == catalog_digest(list(reversed(a)))

    def test_adding_a_check_changes_it(self):
        base = [self._check("A001", Severity.ERROR)]
        assert catalog_digest(base) != catalog_digest(base + [self._check("B001", Severity.WARN)])

    def test_changing_a_severity_changes_it(self):
        assert catalog_digest([self._check("A001", Severity.ERROR)]) != catalog_digest(
            [self._check("A001", Severity.WARN)]
        )


class TestDeriveState:
    def _status(self, state):
        return UploadStatus(subject="x", state=state)

    def test_any_error_is_fail(self):
        findings = [Finding("E", Severity.ERROR, "x", "m")]
        for state in PublicationState:
            assert derive_state(findings, self._status(state)) is ReportState.FAIL

    def test_absent_without_errors_is_pending(self):
        assert derive_state([], self._status(PublicationState.ABSENT)) is ReportState.PENDING

    def test_partial_without_errors_is_also_pending(self):
        """An incomplete upload cannot be fully validated, so PASS would overstate."""
        findings = [Finding("G010", Severity.WARN, "x", "no VCF")]
        assert derive_state(findings, self._status(PublicationState.PARTIAL)) is ReportState.PENDING

    def test_complete_with_warnings(self):
        findings = [Finding("W", Severity.WARN, "x", "m")]
        assert (
            derive_state(findings, self._status(PublicationState.COMPLETE))
            is ReportState.PASS_WITH_WARNINGS
        )

    def test_complete_and_clean_is_pass(self):
        assert derive_state([], self._status(PublicationState.COMPLETE)) is ReportState.PASS

    def test_info_alone_is_still_a_pass(self):
        findings = [Finding("P010", Severity.INFO, "x", "versions")]
        assert derive_state(findings, self._status(PublicationState.COMPLETE)) is ReportState.PASS

    def test_no_status_is_pending(self):
        assert derive_state([], None) is ReportState.PENDING


class TestBuildAndRoundTrip:
    def test_builds_a_complete_record(self, species_dir):
        record = build_record(
            subject="birds/foo",
            findings=[
                Finding("E001", Severity.ERROR, "birds/foo", "bad"),
                Finding("S002", Severity.SKIPPED, "birds/foo", "skipped"),
            ],
            status=build_upload_status(
                subject="birds/foo", declared_accession="GCA_1.1", resolved_accession=None
            ),
            species_dir=species_dir,
            inventory=None,
            accession="GCA_1.1",
            checks_run=["E001", "S002"],
            checks_available=45,
            catalog="sha256:cat",
            tool_version="0.1.0",
        )
        assert record.state is ReportState.FAIL
        assert record.is_partial_selection
        assert record.data == {"published": False, "accession": "GCA_1.1"}
        assert len(record.findings) == 2

    def test_skipped_findings_do_not_drive_the_state(self, species_dir):
        record = build_record(
            subject="x",
            findings=[Finding("S001", Severity.SKIPPED, "x", "s")],
            status=build_upload_status(
                subject="x", declared_accession=None, resolved_accession=None
            ),
            species_dir=species_dir,
            inventory=None,
            accession=None,
            checks_run=["S001"],
            checks_available=1,
            catalog="c",
            tool_version="0.1.0",
        )
        assert record.state is ReportState.PENDING

    def test_json_round_trip(self, species_dir, tmp_path):
        record = ValidationRecord(
            subject="x",
            state=ReportState.PASS,
            validated_at=utc_now(),
            tool_version="0.1.0",
            catalog="sha256:c",
        )
        path = tmp_path / "validation.json"
        assert record.write_json(path) is True
        assert record.write_json(path) is False  # unchanged
        assert load_record(path) == record

    def test_a_corrupt_record_loads_as_none(self, tmp_path):
        path = tmp_path / "validation.json"
        path.write_text("{ not json")
        assert load_record(path) is None

    def test_a_missing_record_loads_as_none(self, tmp_path):
        assert load_record(tmp_path / "absent.json") is None


class TestStaleness:
    def _record(self, species_dir, **kwargs):
        defaults = dict(
            subject="x",
            state=ReportState.PASS,
            validated_at=utc_now(),
            tool_version="0.1.0",
            catalog="sha256:cat",
            inputs=input_digests(species_dir),
            data={"published": True, "accession": "GCA_1.1", "objects": {}, "bams": {}},
        )
        defaults.update(kwargs)
        return ValidationRecord(**defaults)

    def test_unchanged_inputs_are_fresh(self, species_dir):
        assert assess(self._record(species_dir), species_dir=species_dir) == FRESH

    def test_no_record_is_stale(self, species_dir):
        verdict = assess(None, species_dir=species_dir)
        assert verdict and "no previous report" in verdict.reason

    def test_an_edited_input_is_stale_and_named(self, species_dir):
        record = self._record(species_dir)
        (species_dir / "config.yaml").write_text("reference:\n  source: GCA_2.1\n")
        verdict = assess(record, species_dir=species_dir)
        assert verdict and "config.yaml changed" in verdict.reason

    def test_an_added_input_is_stale(self, species_dir):
        record = self._record(species_dir)
        (species_dir / "README.txt").write_text("Species: X\n")
        verdict = assess(record, species_dir=species_dir)
        assert verdict and "README.txt" in verdict.reason

    def test_a_catalog_change_is_stale(self, species_dir):
        record = self._record(species_dir)
        verdict = assess(record, species_dir=species_dir, catalog="sha256:different")
        assert verdict and "catalog" in verdict.reason

    def test_the_catalog_is_ignored_when_not_supplied(self, species_dir):
        assert assess(self._record(species_dir), species_dir=species_dir) == FRESH

    def test_newly_published_data_is_stale(self, species_dir):
        record = self._record(
            species_dir, data={"published": False, "accession": "GCA_1.1"}
        )
        verdict = assess(
            record, species_dir=species_dir, published_accessions={"GCA_1.1"}
        )
        assert verdict and "has been published" in verdict.reason

    def test_still_unpublished_stays_fresh(self, species_dir):
        record = self._record(
            species_dir, data={"published": False, "accession": "GCA_1.1"}
        )
        assert assess(record, species_dir=species_dir, published_accessions=set()) == FRESH

    def test_a_previous_failure_is_rechecked(self, species_dir):
        record = self._record(species_dir, state=ReportState.FAIL)
        verdict = assess(record, species_dir=species_dir)
        assert verdict and "FAIL" in verdict.reason

    def test_previous_warnings_are_rechecked(self, species_dir):
        record = self._record(
            species_dir,
            findings=[{"id": "W", "severity": "warn", "message": "m"}],
        )
        verdict = assess(record, species_dir=species_dir)
        assert verdict and "warnings" in verdict.reason

    def test_changed_published_data_is_stale(self, species_dir):
        from tests.test_validate_checks import make_inventory

        record = self._record(species_dir)
        inventory = make_inventory("GCA_1.1", bams=["S1"])
        verdict = assess_data(record, inventory, "GCA_1.1")
        assert verdict and "published data changed" in verdict.reason

    def test_identical_published_data_is_fresh(self, species_dir):
        from tests.test_validate_checks import make_inventory

        inventory = make_inventory("GCA_1.1", bams=["S1"])
        record = self._record(species_dir, data=data_digests(inventory, "GCA_1.1"))
        assert assess_data(record, inventory, "GCA_1.1") == FRESH


class TestMarkStale:
    def test_supersedes_without_inventing_a_verdict(self, species_dir):
        original = ValidationRecord(
            subject="x",
            state=ReportState.PASS,
            validated_at="2026-09-08T10:00:00Z",
            tool_version="0.1.0",
            catalog="c",
            findings=[{"id": "P010", "severity": "info", "message": "v"}],
        )
        stale = mark_stale(original, "config.yaml changed since validation")
        assert stale.state is ReportState.STALE
        assert stale.stale_reason == "config.yaml changed since validation"
        assert stale.validated_at == original.validated_at  # not re-dated
        assert stale.superseded["state"] == "PASS"
        assert stale.superseded["findings"] == original.findings

    def test_stamping_twice_does_not_nest(self, species_dir):
        original = ValidationRecord(
            subject="x",
            state=ReportState.PASS,
            validated_at="2026-09-08T10:00:00Z",
            tool_version="0.1.0",
            catalog="c",
        )
        once = mark_stale(original, "a")
        twice = mark_stale(once, "b")
        assert twice is once
        assert twice.superseded.get("superseded") is None
