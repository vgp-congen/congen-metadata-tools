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
