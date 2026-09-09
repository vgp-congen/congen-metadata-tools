"""Tests for tier 4 — config against recorded provenance.

The corpus agrees everywhere, so these are negative controls plus one
positive control against the real header.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from congen.core.findings import Severity
from congen.core.metadata.discovery import SpeciesRepo
from congen.core.remote.headers import (
    CALLER_EVIDENCE,
    FileByteSource,
    VcfHeader,
    parse_vcf_header_lines,
    read_vcf_header,
)
from congen.tools.validate import checks as _checks  # noqa: F401 - registers catalog
from congen.tools.validate.context import CONFIG, VCF_HEADER, Context
from congen.tools.validate.registry import registry
from tests.conftest import FIXTURE_WINDOW, METADATA_ROOT, REMOTE


@pytest.fixture
def repo() -> SpeciesRepo:
    return SpeciesRepo(METADATA_ROOT)


@pytest.fixture
def podarcis_vcf() -> VcfHeader:
    return read_vcf_header(
        FileByteSource(REMOTE / "podarcis-raffonei.raw.vcf.gz.prefix"),
        initial_window=FIXTURE_WINDOW,
    )


def synthetic(*extra: str) -> VcfHeader:
    lines = ["##fileformat=VCFv4.2", *extra, "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tS1"]
    return parse_vcf_header_lines(lines)


def make_context(repo, header, *, config_overrides: dict | None = None) -> Context:
    species = repo.load("reptiles/podarcis-raffonei")
    if config_overrides:
        for path, value in config_overrides.items():
            keys = path.split(".")
            node = species.config.data
            for key in keys[:-1]:
                node = node[key]
            node[keys[-1]] = value
    context = Context(species=species)
    context.vcf_header = header
    context.available = {CONFIG, VCF_HEADER}
    return context


def fire(context: Context, *ids: str):
    out = []
    for check_id in ids:
        for finding in registry.run(context, [registry.get(check_id)]):
            if finding.severity is not Severity.SKIPPED:
                out.append((finding.id, finding.severity))
    return out


class TestHeaderProvenanceAccessors:
    def test_reads_the_real_invocations(self, podarcis_vcf):
        assert podarcis_vcf.gatk_tool_ids() == [
            "GenomicsDBImport",
            "GenotypeGVCFs",
            "HaplotypeCaller",
        ]
        assert podarcis_vcf.tool_versions() == {"gatk": "4.6.2.0", "bcftools": "1.23.1"}
        assert podarcis_vcf.gatk_argument("sample-ploidy") == "2"
        assert podarcis_vcf.gatk_argument("heterozygosity") == "0.005"

    def test_bcftools_concat_is_not_read_as_a_caller(self, podarcis_vcf):
        """Every GATK-called VCF here also carries bcftools_concatCommand."""
        assert "bcftools_concatCommand" in podarcis_vcf.meta_keys
        assert podarcis_vcf.callers() == {"gatk"}

    @pytest.mark.parametrize(
        "line,expected",
        [
            ('##GATKCommandLine=<ID=HaplotypeCaller,CommandLine="x">', {"gatk"}),
            ("##source=HaplotypeCaller", {"gatk"}),
            ('##bcftools_callCommand=call -mv', {"bcftools"}),
            ("##source=DeepVariant", {"deepvariant"}),
            ("##source=freeBayes v1.3", {"freebayes"}),
            ('##bcftools_concatCommand=concat a b', set()),
            ('##bcftools_normCommand=norm -f ref', set()),
            ('##GATKCommandLine=<ID=GenomicsDBImport,CommandLine="x">', set()),
            ("##fileformat=VCFv4.2", set()),
        ],
    )
    def test_caller_evidence(self, line, expected):
        assert synthetic(line).callers() == expected

    def test_every_evidence_pattern_names_a_known_caller(self):
        callers = {caller for caller, _ in CALLER_EVIDENCE}
        assert callers == {"gatk", "bcftools", "deepvariant", "sentieon", "freebayes"}


class TestTheCorpusCase:
    def test_the_real_header_agrees_with_its_config(self, repo, podarcis_vcf):
        """What all 67 published VCFs look like."""
        assert fire(make_context(repo, podarcis_vcf), "P001", "P002", "P003") == []

    def test_p010_is_retired(self, repo, podarcis_vcf):
        """A version stamp is inventory: there is no action that clears it.

        The header accessor stays, because the readme generator wants it;
        what went is the pretence that it was a validation finding.
        """
        assert "P010" not in registry
        assert "P010" in registry.retired
        assert podarcis_vcf.tool_versions() == {"gatk": "4.6.2.0", "bcftools": "1.23.1"}


class TestPloidy:
    def test_a_disagreement_warns(self, repo, podarcis_vcf):
        context = make_context(
            repo, podarcis_vcf, config_overrides={"variant_calling.ploidy": 4}
        )
        findings = registry.run(context, [registry.get("P001")])
        assert findings[0].severity is Severity.WARN
        assert "config says 4" in findings[0].message
        assert "--sample-ploidy 2" in findings[0].message

    def test_a_string_config_value_still_compares(self, repo, podarcis_vcf):
        context = make_context(
            repo, podarcis_vcf, config_overrides={"variant_calling.ploidy": "2"}
        )
        assert fire(context, "P001") == []

    def test_nothing_recorded_means_no_claim(self, repo):
        context = make_context(repo, synthetic("##source=HaplotypeCaller"))
        assert fire(context, "P001", "P002") == []

    def test_nothing_declared_means_no_claim(self, repo, podarcis_vcf):
        context = make_context(
            repo, podarcis_vcf, config_overrides={"variant_calling.ploidy": None}
        )
        assert fire(context, "P001") == []

    def test_an_uncomparable_value_warns_rather_than_crashing(self, repo, podarcis_vcf):
        context = make_context(
            repo, podarcis_vcf, config_overrides={"variant_calling.ploidy": "diploid"}
        )
        findings = registry.run(context, [registry.get("P001")])
        assert findings[0].severity is Severity.WARN
        assert "cannot compare" in findings[0].message


class TestHeterozygosity:
    def test_a_disagreement_warns(self, repo, podarcis_vcf):
        context = make_context(
            repo, podarcis_vcf, config_overrides={"variant_calling.gatk.het_prior": 0.001}
        )
        findings = registry.run(context, [registry.get("P002")])
        assert findings[0].severity is Severity.WARN
        assert "0.001" in findings[0].message

    def test_equivalent_notation_is_not_a_disagreement(self, repo, podarcis_vcf):
        """0.005 and 5e-3 are the same prior."""
        context = make_context(
            repo, podarcis_vcf, config_overrides={"variant_calling.gatk.het_prior": 5e-3}
        )
        assert fire(context, "P002") == []

    def test_a_tiny_difference_still_matters(self, repo, podarcis_vcf):
        context = make_context(
            repo, podarcis_vcf, config_overrides={"variant_calling.gatk.het_prior": 0.0051}
        )
        assert fire(context, "P002") == [("P002", Severity.WARN)]


class TestCaller:
    def test_a_different_caller_warns(self, repo, podarcis_vcf):
        context = make_context(
            repo, podarcis_vcf, config_overrides={"variant_calling.tool": "bcftools"}
        )
        findings = registry.run(context, [registry.get("P003")])
        assert findings[0].severity is Severity.WARN
        assert "'bcftools'" in findings[0].message
        assert "gatk" in findings[0].message

    def test_parabricks_is_consistent_with_gatk_evidence(self, repo, podarcis_vcf):
        """parabricks emits GATK-compatible headers."""
        context = make_context(
            repo, podarcis_vcf, config_overrides={"variant_calling.tool": "parabricks"}
        )
        assert fire(context, "P003") == []

    def test_an_unrecognized_header_says_nothing(self, repo):
        """Absence of evidence is not evidence of a different caller."""
        context = make_context(repo, synthetic("##source=SomethingNew"))
        assert fire(context, "P003") == []

    def test_a_real_bcftools_call_matches_a_bcftools_config(self, repo):
        context = make_context(
            repo,
            synthetic("##bcftools_callCommand=call -mv in.bcf"),
            config_overrides={"variant_calling.tool": "bcftools"},
        )
        assert fire(context, "P003") == []

    def test_case_and_whitespace_in_the_config_are_tolerated(self, repo, podarcis_vcf):
        context = make_context(
            repo, podarcis_vcf, config_overrides={"variant_calling.tool": "  GATK  "}
        )
        assert fire(context, "P003") == []
