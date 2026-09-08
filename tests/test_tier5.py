"""Tests for tier 5 — the sheet against NCBI SRA."""

from __future__ import annotations

import pytest

from congen.core.findings import Severity
from congen.core.metadata.discovery import SpeciesRepo
from congen.core.metadata.loaders import parse_sample_sheet
from congen.core.remote.sra import RunIndex, RunInfo
from congen.tools.validate import checks as _checks  # noqa: F401 - registers catalog
from congen.tools.validate.context import README, SHEET, SRA, Context
from congen.tools.validate.registry import registry
from congen.tools.validate.runner import RunOptions, selected_checks
from tests.conftest import METADATA_ROOT


@pytest.fixture
def repo() -> SpeciesRepo:
    return SpeciesRepo(METADATA_ROOT)


def make_index(mapping: dict[str, list[RunInfo]]) -> RunIndex:
    return RunIndex(
        resolved={k: tuple(v) for k, v in mapping.items()},
        missing=tuple(k for k, v in mapping.items() if not v),
    )


def make_context(repo, sheet_text: str, index: RunIndex, *, with_readme: bool = True) -> Context:
    species = repo.load("reptiles/podarcis-raffonei")
    species.sheet.rows = parse_sample_sheet(sheet_text, species.sheet.path).rows
    if not with_readme:
        species.readme = None
    context = Context(species=species)
    context.sra = index
    context.available = {SHEET, SRA} | ({README} if with_readme else set())
    return context


def fire(context: Context, *ids: str):
    out = []
    for check_id in ids:
        for finding in registry.run(context, [registry.get(check_id)]):
            if finding.severity is not Severity.SKIPPED:
                out.append((finding.id, finding.severity))
    return out


class TestOptIn:
    def test_sra_checks_are_excluded_by_default(self):
        """Excluded, not skipped: an ordinary report is not padded."""
        default = {c.id for c in selected_checks(RunOptions())}
        enabled = {c.id for c in selected_checks(RunOptions(check_sra=True))}
        assert enabled - default == {"E001", "E002", "E003", "R017"}
        assert not {"E001", "R017"} & default

    def test_gating_is_by_declared_need_not_by_id(self):
        for check in selected_checks(RunOptions(check_sra=True)):
            if check.id in {"E001", "E002", "E003", "R017"}:
                assert SRA in check.needs, check.id


class TestE001:
    def test_a_matching_biosample_is_clean(self, repo):
        index = make_index(
            {"SRR1": [RunInfo(run="SRR1", biosample="SAMN18355762", bioproject="PRJNA715201")]}
        )
        context = make_context(
            repo, "sample_id,input_type,input\nSAMN18355762,srr,SRR1\n", index
        )
        assert fire(context, "E001") == []

    def test_a_run_belonging_to_another_sample_errors(self, repo):
        index = make_index(
            {"SRR1": [RunInfo(run="SRR1", biosample="SAMN99999999", bioproject="PRJNA1")]}
        )
        context = make_context(
            repo, "sample_id,input_type,input\nSAMN18355762,srr,SRR1\n", index
        )
        findings = registry.run(context, [registry.get("E001")])
        assert findings[0].severity is Severity.ERROR
        assert "SAMN99999999" in findings[0].message
        assert "SAMN18355762" in findings[0].message

    def test_an_experiment_resolves_through_its_runs(self, repo):
        index = make_index(
            {
                "ERX1": [
                    RunInfo(run="ERR1", experiment="ERX1", biosample="SAMEA1"),
                    RunInfo(run="ERR2", experiment="ERX1", biosample="SAMEA1"),
                ]
            }
        )
        context = make_context(repo, "sample_id,input_type,input\nSAMEA1,srr,ERX1\n", index)
        assert fire(context, "E001") == []

    def test_an_unknown_accession_errors(self, repo):
        context = make_context(
            repo, "sample_id,input_type,input\nSAMN1,srr,SRR404\n", make_index({"SRR404": []})
        )
        findings = registry.run(context, [registry.get("E001")])
        assert findings[0].severity is Severity.ERROR
        assert "unknown to SRA" in findings[0].message

    def test_local_path_inputs_are_ignored(self, repo):
        context = make_context(
            repo,
            "sample_id,input_type,input\nSAMN1,fastq,/scratch/a_1.fq.gz\n",
            make_index({}),
        )
        assert fire(context, "E001") == []


class TestE002AndE003:
    SHEET = "sample_id,input_type,input\nSAMN18355762,srr,SRR1\n"

    def test_agreement_is_clean(self, repo):
        index = make_index(
            {"SRR1": [RunInfo(run="SRR1", biosample="SAMN18355762", bioproject="PRJNA1089471")]}
        )
        context = make_context(repo, self.SHEET, index)
        # The fixture README cites PRJNA1089471 and PRJNA715201; the second
        # contributing nothing is E003's business, checked separately.
        assert fire(context, "E002") == []

    def test_an_undocumented_bioproject_warns(self, repo):
        index = make_index(
            {"SRR1": [RunInfo(run="SRR1", biosample="SAMN18355762", bioproject="PRJNA9999999")]}
        )
        context = make_context(repo, self.SHEET, index)
        findings = registry.run(context, [registry.get("E002")])
        assert findings[0].severity is Severity.WARN
        assert "PRJNA9999999" in findings[0].detail

    def test_an_idle_documented_bioproject_warns(self, repo):
        """The README cites two projects; only one contributes runs."""
        index = make_index(
            {"SRR1": [RunInfo(run="SRR1", biosample="SAMN18355762", bioproject="PRJNA1089471")]}
        )
        context = make_context(repo, self.SHEET, index)
        findings = registry.run(context, [registry.get("E003")])
        assert findings[0].severity is Severity.WARN
        assert "PRJNA715201" in findings[0].detail

    def test_a_readme_citing_nothing_makes_no_claim(self, repo, tmp_path):
        index = make_index(
            {"SRR1": [RunInfo(run="SRR1", biosample="SAMN18355762", bioproject="PRJNA1")]}
        )
        context = make_context(repo, self.SHEET, index)
        context.species.readme.bioprojects = []
        assert fire(context, "E002", "E003") == []

    def test_without_a_readme_both_are_skipped(self, repo):
        index = make_index(
            {"SRR1": [RunInfo(run="SRR1", biosample="SAMN18355762", bioproject="PRJNA1")]}
        )
        context = make_context(repo, self.SHEET, index, with_readme=False)
        for check_id in ("E002", "E003"):
            findings = registry.run(context, [registry.get(check_id)])
            assert [f.severity for f in findings] == [Severity.SKIPPED], check_id


class TestR017:
    """Error on genuine ambiguity, info on an imprecise name."""

    def test_a_multi_run_experiment_errors(self, repo):
        index = make_index(
            {
                "ERX1": [
                    RunInfo(run="ERR1", experiment="ERX1", biosample="SAMEA1"),
                    RunInfo(run="ERR2", experiment="ERX1", biosample="SAMEA1"),
                    RunInfo(run="ERR3", experiment="ERX1", biosample="SAMEA1"),
                ]
            }
        )
        context = make_context(repo, "sample_id,input_type,input\nSAMEA1,srr,ERX1\n", index)
        findings = [f for f in registry.run(context, [registry.get("R017")])]
        assert [f.severity for f in findings] == [Severity.ERROR]
        assert "contains 3 runs" in findings[0].message
        assert "does not identify which reads" in findings[0].message
        assert "ERR1" in findings[0].detail

    def test_a_single_run_experiment_is_informational(self, repo):
        """All 42 experiment accessions in anser-anser are this case."""
        index = make_index({"ERX1": [RunInfo(run="ERR1", experiment="ERX1", biosample="SAMEA1")]})
        context = make_context(repo, "sample_id,input_type,input\nSAMEA1,srr,ERX1\n", index)
        findings = registry.run(context, [registry.get("R017")])
        assert [f.severity for f in findings] == [Severity.INFO]
        assert "exactly one run" in findings[0].message

    def test_run_accessions_produce_nothing(self, repo):
        index = make_index({"SRR1": [RunInfo(run="SRR1", biosample="SAMN1")]})
        context = make_context(repo, "sample_id,input_type,input\nSAMN1,srr,SRR1\n", index)
        assert fire(context, "R017") == []

    def test_both_severities_can_appear_together(self, repo):
        index = make_index(
            {
                "ERX1": [
                    RunInfo(run="ERR1", experiment="ERX1", biosample="SAMEA1"),
                    RunInfo(run="ERR2", experiment="ERX1", biosample="SAMEA1"),
                ],
                "ERX2": [RunInfo(run="ERR3", experiment="ERX2", biosample="SAMEA2")],
            }
        )
        context = make_context(
            repo,
            "sample_id,input_type,input\nSAMEA1,srr,ERX1\nSAMEA2,srr,ERX2\n",
            index,
        )
        assert fire(context, "R017") == [
            ("R017", Severity.ERROR),
            ("R017", Severity.INFO),
        ]

    def test_an_unresolvable_experiment_is_left_to_e001(self, repo):
        index = make_index({"ERX404": []})
        context = make_context(repo, "sample_id,input_type,input\nSAMEA1,srr,ERX404\n", index)
        assert fire(context, "R017") == []
        assert fire(context, "E001") == [("E001", Severity.ERROR)]
