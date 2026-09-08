"""Tests for the findings framework."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from congen.core.findings import (
    CheckRegistry,
    Finding,
    Location,
    Report,
    Severity,
)


@dataclass
class FakeContext:
    subject: str = "birds/example"
    available: set[str] = field(default_factory=set)


def build_registry() -> CheckRegistry:
    registry = CheckRegistry()

    @registry.register(id="A001", tier="A", severity=Severity.ERROR, summary="always fires")
    def always(context):
        return [Finding("A001", Severity.ERROR, context.subject, "bang")]

    @registry.register(
        id="A002", tier="A", severity=Severity.WARN, summary="wants a vcf", needs=("vcf",)
    )
    def wants_vcf(context):
        return [Finding("A002", Severity.WARN, context.subject, "vcf seen")]

    @registry.register(id="B001", tier="B", severity=Severity.ERROR, summary="explodes")
    def explodes(context):
        raise RuntimeError("kaboom")

    @registry.register(id="B002", tier="B", severity=Severity.INFO, summary="silent")
    def silent(context):
        return []

    return registry


class TestSeverity:
    def test_ranks_worst_first(self):
        ordered = sorted(Severity, key=lambda s: s.rank)
        assert ordered == [Severity.ERROR, Severity.WARN, Severity.INFO, Severity.SKIPPED]

    def test_str_is_the_value(self):
        assert str(Severity.ERROR) == "error"


class TestRegistration:
    def test_duplicate_ids_are_rejected(self):
        registry = CheckRegistry()

        @registry.register(id="A001", tier="A", severity=Severity.ERROR, summary="one")
        def first(context):
            return []

        with pytest.raises(ValueError, match="already registered"):

            @registry.register(id="A001", tier="A", severity=Severity.ERROR, summary="two")
            def second(context):
                return []

    def test_catalog_is_id_ordered(self):
        assert [c.id for c in build_registry().all] == ["A001", "A002", "B001", "B002"]

    def test_membership_and_lookup(self):
        registry = build_registry()
        assert "A001" in registry
        assert registry.get("A001").summary == "always fires"
        assert registry.get("nope") is None


class TestSelection:
    def test_only_matches_prefixes(self):
        registry = build_registry()
        assert [c.id for c in registry.select(only=["A"])] == ["A001", "A002"]
        assert [c.id for c in registry.select(only=["A002", "B"])] == ["A002", "B001", "B002"]

    def test_skip_removes(self):
        registry = build_registry()
        assert [c.id for c in registry.select(skip=["B"])] == ["A001", "A002"]

    def test_only_then_skip(self):
        registry = build_registry()
        assert [c.id for c in registry.select(only=["A"], skip=["A002"])] == ["A001"]

    def test_empty_strings_are_ignored(self):
        registry = build_registry()
        assert len(registry.select(only=["", "  "])) == 4

    def test_required_context_is_the_union(self):
        registry = build_registry()
        assert registry.required_context(registry.all) == {"vcf"}
        assert registry.required_context(registry.select(only=["B"])) == set()


class TestRunning:
    def test_missing_needs_yields_skipped_not_error(self):
        """A species with no VCF must not produce false sample mismatches."""
        registry = build_registry()
        findings = registry.run(FakeContext(), registry.select(only=["A002"]))
        assert len(findings) == 1
        assert findings[0].severity is Severity.SKIPPED
        assert "needs vcf" in findings[0].detail

    def test_satisfied_needs_run_normally(self):
        registry = build_registry()
        context = FakeContext(available={"vcf"})
        findings = registry.run(context, registry.select(only=["A002"]))
        assert [f.severity for f in findings] == [Severity.WARN]

    def test_a_raising_check_becomes_an_error_finding(self):
        """One malformed species must not hide the other 78."""
        registry = build_registry()
        findings = registry.run(FakeContext(), registry.select(only=["B001"]))
        assert len(findings) == 1
        assert findings[0].severity is Severity.ERROR
        assert "RuntimeError" in findings[0].message
        assert "kaboom" in findings[0].message

    def test_runs_every_check_when_none_given(self):
        registry = build_registry()
        ids = {f.id for f in registry.run(FakeContext())}
        assert ids == {"A001", "A002", "B001"}  # B002 returns nothing


class TestReport:
    def _report(self) -> Report:
        report = Report(subjects=["birds/a", "birds/b"])
        report.extend(
            [
                Finding("A001", Severity.ERROR, "birds/a", "e"),
                Finding("A002", Severity.WARN, "birds/a", "w"),
                Finding("B002", Severity.INFO, "birds/b", "i"),
                Finding("A002", Severity.SKIPPED, "birds/b", "s"),
            ]
        )
        return report

    def test_counts(self):
        counts = self._report().counts()
        assert counts[Severity.ERROR] == 1
        assert counts[Severity.WARN] == 1
        assert counts[Severity.INFO] == 1
        assert counts[Severity.SKIPPED] == 1

    def test_grouping_sorts_worst_first(self):
        grouped = self._report().by_subject()
        assert list(grouped) == ["birds/a", "birds/b"]
        assert [f.severity for f in grouped["birds/a"]] == [Severity.ERROR, Severity.WARN]

    def test_subjects_with_no_findings_still_appear(self):
        report = Report(subjects=["birds/clean"])
        assert report.by_subject() == {"birds/clean": []}

    def test_exit_codes(self):
        report = self._report()
        assert report.exit_code() == 1  # has an error

        warn_only = Report(subjects=["x"])
        warn_only.extend([Finding("A002", Severity.WARN, "x", "w")])
        assert warn_only.exit_code() == 0
        assert warn_only.exit_code(strict=True) == 1

        clean = Report(subjects=["x"])
        assert clean.exit_code() == 0
        assert clean.exit_code(strict=True) == 0

    def test_info_and_skipped_never_fail_even_when_strict(self):
        report = Report(subjects=["x"])
        report.extend(
            [
                Finding("B002", Severity.INFO, "x", "i"),
                Finding("A002", Severity.SKIPPED, "x", "s"),
            ]
        )
        assert report.exit_code(strict=True) == 0


class TestFindingSerialization:
    def test_as_dict_flattens_the_location(self):
        finding = Finding(
            "S001",
            Severity.ERROR,
            "birds/a",
            "mismatch",
            detail="extra sample",
            location=Location(Path("species/birds/a/sample_sheet.csv"), 14),
        )
        assert finding.as_dict() == {
            "id": "S001",
            "severity": "error",
            "subject": "birds/a",
            "message": "mismatch",
            "detail": "extra sample",
            "path": "species/birds/a/sample_sheet.csv",
            "line": 14,
        }

    def test_as_dict_without_a_location(self):
        payload = Finding("G003", Severity.WARN, "birds/a", "no data").as_dict()
        assert payload["path"] is None and payload["line"] is None
