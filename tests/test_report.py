"""Tests for the report renderers."""

from __future__ import annotations

import json
from pathlib import Path

from congen.core.findings import Finding, Location, Report, Severity
from congen.core.report import render_github, render_human, render_json
from congen.core.report.render import render_status

ROOT = Path("/repo/congen-metadata")


def sample_report() -> Report:
    report = Report(
        subjects=["birds/anser-albifrons", "reptiles/podarcis-raffonei"],
        checks_run=["S001", "G016"],
    )
    report.extend(
        [
            Finding(
                "S001",
                Severity.ERROR,
                "birds/anser-albifrons",
                "sheet and VCF differ",
                detail="only in VCF: SAMEA112262514",
                location=Location(ROOT / "species/birds/anser-albifrons/sample_sheet.csv", 14),
            ),
            Finding("G016", Severity.INFO, "birds/anser-albifrons", "filtered.vcf.gz absent"),
            Finding(
                "S002", Severity.SKIPPED, "reptiles/podarcis-raffonei", "skipped", detail="needs s3"
            ),
        ]
    )
    return report


class TestHuman:
    def test_groups_by_subject_with_a_summary(self):
        out = render_human(sample_report())
        assert "birds/anser-albifrons" in out
        assert "error S001" in out
        assert "info  G016" in out
        assert out.strip().endswith("2 species: 1 error, 1 info, 1 skipped")

    def test_relativizes_paths_against_the_root(self):
        out = render_human(sample_report(), root=ROOT)
        assert "at species/birds/anser-albifrons/sample_sheet.csv:14" in out
        assert "/repo/" not in out

    def test_leaves_outside_paths_absolute(self):
        out = render_human(sample_report(), root=Path("/somewhere/else"))
        assert "/repo/congen-metadata/species" in out

    def test_a_clean_subject_says_ok_and_counts_skips(self):
        out = render_human(sample_report())
        assert "reptiles/podarcis-raffonei\n  ok (1 skipped)" in out

    def test_show_skipped_lists_them(self):
        out = render_human(sample_report(), show_skipped=True)
        assert "skip  S002" in out

    def test_quiet_hides_info(self):
        out = render_human(sample_report(), show_info=False)
        assert "G016" not in out
        assert "S001" in out

    def test_a_wholly_clean_report(self):
        out = render_human(Report(subjects=["birds/clean"]))
        assert "birds/clean\n  ok" in out
        assert "clean" in out.splitlines()[-1]

    def test_single_subject_summary_names_it(self):
        report = Report(subjects=["birds/only"])
        assert render_human(report).strip().endswith("birds/only: clean")


class TestJson:
    def test_shape_is_stable(self):
        payload = json.loads(render_json(sample_report()))
        assert payload["subjects"] == [
            "birds/anser-albifrons",
            "reptiles/podarcis-raffonei",
        ]
        assert payload["checks_run"] == ["S001", "G016"]
        assert payload["counts"]["error"] == 1
        assert payload["counts"]["skipped"] == 1
        assert len(payload["findings"]) == 3
        assert payload["findings"][0]["line"] == 14

    def test_extra_fields_merge(self):
        payload = json.loads(render_json(sample_report(), extra={"tool": "validate"}))
        assert payload["tool"] == "validate"


class TestGithub:
    def test_maps_severities_to_annotation_levels(self):
        out = render_github(sample_report().findings, root=ROOT)
        assert out.startswith("::error ")
        assert "::notice " in out

    def test_skipped_findings_are_not_annotations(self):
        assert "S002" not in render_github(sample_report().findings, root=ROOT)

    def test_paths_are_workspace_relative(self):
        """GitHub only anchors an annotation to a relative path."""
        out = render_github(sample_report().findings, root=ROOT)
        assert "file=species/birds/anser-albifrons/sample_sheet.csv" in out
        assert "line=14" in out

    def test_detail_is_appended_to_the_message(self):
        out = render_github(sample_report().findings, root=ROOT)
        assert "sheet and VCF differ — only in VCF: SAMEA112262514" in out

    def test_property_values_are_escaped(self):
        finding = Finding(
            "X001",
            Severity.ERROR,
            "birds/a",
            "line one\nline two",
            detail="a,b:c",
            location=Location(Path("f.csv")),
        )
        out = render_github([finding])
        assert "%0A" in out  # newline escaped in the message
        assert "\n" not in out

    def test_empty_when_there_is_nothing_to_annotate(self):
        report = Report(subjects=["x"])
        report.extend([Finding("A", Severity.SKIPPED, "x", "s")])
        assert render_github(report.findings) == ""


class TestStatusBlock:
    def _report_with_statuses(self) -> Report:
        from congen.core.status import build_upload_status

        report = Report(subjects=["birds/a", "birds/b", "birds/c"])
        report.add_status(
            build_upload_status(
                subject="birds/a",
                declared_accession="GCA_1.1",
                resolved_accession=None,
                sheet_sample_count=5,
            )
        )

        class FakeInventory:
            exists = True
            subdirs = ("bams", "vcfs", "qc", "callable_sites")
            bam_objects = {"S1": object()}
            vcf_names = {"raw.vcf.gz", "filtered.vcf.gz"}

            def object(self, *path):
                return object() if path[-1] in {"raw.vcf.gz", "raw.vcf.gz.tbi", "README.txt"} else None

        report.add_status(
            build_upload_status(
                subject="birds/b",
                declared_accession="GCF_2.1",
                resolved_accession="GCA_2.1",
                inventory=FakeInventory(),
                sheet_sample_count=1,
                vcf_sample_count=1,
                repo_readme=True,
            )
        )

        class PartialInventory(FakeInventory):
            subdirs = ("bams",)
            vcf_names: set[str] = set()

            def object(self, *path):
                return object() if path[-1] == "README.txt" else None

        report.add_status(
            build_upload_status(
                subject="birds/c",
                declared_accession="GCA_3.1",
                resolved_accession="GCA_3.1",
                inventory=PartialInventory(),
                sheet_sample_count=9,
            )
        )
        return report

    def test_counts_each_state(self):
        from congen.core.status import PublicationState

        counts = self._report_with_statuses().status_counts()
        assert counts[PublicationState.ABSENT] == 1
        assert counts[PublicationState.COMPLETE] == 1
        assert counts[PublicationState.PARTIAL] == 1

    def test_human_report_names_the_incomplete_ones(self):
        out = render_human(self._report_with_statuses())
        assert "publication status" in out
        assert "absent" in out and "a" in out
        assert "partial" in out and "c" in out

    def test_complete_species_are_counted_not_listed(self):
        """64 names would drown the block."""
        out = render_status(self._report_with_statuses())
        line = [l for l in out.splitlines() if "complete" in l][0]
        assert line.split() == ["complete", "1"]

    def test_optional_artifacts_are_summarized(self):
        out = render_status(self._report_with_statuses())
        assert "optional:" in out
        assert "filtered_vcf 1/2" in out  # only b has one; a is absent so excluded

    def test_a_differing_accession_is_noted(self):
        out = render_status(self._report_with_statuses())
        assert "birds/b data is under GCA_2.1, config declares GCF_2.1" in out

    def test_json_carries_statuses_and_counts(self):
        payload = json.loads(render_json(self._report_with_statuses()))
        assert payload["status_counts"]["absent"] == 1
        assert len(payload["statuses"]) == 3
        by_subject = {s["subject"]: s for s in payload["statuses"]}
        assert by_subject["birds/c"]["state"] == "partial"
        assert "raw_vcf" in by_subject["birds/c"]["missing"]
        assert by_subject["birds/b"]["accession_differs"] is True
        assert by_subject["birds/a"]["counts"]["sheet_samples"] == 5

    def test_no_status_block_when_there_are_no_statuses(self):
        assert render_status(Report(subjects=["x"])) == ""
        assert "publication status" not in render_human(Report(subjects=["x"]))
