"""Shared test fixtures.

The suite runs entirely offline: metadata fixtures are real files copied
from congen-metadata, and header fixtures are the first 16 KiB of real
GenomeArk objects, which is enough to contain any header in the corpus.
"""

from __future__ import annotations

import zlib
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
METADATA_ROOT = FIXTURES / "metadata"
REMOTE = FIXTURES / "remote"

#: Fixture header prefixes are 16 KiB, so readers must be told not to ask
#: for more than exists.
FIXTURE_WINDOW = 16 * 1024


@pytest.fixture
def metadata_root() -> Path:
    return METADATA_ROOT


@pytest.fixture
def remote_fixtures() -> Path:
    return REMOTE


@pytest.fixture
def podarcis_vcf() -> Path:
    return REMOTE / "podarcis-raffonei.raw.vcf.gz.prefix"


@pytest.fixture
def podarcis_bam() -> Path:
    return REMOTE / "podarcis-raffonei.SAMN18355762.bam.prefix"


@pytest.fixture
def anser_vcf() -> Path:
    return REMOTE / "anser-albifrons.raw.vcf.gz.prefix"


@pytest.fixture
def grus_bam() -> Path:
    return REMOTE / "grus-americana.SAMEA116096693.bam.prefix"


def bgzf_pack(payload: bytes, block_size: int = 512) -> bytes:
    """Compress ``payload`` into a series of independent gzip members.

    BGZF's defining property for our purposes is that it is a
    concatenation of gzip members, so a byte prefix of the file yields a
    prefix of the content. This builds such a stream without needing a
    real bgzip.
    """
    out = bytearray()
    for start in range(0, max(len(payload), 1), block_size):
        chunk = payload[start : start + block_size]
        compressor = zlib.compressobj(6, zlib.DEFLATED, 16 + zlib.MAX_WBITS)
        out += compressor.compress(chunk) + compressor.flush()
    return bytes(out)


class RecordingSource:
    """A ByteSource that remembers every window it was asked for."""

    def __init__(self, data: bytes, label: str = "<recording>") -> None:
        self.data = data
        self.label = label
        self.requests: list[tuple[int, int]] = []

    def fetch(self, start: int, end: int) -> bytes:
        self.requests.append((start, end))
        return self.data[start : end + 1]
