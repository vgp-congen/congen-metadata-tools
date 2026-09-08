"""Tests for the disk cache."""

from __future__ import annotations

from congen.core.cache import ENV_CACHE_DIR, Cache, default_cache_dir


class TestDefaultLocation:
    def test_env_override(self, monkeypatch, tmp_path):
        monkeypatch.setenv(ENV_CACHE_DIR, str(tmp_path / "custom"))
        assert default_cache_dir() == tmp_path / "custom"

    def test_respects_xdg_cache_home(self, monkeypatch, tmp_path):
        monkeypatch.delenv(ENV_CACHE_DIR, raising=False)
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
        assert default_cache_dir() == tmp_path / "congen"


class TestRoundTrip:
    def test_miss_then_hit(self, tmp_path):
        cache = Cache(directory=tmp_path)
        assert cache.get("ns", "key") is None
        cache.set("ns", "key", {"a": 1})
        assert cache.get("ns", "key") == {"a": 1}

    def test_namespaces_are_separate(self, tmp_path):
        cache = Cache(directory=tmp_path)
        cache.set("one", "key", 1)
        assert cache.get("two", "key") is None

    def test_ttl_expiry(self, tmp_path):
        cache = Cache(directory=tmp_path)
        cache.set("ns", "key", "value")
        assert cache.get("ns", "key", ttl=3600) == "value"
        assert cache.get("ns", "key", ttl=-1) is None

    def test_etag_keys_invalidate_naturally(self, tmp_path):
        """Keying on ETag means a re-upload misses without a TTL guess."""
        cache = Cache(directory=tmp_path)
        cache.set("headers", "s3://x/raw.vcf.gz#etag-aaa", {"contigs": 27})
        assert cache.get("headers", "s3://x/raw.vcf.gz#etag-bbb") is None

    def test_corrupt_entry_is_a_miss_not_a_crash(self, tmp_path):
        cache = Cache(directory=tmp_path)
        cache.set("ns", "key", "value")
        path = cache._path("ns", "key")
        path.write_text("{ this is not json")
        assert cache.get("ns", "key") is None

    def test_sharded_layout(self, tmp_path):
        cache = Cache(directory=tmp_path)
        cache.set("ns", "key", 1)
        shard = cache._path("ns", "key").parent
        assert shard.parent.name == "ns"
        assert len(shard.name) == 2


class TestMemoize:
    def test_calls_the_producer_once(self, tmp_path):
        cache = Cache(directory=tmp_path)
        calls = []

        def produce():
            calls.append(1)
            return "value"

        assert cache.memoize("ns", "key", produce) == "value"
        assert cache.memoize("ns", "key", produce) == "value"
        assert len(calls) == 1


class TestDisabled:
    def test_never_reads_or_writes(self, tmp_path):
        cache = Cache(directory=tmp_path, enabled=False)
        cache.set("ns", "key", "value")
        assert cache.get("ns", "key") is None
        assert not any(tmp_path.rglob("*.json"))

    def test_memoize_still_produces(self, tmp_path):
        cache = Cache(directory=tmp_path, enabled=False)
        calls = []
        assert cache.memoize("ns", "k", lambda: calls.append(1) or "v") == "v"
        assert cache.memoize("ns", "k", lambda: calls.append(1) or "v") == "v"
        assert len(calls) == 2


class TestClear:
    def test_clears_a_namespace_or_everything(self, tmp_path):
        cache = Cache(directory=tmp_path)
        cache.set("a", "1", 1)
        cache.set("b", "1", 1)
        assert cache.clear("a") == 1
        assert cache.get("b", "1") == 1
        assert cache.clear() == 1

    def test_clearing_nothing_is_fine(self, tmp_path):
        assert Cache(directory=tmp_path).clear("absent") == 0
