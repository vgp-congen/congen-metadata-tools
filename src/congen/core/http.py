"""HTTP access with the retry behaviour GenomeArk actually requires.

A full-corpus sweep reset roughly 1.5% of connections (2 of ~134 header
reads), so retry with backoff is a correctness requirement rather than
defensiveness: without it, a clean corpus reports spurious failures.
"""

from __future__ import annotations

import http.client
import json
import os
import random
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

#: Requests per second allowed per host, where a host publishes a limit.
#: NCBI eutils refuses above 3/s anonymously (measured: 3 of 12 unpaced
#: requests rejected, 0 of 12 when paced beneath the cap) and 10/s with an
#: API key.
DEFAULT_HOST_RATE_LIMITS: dict[str, float] = {
    "eutils.ncbi.nlm.nih.gov": 3.0,
}

EUTILS_HOST = "eutils.ncbi.nlm.nih.gov"
ENV_NCBI_API_KEY = "NCBI_API_KEY"

USER_AGENT = (
    "congen-metadata-tools/0.1 "
    "(+https://github.com/vgp-congen/congen-metadata-tools)"
)

DEFAULT_ATTEMPTS = 5
DEFAULT_TIMEOUT = 90.0

#: Status codes worth retrying. Everything else is a real answer.
RETRY_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})

#: Transport-level failures worth retrying. ConnectionResetError is the one
#: GenomeArk actually produces.
RETRY_EXCEPTIONS = (
    ConnectionResetError,
    ConnectionAbortedError,
    socket.timeout,
    TimeoutError,
    http.client.IncompleteRead,
    http.client.BadStatusLine,
)


class _RateLimiter:
    """Spaces requests to a host at most ``per_second`` apart.

    Shared across threads, since a full-corpus run drives six workers at
    once and the cap is per client, not per connection.
    """

    def __init__(self, per_second: float) -> None:
        self.min_interval = 1.0 / per_second if per_second > 0 else 0.0
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def acquire(self, sleep=time.sleep) -> None:
        if not self.min_interval:
            return
        with self._lock:
            now = time.monotonic()
            wait = self._next_allowed - now
            self._next_allowed = max(now, self._next_allowed) + self.min_interval
        if wait > 0:
            sleep(wait)


_limiters: dict[str, _RateLimiter] = {}
_limiters_lock = threading.Lock()


def set_host_rate_limit(host: str, per_second: float) -> None:
    """Override a host's rate limit, e.g. once an API key is known."""
    with _limiters_lock:
        _limiters[host] = _RateLimiter(per_second)


def _limiter_for(url: str) -> _RateLimiter | None:
    host = urllib.parse.urlsplit(url).hostname or ""
    with _limiters_lock:
        limiter = _limiters.get(host)
        if limiter is None:
            per_second = DEFAULT_HOST_RATE_LIMITS.get(host)
            if per_second is None:
                return None
            limiter = _limiters[host] = _RateLimiter(per_second)
        return limiter


def ncbi_api_key() -> str | None:
    """``$NCBI_API_KEY``, which raises the eutils cap from 3/s to 10/s."""
    key = os.environ.get(ENV_NCBI_API_KEY, "").strip()
    if key:
        set_host_rate_limit(EUTILS_HOST, 10.0)
    return key or None


class HttpError(RuntimeError):
    """A non-retryable HTTP response."""

    def __init__(self, url: str, status: int, reason: str) -> None:
        super().__init__(f"HTTP {status} {reason}: {url}")
        self.url = url
        self.status = status
        self.reason = reason


@dataclass(frozen=True)
class Response:
    url: str
    status: int
    body: bytes
    headers: dict[str, str]

    def header(self, name: str, default: str | None = None) -> str | None:
        return self.headers.get(name.lower(), default)


def _should_retry(exc: BaseException) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in RETRY_STATUS
    if isinstance(exc, urllib.error.URLError):
        # Wraps socket-level errors, including connection resets.
        return not isinstance(exc, urllib.error.HTTPError)
    return isinstance(exc, RETRY_EXCEPTIONS)


def _retry_after(exc: BaseException) -> float | None:
    if isinstance(exc, urllib.error.HTTPError):
        value = exc.headers.get("Retry-After") if exc.headers else None
        if value and value.strip().isdigit():
            return float(value.strip())
    return None


def _backoff(attempt: int) -> float:
    """Exponential backoff with jitter, capped at 8s."""
    return min(2.0**attempt, 8.0) * (0.5 + random.random())


def request(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    data: bytes | None = None,
    max_bytes: int | None = None,
    attempts: int = DEFAULT_ATTEMPTS,
    timeout: float = DEFAULT_TIMEOUT,
    sleep=time.sleep,
) -> Response:
    """GET ``url`` with retry/backoff.

    ``max_bytes`` bounds how much of the body is read. This matters for
    ranged requests: a server that ignores ``Range`` answers 200 with the
    whole object, and reading that from a 1.5 GB VCF would be a disaster.
    """
    all_headers = {"User-Agent": USER_AGENT}
    if headers:
        all_headers.update(headers)

    limiter = _limiter_for(url)
    last: BaseException | None = None
    for attempt in range(attempts):
        if limiter is not None:
            limiter.acquire(sleep)
        req = urllib.request.Request(url, headers=all_headers, data=data)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read() if max_bytes is None else resp.read(max_bytes)
                return Response(
                    url=url,
                    status=resp.status,
                    body=body,
                    headers={k.lower(): v for k, v in resp.headers.items()},
                )
        except BaseException as exc:  # noqa: BLE001 - classified below
            if not _should_retry(exc):
                if isinstance(exc, urllib.error.HTTPError):
                    raise HttpError(url, exc.code, exc.reason or "") from exc
                raise
            last = exc
            if attempt == attempts - 1:
                break
            sleep(_retry_after(exc) or _backoff(attempt))

    assert last is not None
    raise last


def range_get(
    url: str,
    start: int,
    end: int,
    *,
    attempts: int = DEFAULT_ATTEMPTS,
    timeout: float = DEFAULT_TIMEOUT,
    sleep=time.sleep,
) -> bytes:
    """Fetch bytes ``start``..``end`` inclusive.

    A short read is not an error: ranges are routinely requested past
    end-of-object, and S3 answers with whatever exists.
    """
    if start < 0 or end < start:
        raise ValueError(f"invalid range {start}-{end}")
    want = end - start + 1
    resp = request(
        url,
        headers={"Range": f"bytes={start}-{end}"},
        max_bytes=want,
        attempts=attempts,
        timeout=timeout,
        sleep=sleep,
    )
    return resp.body


def get_bytes(url: str, **kwargs) -> bytes:
    return request(url, **kwargs).body


def get_text(url: str, encoding: str = "utf-8", **kwargs) -> str:
    return request(url, **kwargs).body.decode(encoding, "replace")


def get_json(url: str, **kwargs):
    return json.loads(request(url, **kwargs).body)


def post_text(url: str, fields: dict[str, str], encoding: str = "utf-8", **kwargs) -> str:
    """Form-encoded POST. eutils needs it once an ID list outgrows a URL."""
    body = urllib.parse.urlencode(fields).encode("ascii")
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    headers.update(kwargs.pop("headers", {}) or {})
    return request(url, data=body, headers=headers, **kwargs).body.decode(encoding, "replace")


def build_url(base: str, params: dict[str, str]) -> str:
    return f"{base}?{urllib.parse.urlencode(params)}"
