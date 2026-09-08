"""Canary tests against live GenomeArk, deselected by default.

Run with ``pytest -m network``. These guard against the recorded fixtures
silently drifting from reality: if GenomeArk republishes an object, the
offline suite would keep passing against stale bytes.
"""

from __future__ import annotations

import pytest

from congen.core.remote.headers import FileByteSource, read_bam_header, read_vcf_header

pytestmark = pytest.mark.network

BASE = (
    "https://genomeark.s3.amazonaws.com/downstream_analyses/"
    "conservation_genomics/variant_calling"
)


def test_podarcis_vcf_fixture_matches_live(podarcis_vcf):
    live = read_vcf_header(f"{BASE}/GCA_027172205.1/vcfs/raw.vcf.gz")
    recorded = read_vcf_header(FileByteSource(podarcis_vcf), initial_window=16 * 1024)
    assert live.contigs == recorded.contigs
    assert live.samples == recorded.samples


def test_podarcis_bam_fixture_matches_live(podarcis_bam):
    live = read_bam_header(f"{BASE}/GCA_027172205.1/bams/SAMN18355762.bam")
    recorded = read_bam_header(FileByteSource(podarcis_bam), initial_window=16 * 1024)
    assert live.references == recorded.references
    assert live.text == recorded.text


def test_anser_vcf_fixture_matches_live(anser_vcf):
    live = read_vcf_header(f"{BASE}/GCA_976913865.1/vcfs/raw.vcf.gz")
    recorded = read_vcf_header(FileByteSource(anser_vcf), initial_window=16 * 1024)
    assert live.samples == recorded.samples


def test_default_windows_suffice_for_the_largest_header():
    """A 1337-contig header must not need the growth path in practice."""
    header = read_vcf_header(f"{BASE}/GCA_976913865.1/vcfs/raw.vcf.gz")
    assert len(header.contigs) == 1337


def test_assembly_report_fixtures_match_live():
    """The constructed FTP path still resolves, and the table is unchanged."""
    from congen.core.remote.ncbi import Ncbi, parse_assembly_report
    from tests.conftest import REMOTE

    live = Ncbi().assembly_report("GCA_027172205.1")
    recorded = parse_assembly_report(
        "GCA_027172205.1",
        "rPodRaf1.pri",
        (REMOTE / "assembly_report_GCA_027172205_1.txt").read_text(),
    )
    assert live is not None
    assert live.sequences == recorded.sequences


def test_the_two_odd_assembly_names_still_resolve():
    """Both need NCBI's sanitizing rule to build a working path."""
    from congen.core.remote.ncbi import Ncbi

    ncbi = Ncbi()
    for accession in ("GCA_028564815.2", "GCA_046562875.2"):
        report = ncbi.assembly_report(accession)
        assert report is not None, accession
        assert report.sequences, accession
