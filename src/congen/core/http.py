"""HTTP access with the retry behaviour GenomeArk actually requires.

A full-corpus sweep reset roughly 1.5% of connections (2 of ~134 header
reads), so retry with backoff is a correctness requirement rather than
defensiveness: without it, a clean corpus reports spurious failures.
"""

from __future__ import annotations

import http.client
import json
import random
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

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

    last: BaseException | None = None
    for attempt in range(attempts):
        req = urllib.request.Request(url, headers=all_headers)
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


def build_url(base: str, params: dict[str, str]) -> str:
    return f"{base}?{urllib.parse.urlencode(params)}"
