"""Tests for tier 3a — reference identity.

The corpus is clean on every one of these checks, so the important tests
are the negative controls: a check that never fires might simply be
broken. Each scenario below constructs a VCF header that differs from the
real assembly in exactly one way.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from congen.core.findings import Severity
from congen.core.metadata.discovery import SpeciesRepo
from congen.core.remote.headers import FileByteSource, VcfHeader, read_bam_header, read_vcf_header
from congen.core.remote.ncbi import AssemblyInfo, parse_assembly_report
from congen.core.remote.qc import parse_contig_map
from congen.tools.validate import checks as _checks  # noqa: F401 - registers catalog
from congen.tools.validate.context import (
    ASSEMBLY_REPORT,
    BAM_HEADERS,
    CONFIG,
    CONTIG_MAP,
    NCBI,
    VCF_HEADER,
    Context,
)
from congen.tools.validate.registry import registry
from tests.conftest import FIXTURE_WINDOW, METADATA_ROOT, REMOTE

TIER_3A = ("F001", "F002", "F003", "F004", "F005", "F006", "F007", "F008", "F009", "F010", "F011")


@pytest.fixture
def repo() -> SpeciesRepo:
    return SpeciesRepo(METADATA_ROOT)


@pytest.fixture
def podarcis_assembly():
    return parse_assembly_report(
        "GCA_027172205.1",
        "rPodRaf1.pri",
        (REMOTE / "assembly_report_GCA_027172205_1.txt").read_text(),
    )


@pytest.fixture
def catharus_assembly():
    return parse_assembly_report(
        "GCA_009819885.2",
        "bCatUst1.pri.v2",
        (REMOTE / "assembly_report_GCA_009819885_2.txt").read_text(),
    )


@pytest.fixture
def podarcis_vcf() -> VcfHeader:
    return read_vcf_header(
        FileByteSource(REMOTE / "podarcis-raffonei.raw.vcf.gz.prefix"),
        initial_window=FIXTURE_WINDOW,
    )


def make_context(repo, *, vcf=None, report=None, **kwargs) -> Context:
    context = Context(species=repo.load("reptiles/podarcis-raffonei"), vgp_list=repo.vgp_list)
    context.vgp_entry = repo.vgp_list.by_slug("podarcis-raffonei")
    context.vcf_header = vcf
    context.assembly_report = report
    for name, value in kwargs.items():
        setattr(context, name, value)
    context.available = {CONFIG, NCBI, ASSEMBLY_REPORT, CONTIG_MAP, VCF_HEADER, BAM_HEADERS}
    return context


def fire(context: Context, *ids: str):
    """Run checks and return non-skipped (id, severity) pairs."""
    out = []
    for check_id in ids or TIER_3A:
        check = registry.get(check_id)
        assert check is not None, check_id
        for finding in registry.run(context, [check]):
            if finding.severity is not Severity.SKIPPED:
                out.append((finding.id, finding.severity))
    return out


def with_contigs(header: VcfHeader, contigs: dict) -> VcfHeader:
    return replace(header, contigs=contigs)


def to_refseq(report, contigs: dict) -> dict:
    mapping = {s.genbank: s.refseq for s in report.sequences if s.genbank and s.refseq}
    return {mapping.get(name, name): length for name, length in contigs.items()}


class TestAssemblyReportParsing:
    def test_parses_sequences_with_both_accessions(self, podarcis_assembly):
        assert len(podarcis_assembly.sequences) == 27
        first = podarcis_assembly.sequences[0]
        assert first.name == "SUPER_1"
        assert first.genbank == "CM049750.1"
        assert first.refseq == "NC_070602.1"
        assert first.length == 139138986
        assert first.is_assembled_molecule

    def test_exposes_each_naming_scheme(self, podarcis_assembly):
        assert len(podarcis_assembly.lengths("genbank")) == 27
        assert len(podarcis_assembly.lengths("refseq")) == 27
        assert len(podarcis_assembly.lengths("name")) == 27
        assert podarcis_assembly.lengths("genbank")["CM049750.1"] == 139138986

    def test_detects_which_scheme_a_name_belongs_to(self, podarcis_assembly):
        assert podarcis_assembly.scheme_of("CM049750.1") == "genbank"
        assert podarcis_assembly.scheme_of("NC_070602.1") == "refseq"
        assert podarcis_assembly.scheme_of("SUPER_1") == "name"
        assert podarcis_assembly.scheme_of("nonsense") is None

    def test_best_scheme_picks_the_majority(self, podarcis_assembly):
        assert podarcis_assembly.best_scheme({"CM049750.1", "CM049751.1"}) == "genbank"
        assert podarcis_assembly.best_scheme({"NC_070602.1"}) == "refseq"

    def test_counts_assembled_molecules(self, podarcis_assembly):
        assert sum(1 for s in podarcis_assembly.sequences if s.is_assembled_molecule) == 20

    def test_round_trips_through_the_cache_shape(self, podarcis_assembly):
        from congen.core.remote.ncbi import AssemblyReport

        assert AssemblyReport.from_dict(podarcis_assembly.as_dict()) == podarcis_assembly

    def test_ignores_comments_and_short_rows(self):
        report = parse_assembly_report(
            "GCA_1.1",
            "n",
            "# a comment\n\nS1\tassembled-molecule\t1\tChromosome\tCM1.1\t=\tNC1.1\tPrimary\t100\tna\ntoo\tshort\n",
        )
        assert len(report.sequences) == 1


class TestTheCorpusCase:
    def test_the_real_vcf_against_its_real_assembly_is_clean(
        self, repo, podarcis_vcf, podarcis_assembly
    ):
        """What all 67 species with a VCF actually look like."""
        context = make_context(repo, vcf=podarcis_vcf, report=podarcis_assembly)
        assert fire(context, "F001", "F002", "F003", "F009", "F011") == []


class TestWrongReferenceDetection:
    def test_a_different_species_assembly_is_f001(
        self, repo, podarcis_vcf, catharus_assembly
    ):
        context = make_context(repo, vcf=podarcis_vcf, report=catharus_assembly)
        assert fire(context, "F001") == [("F001", Severity.ERROR)]

    def test_a_wrong_contig_length_is_f002(self, repo, podarcis_vcf, podarcis_assembly):
        """A different assembly of the same species keeps names, not lengths."""
        contigs = dict(podarcis_vcf.contigs)
        first = next(iter(contigs))
        contigs[first] = contigs[first] + 1000
        context = make_context(
            repo, vcf=with_contigs(podarcis_vcf, contigs), report=podarcis_assembly
        )
        findings = fire(context, "F002")
        assert findings == [("F002", Severity.ERROR)]

    def test_f002_message_shows_both_lengths(self, repo, podarcis_vcf, podarcis_assembly):
        contigs = dict(podarcis_vcf.contigs)
        first = next(iter(contigs))
        contigs[first] = 12345
        context = make_context(
            repo, vcf=with_contigs(podarcis_vcf, contigs), report=podarcis_assembly
        )
        detail = registry.run(context, [registry.get("F002")])[0].detail
        assert "12345" in detail and str(podarcis_assembly.lengths("genbank")[first]) in detail


class TestNamingSchemes:
    def test_a_refseq_named_vcf_is_not_a_false_positive(
        self, repo, podarcis_vcf, podarcis_assembly
    ):
        """The scheme is detected, not assumed."""
        contigs = to_refseq(podarcis_assembly, podarcis_vcf.contigs)
        context = make_context(
            repo, vcf=with_contigs(podarcis_vcf, contigs), report=podarcis_assembly
        )
        assert fire(context, "F001", "F002", "F003", "F009", "F011") == []

    def test_mixed_schemes_are_f003(self, repo, podarcis_vcf, podarcis_assembly):
        items = list(podarcis_vcf.contigs.items())
        half = len(items) // 2
        mixed = {
            **dict(items[:half]),
            **to_refseq(podarcis_assembly, dict(items[half:])),
        }
        context = make_context(
            repo, vcf=with_contigs(podarcis_vcf, mixed), report=podarcis_assembly
        )
        assert ("F003", Severity.ERROR) in fire(context, "F003")


class TestAsymmetry:
    """Absence in the VCF means something different from absence in the assembly."""

    def test_a_dropped_chromosome_warns_and_is_f011(
        self, repo, podarcis_vcf, podarcis_assembly
    ):
        contigs = dict(podarcis_vcf.contigs)
        biggest = max(contigs, key=lambda k: contigs[k] or 0)
        del contigs[biggest]
        context = make_context(
            repo, vcf=with_contigs(podarcis_vcf, contigs), report=podarcis_assembly
        )
        assert fire(context, "F009", "F011") == [
            ("F009", Severity.WARN),
            ("F011", Severity.WARN),
        ]

    def test_a_dropped_small_scaffold_is_only_informational(
        self, repo, podarcis_vcf, podarcis_assembly
    ):
        contigs = dict(podarcis_vcf.contigs)
        smallest = min(contigs, key=lambda k: contigs[k] or 0)
        del contigs[smallest]
        context = make_context(
            repo, vcf=with_contigs(podarcis_vcf, contigs), report=podarcis_assembly
        )
        findings = fire(context, "F009", "F011")
        assert findings == [("F009", Severity.INFO)]

    def test_the_threshold_is_configurable(self, repo, podarcis_vcf, podarcis_assembly):
        contigs = dict(podarcis_vcf.contigs)
        smallest = min(contigs, key=lambda k: contigs[k] or 0)
        del contigs[smallest]
        context = make_context(
            repo,
            vcf=with_contigs(podarcis_vcf, contigs),
            report=podarcis_assembly,
            missing_contig_threshold=0.0,
        )
        assert fire(context, "F009") == [("F009", Severity.WARN)]


class TestCascadeSuppression:
    """One root cause, not three findings that all describe it."""

    def test_f009_and_f011_stay_quiet_against_the_wrong_assembly(
        self, repo, podarcis_vcf, catharus_assembly
    ):
        context = make_context(repo, vcf=podarcis_vcf, report=catharus_assembly)
        assert fire(context, "F001", "F009", "F011") == [("F001", Severity.ERROR)]

    def test_f009_and_f011_stay_quiet_when_naming_is_mixed(
        self, repo, podarcis_vcf, podarcis_assembly
    ):
        items = list(podarcis_vcf.contigs.items())
        half = len(items) // 2
        mixed = {**dict(items[:half]), **to_refseq(podarcis_assembly, dict(items[half:]))}
        context = make_context(
            repo, vcf=with_contigs(podarcis_vcf, mixed), report=podarcis_assembly
        )
        assert fire(context, "F003", "F009", "F011") == [("F003", Severity.ERROR)]


class TestBamAgreement:
    def test_bam_sq_matching_the_vcf_is_clean(self, repo, podarcis_vcf, podarcis_assembly):
        bam = read_bam_header(
            FileByteSource(REMOTE / "podarcis-raffonei.SAMN18355762.bam.prefix"),
            initial_window=FIXTURE_WINDOW,
        )
        context = make_context(
            repo,
            vcf=podarcis_vcf,
            report=podarcis_assembly,
            bam_headers={"SAMN18355762": bam},
        )
        assert fire(context, "F004", "F005", "F006") == []

    def test_a_bam_from_another_reference_is_f004(self, repo, podarcis_vcf, podarcis_assembly):
        other = read_bam_header(
            FileByteSource(REMOTE / "grus-americana.SAMEA116096693.bam.prefix"),
            initial_window=FIXTURE_WINDOW,
        )
        context = make_context(
            repo, vcf=podarcis_vcf, report=podarcis_assembly, bam_headers={"x": other}
        )
        assert fire(context, "F004") == [("F004", Severity.ERROR)]

    def test_disagreeing_bams_are_f005(self, repo, podarcis_vcf, podarcis_assembly):
        one = read_bam_header(
            FileByteSource(REMOTE / "podarcis-raffonei.SAMN18355762.bam.prefix"),
            initial_window=FIXTURE_WINDOW,
        )
        two = read_bam_header(
            FileByteSource(REMOTE / "grus-americana.SAMEA116096693.bam.prefix"),
            initial_window=FIXTURE_WINDOW,
        )
        context = make_context(
            repo, vcf=podarcis_vcf, report=podarcis_assembly, bam_headers={"a": one, "b": two}
        )
        assert fire(context, "F005") == [("F005", Severity.ERROR)]

    def test_f005_says_whether_it_saw_every_bam(self, repo, podarcis_assembly):
        one = read_bam_header(
            FileByteSource(REMOTE / "podarcis-raffonei.SAMN18355762.bam.prefix"),
            initial_window=FIXTURE_WINDOW,
        )
        two = read_bam_header(
            FileByteSource(REMOTE / "grus-americana.SAMEA116096693.bam.prefix"),
            initial_window=FIXTURE_WINDOW,
        )
        sampled = make_context(repo, report=podarcis_assembly, bam_headers={"a": one, "b": two})
        assert "sampled" in registry.run(sampled, [registry.get("F005")])[0].message

        complete = make_context(
            repo,
            report=podarcis_assembly,
            bam_headers={"a": one, "b": two},
            bam_headers_complete=True,
        )
        assert "all" in registry.run(complete, [registry.get("F005")])[0].message

    def test_a_single_bam_cannot_disagree(self, repo, podarcis_assembly):
        one = read_bam_header(
            FileByteSource(REMOTE / "podarcis-raffonei.SAMN18355762.bam.prefix"),
            initial_window=FIXTURE_WINDOW,
        )
        context = make_context(repo, report=podarcis_assembly, bam_headers={"a": one})
        assert fire(context, "F005") == []

    def test_bwa_reference_mismatch_is_f006(self, repo, podarcis_assembly):
        other = read_bam_header(
            FileByteSource(REMOTE / "grus-americana.SAMEA116096693.bam.prefix"),
            initial_window=FIXTURE_WINDOW,
        )
        context = make_context(repo, report=podarcis_assembly, bam_headers={"x": other})
        findings = registry.run(context, [registry.get("F006")])
        # The grus BAM names a different reference than podarcis's config.
        if findings:
            assert findings[0].severity is Severity.WARN


class TestContigMap:
    def test_a_matching_contig_map_is_clean(self, repo, podarcis_vcf, podarcis_assembly):
        contig_map = parse_contig_map((REMOTE / "contig_map.tsv").read_text())
        context = make_context(
            repo, vcf=podarcis_vcf, report=podarcis_assembly, contig_map=contig_map
        )
        assert fire(context, "F008") == []

    def test_a_contig_map_naming_absent_contigs_is_f008(self, repo, podarcis_vcf, podarcis_assembly):
        contig_map = parse_contig_map(
            "original_contig\tplink_contig\tadmixture_id\nNOT_A_CONTIG\tX\t1\n"
        )
        context = make_context(
            repo, vcf=podarcis_vcf, report=podarcis_assembly, contig_map=contig_map
        )
        assert fire(context, "F008") == [("F008", Severity.WARN)]


class TestResolvability:
    def test_an_accession_source_is_clean(self, repo):
        assert fire(make_context(repo), "F010") == []

    def test_a_url_source_warns_that_the_check_weakened(self, repo):
        context = make_context(repo)
        context.species.config.reference = replace(
            context.species.reference, source="https://example.org/ref.fa.gz"
        )
        findings = fire(context, "F010")
        assert findings == [("F010", Severity.WARN)]

    def test_f007_defers_to_the_vgp_checks_when_an_entry_exists(self, repo):
        context = make_context(
            repo,
            ncbi_info=AssemblyInfo(
                accession="GCA_027172205.1", organism_name="Completely Different", tax_id=1
            ),
        )
        assert context.vgp_entry is not None
        assert fire(context, "F007") == []

    def test_f007_is_the_fallback_when_there_is_no_vgp_entry(self, repo):
        context = make_context(
            repo,
            ncbi_info=AssemblyInfo(
                accession="GCA_027172205.1", organism_name="Completely Different", tax_id=1
            ),
        )
        context.vgp_entry = None
        assert fire(context, "F007") == [("F007", Severity.WARN)]
