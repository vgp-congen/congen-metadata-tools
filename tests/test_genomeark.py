"""Tests for GenomeArk listing, from recorded XML."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from congen.core.cache import Cache
from congen.core.remote import genomeark as ga
from congen.core.remote.genomeark import GenomeArk, S3Object
from tests.conftest import REMOTE


@pytest.fixture
def offline(monkeypatch, tmp_path):
    """Serve recorded XML for each prefix; fail on anything unexpected."""
    base = f"{ga.VARIANT_CALLING_PREFIX}/GCA_027172205.1"
    pages = {
        f"{base}/": REMOTE / "s3_top.xml",
        f"{base}/vcfs/": REMOTE / "s3_vcfs.xml",
        f"{base}/bams/": REMOTE / "s3_bams.xml",
        f"{base}/qc/": REMOTE / "s3_qc.xml",
    }
    empty = (
        b'<?xml version="1.0"?><ListBucketResult '
        b'xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
        b"<IsTruncated>false</IsTruncated></ListBucketResult>"
    )
    requested: list[str] = []

    def fake_get_bytes(url: str, **kwargs) -> bytes:
        requested.append(url)
        query = parse_qs(urlsplit(url).query)
        prefix = query.get("prefix", [""])[0]
        if prefix in pages:
            return pages[prefix].read_bytes()
        return empty

    monkeypatch.setattr(ga.http, "get_bytes", fake_get_bytes)
    client = GenomeArk(Cache(directory=tmp_path))
    client.requested = requested  # type: ignore[attr-defined]
    return client


class TestObjectModel:
    def test_name_url_and_emptiness(self):
        obj = S3Object(key="a/b/c/raw.vcf.gz", size=10, etag="x", last_modified="t")
        assert obj.name == "raw.vcf.gz"
        assert obj.url == f"{ga.BASE_URL}/a/b/c/raw.vcf.gz"
        assert not obj.is_empty
        assert S3Object(key="k", size=0, etag="", last_modified="").is_empty


class TestListingParse:
    def test_top_level_splits_subdirs_from_objects(self, offline):
        listing = offline.list(f"{ga.VARIANT_CALLING_PREFIX}/GCA_027172205.1/")
        assert listing.subdirs == ("bams", "callable_sites", "qc", "vcfs")
        assert listing.names() == {"README.txt", "sample_sheet.csv"}

    def test_objects_carry_size_and_etag(self, offline):
        listing = offline.list(f"{ga.VARIANT_CALLING_PREFIX}/GCA_027172205.1/vcfs/")
        vcf = listing.by_name()["raw.vcf.gz"]
        assert vcf.size > 1_000_000
        assert vcf.etag
        assert vcf.last_modified

    def test_results_are_cached(self, offline):
        prefix = f"{ga.VARIANT_CALLING_PREFIX}/GCA_027172205.1/"
        offline.list(prefix)
        before = len(offline.requested)
        offline.list(prefix)
        assert len(offline.requested) == before

    def test_pagination_follows_continuation_tokens(self, monkeypatch, tmp_path):
        first = (REMOTE / "s3_bams_truncated.xml").read_bytes()
        second = (REMOTE / "s3_bams.xml").read_bytes()
        assert b"<IsTruncated>true" in first

        calls: list[str] = []

        def fake_get_bytes(url: str, **kwargs) -> bytes:
            calls.append(url)
            return first if len(calls) == 1 else second

        monkeypatch.setattr(ga.http, "get_bytes", fake_get_bytes)
        client = GenomeArk(Cache(directory=tmp_path))
        listing = client.list(f"{ga.VARIANT_CALLING_PREFIX}/GCA_027172205.1/bams/")
        assert len(calls) == 2
        assert "continuation-token" in calls[1]
        # Objects from both pages are merged.
        assert len(listing.objects) > 5


class TestInventory:
    def test_gathers_the_data_directories(self, offline):
        inventory = offline.inventory("GCA_027172205.1")
        assert inventory.exists
        assert inventory.subdirs == ("bams", "callable_sites", "qc", "vcfs")
        assert len(inventory.bam_objects) == 21
        assert len(inventory.bam_index_names) == 21
        assert inventory.vcf_names == {"raw.vcf.gz"}

    def test_never_descends_into_zarr_stores(self, offline):
        """callable_sites/ holds 3,264 objects for this one species."""
        offline.inventory("GCA_027172205.1")
        assert not any("callable_sites" in url for url in offline.requested)
        assert "callable_sites" in ga.OPAQUE_SUBDIRS

    def test_object_lookup_by_path(self, offline):
        inventory = offline.inventory("GCA_027172205.1")
        assert inventory.object("README.txt") is not None
        assert inventory.object("vcfs", "raw.vcf.gz") is not None
        assert inventory.object("qc", "individuals.samps.txt") is not None
        assert inventory.object("vcfs", "filtered.vcf.gz") is None
        assert inventory.object("qc", "not-a-file") is None

    def test_bam_sample_names_strip_the_extension(self, offline):
        inventory = offline.inventory("GCA_027172205.1")
        assert "SAMN18355762" in inventory.bam_objects
        assert all(not n.endswith(".bam") for n in inventory.bam_objects)

    def test_a_missing_accession_reports_absent(self, monkeypatch, tmp_path):
        empty = (
            b'<?xml version="1.0"?><ListBucketResult '
            b'xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
            b"<IsTruncated>false</IsTruncated></ListBucketResult>"
        )
        monkeypatch.setattr(ga.http, "get_bytes", lambda url, **kw: empty)
        inventory = GenomeArk(Cache(directory=tmp_path)).inventory("GCA_000000000.9")
        assert not inventory.exists
        assert inventory.bam_objects == {}

    def test_directory_markers_are_not_objects(self, monkeypatch, tmp_path):
        body = (
            b'<?xml version="1.0"?><ListBucketResult '
            b'xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
            b"<IsTruncated>false</IsTruncated>"
            b"<Contents><Key>a/b/</Key><Size>0</Size>"
            b"<ETag>&quot;x&quot;</ETag><LastModified>t</LastModified></Contents>"
            b"</ListBucketResult>"
        )
        monkeypatch.setattr(ga.http, "get_bytes", lambda url, **kw: body)
        listing = GenomeArk(Cache(directory=tmp_path)).list("a/b/")
        assert listing.objects == ()
