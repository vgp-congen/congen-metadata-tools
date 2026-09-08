"""Tests for the HTTP layer. No network: retry policy is what matters."""

from __future__ import annotations

import io
import urllib.error

import pytest

from congen.core import http


def http_error(code: int, headers: dict[str, str] | None = None) -> urllib.error.HTTPError:
    import email.message

    message = email.message.Message()
    for key, value in (headers or {}).items():
        message[key] = value
    return urllib.error.HTTPError("http://x", code, "reason", message, None)


class TestRetryClassification:
    @pytest.mark.parametrize("code", [408, 425, 429, 500, 502, 503, 504])
    def test_transient_statuses_retry(self, code):
        assert http._should_retry(http_error(code))

    @pytest.mark.parametrize("code", [400, 401, 403, 404, 416])
    def test_client_errors_do_not_retry(self, code):
        assert not http._should_retry(http_error(code))

    def test_connection_reset_retries(self):
        """The failure GenomeArk actually produces, ~1.5% of requests."""
        assert http._should_retry(ConnectionResetError("reset by peer"))

    def test_url_error_retries(self):
        assert http._should_retry(urllib.error.URLError("temporary failure"))

    def test_unrelated_exceptions_do_not_retry(self):
        assert not http._should_retry(ValueError("nope"))


class TestRetryAfter:
    def test_honours_retry_after_seconds(self):
        assert http._retry_after(http_error(429, {"Retry-After": "7"})) == 7.0

    def test_ignores_a_non_numeric_retry_after(self):
        assert http._retry_after(http_error(429, {"Retry-After": "Wed, 01 Jan 2025"})) is None

    def test_none_without_the_header(self):
        assert http._retry_after(http_error(503)) is None


class TestBackoff:
    def test_grows_and_is_capped(self):
        assert http._backoff(0) <= 2.0
        assert http._backoff(20) <= 8.0 * 1.5

    def test_is_jittered(self):
        values = {http._backoff(3) for _ in range(20)}
        assert len(values) > 1


class FakeResponse(io.BytesIO):
    def __init__(self, body: bytes, status: int = 206, headers: dict | None = None):
        super().__init__(body)
        self.status = status
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class TestRequestBehaviour:
    def test_retries_then_succeeds(self, monkeypatch):
        attempts = []

        def urlopen(req, timeout=None):
            attempts.append(req)
            if len(attempts) < 3:
                raise ConnectionResetError("reset")
            return FakeResponse(b"payload")

        monkeypatch.setattr(http.urllib.request, "urlopen", urlopen)
        response = http.request("http://x", sleep=lambda _s: None)
        assert response.body == b"payload"
        assert len(attempts) == 3

    def test_gives_up_after_the_attempt_budget(self, monkeypatch):
        calls = []

        def urlopen(req, timeout=None):
            calls.append(1)
            raise ConnectionResetError("reset")

        monkeypatch.setattr(http.urllib.request, "urlopen", urlopen)
        with pytest.raises(ConnectionResetError):
            http.request("http://x", attempts=4, sleep=lambda _s: None)
        assert len(calls) == 4

    def test_non_retryable_status_raises_immediately(self, monkeypatch):
        calls = []

        def urlopen(req, timeout=None):
            calls.append(1)
            raise http_error(404)

        monkeypatch.setattr(http.urllib.request, "urlopen", urlopen)
        with pytest.raises(http.HttpError) as caught:
            http.request("http://x", sleep=lambda _s: None)
        assert caught.value.status == 404
        assert len(calls) == 1

    def test_sends_a_user_agent(self, monkeypatch):
        seen = {}

        def urlopen(req, timeout=None):
            seen.update(req.headers)
            return FakeResponse(b"")

        monkeypatch.setattr(http.urllib.request, "urlopen", urlopen)
        http.request("http://x")
        assert "congen-metadata-tools" in seen.get("User-agent", "")

    def test_range_get_bounds_the_read(self, monkeypatch):
        """A server that ignores Range must not stream a 1.5 GB VCF at us."""

        def urlopen(req, timeout=None):
            assert req.headers["Range"] == "bytes=0-9"
            return FakeResponse(b"x" * 1_000_000, status=200)

        monkeypatch.setattr(http.urllib.request, "urlopen", urlopen)
        assert http.range_get("http://x", 0, 9) == b"x" * 10

    def test_short_read_is_not_an_error(self, monkeypatch):
        """Ranges past end-of-object are normal; S3 returns what exists."""

        def urlopen(req, timeout=None):
            return FakeResponse(b"abc")

        monkeypatch.setattr(http.urllib.request, "urlopen", urlopen)
        assert http.range_get("http://x", 0, 999) == b"abc"

    @pytest.mark.parametrize("start,end", [(-1, 5), (10, 9)])
    def test_invalid_ranges_are_rejected(self, start, end):
        with pytest.raises(ValueError, match="invalid range"):
            http.range_get("http://x", start, end)


class TestHostRateLimiting:
    """NCBI eutils refuses above 3/s anonymously; measured 3 of 12 unpaced."""

    def test_eutils_is_paced_and_genomeark_is_not(self):
        assert http._limiter_for("https://eutils.ncbi.nlm.nih.gov/entrez/x") is not None
        assert http._limiter_for("https://genomeark.s3.amazonaws.com/x") is None
        assert http._limiter_for("https://ftp.ncbi.nlm.nih.gov/genomes/x") is None

    def test_successive_acquires_space_out(self):
        limiter = http._RateLimiter(per_second=3.0)
        waits: list[float] = []
        for _ in range(4):
            limiter.acquire(sleep=waits.append)
        assert waits[0] == pytest.approx(1 / 3, abs=0.05)
        assert waits[-1] == pytest.approx(1.0, abs=0.05)

    def test_a_zero_limit_never_waits(self):
        limiter = http._RateLimiter(per_second=0)
        waits: list[float] = []
        limiter.acquire(sleep=waits.append)
        assert waits == []

    def test_an_api_key_raises_the_cap(self, monkeypatch):
        monkeypatch.setenv("NCBI_API_KEY", "k")
        assert http.ncbi_api_key() == "k"
        assert http._limiter_for("https://eutils.ncbi.nlm.nih.gov/x").min_interval == 0.1

    def test_no_key_returns_none(self, monkeypatch):
        monkeypatch.delenv("NCBI_API_KEY", raising=False)
        assert http.ncbi_api_key() is None

    def test_a_blank_key_is_treated_as_absent(self, monkeypatch):
        monkeypatch.setenv("NCBI_API_KEY", "   ")
        assert http.ncbi_api_key() is None

    def test_request_paces_before_each_attempt(self, monkeypatch):
        http.set_host_rate_limit("paced.example", 2.0)
        waits: list[float] = []

        def urlopen(req, timeout=None):
            return FakeResponse(b"ok")

        monkeypatch.setattr(http.urllib.request, "urlopen", urlopen)
        http.request("https://paced.example/a", sleep=waits.append)
        http.request("https://paced.example/b", sleep=waits.append)
        assert any(w > 0 for w in waits)


class TestPost:
    def test_form_encodes_and_sends_a_body(self, monkeypatch):
        seen = {}

        def urlopen(req, timeout=None):
            seen["data"] = req.data
            seen["type"] = req.headers.get("Content-type")
            return FakeResponse(b"result")

        monkeypatch.setattr(http.urllib.request, "urlopen", urlopen)
        out = http.post_text("http://x", {"db": "sra", "id": "1,2,3"})
        assert out == "result"
        assert b"db=sra" in seen["data"]
        assert b"id=1%2C2%2C3" in seen["data"]
        assert seen["type"] == "application/x-www-form-urlencoded"
