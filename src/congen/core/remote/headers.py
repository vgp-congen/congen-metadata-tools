"""Read VCF and BAM headers over HTTP without htslib.

**Do not replace this with pysam.** htslib downloads a remote index into
the current working directory, named by basename. Every species' VCF on
GenomeArk is called ``raw.vcf.gz``, so the cached ``raw.vcf.gz.tbi`` left
behind by one species is silently reused for the next — and htslib merges
the stale index's sequence names into the header it reports. Observed
live: a *Catharus ustulatus* VCF reporting 183 contigs, 22 of them
*Podarcis raffonei*, appended after the file's own 161. The raw header
bytes contain only 161. It fails silently and looks plausible.

Reading the header from a ranged GET avoids the index entirely, writes
nothing to the working directory, is roughly three times faster, and is
what lets the core stay pure Python.
"""

from __future__ import annotations

import re
import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from congen.core import http

#: Enough for every VCF header in the corpus (the largest needs < 8 KiB
#: compressed), with room to spare before the first growth step.
VCF_INITIAL_WINDOW = 256 * 1024
VCF_MAX_WINDOW = 64 * 1024 * 1024

BAM_INITIAL_WINDOW = 128 * 1024
BAM_MAX_WINDOW = 32 * 1024 * 1024

BAM_MAGIC = b"BAM\x01"

_CONTIG_RE = re.compile(r"^##contig=<(?P<body>.*)>\s*$")
_ID_RE = re.compile(r"\bID=(?P<id>[^,>]+)")
_LENGTH_RE = re.compile(r"\blength=(?P<length>\d+)")
_BWA_REF_RE = re.compile(r"(?:^|\s)(?P<path>\S+?)\.fa(?:\.gz)?(?=\s|$)")


class HeaderError(ValueError):
    """The bytes fetched were not a parseable header."""


class ByteSource(Protocol):
    """Something that can serve a byte range."""

    label: str

    def fetch(self, start: int, end: int) -> bytes:
        """Return bytes ``start``..``end`` inclusive, possibly fewer."""


@dataclass
class HttpByteSource:
    url: str

    @property
    def label(self) -> str:
        return self.url

    def fetch(self, start: int, end: int) -> bytes:
        return http.range_get(self.url, start, end)


@dataclass
class BytesByteSource:
    """An in-memory source. Fixtures and tests use this."""

    data: bytes
    label: str = "<bytes>"

    def fetch(self, start: int, end: int) -> bytes:
        return self.data[start : end + 1]


@dataclass
class FileByteSource:
    path: Path

    @property
    def label(self) -> str:
        return str(self.path)

    def fetch(self, start: int, end: int) -> bytes:
        with open(self.path, "rb") as handle:
            handle.seek(start)
            return handle.read(end - start + 1)


def as_source(target: str | Path | ByteSource) -> ByteSource:
    if isinstance(target, str):
        return HttpByteSource(target)
    if isinstance(target, Path):
        return FileByteSource(target)
    return target


def bgzf_inflate(buf: bytes) -> bytes:
    """Inflate consecutive gzip/BGZF members, stopping at truncation.

    BGZF is a sequence of independent gzip members, so a prefix of the
    file decompresses to a prefix of the content as long as the final,
    partial member is discarded — which is what makes ranged header
    reads work at all.
    """
    out = bytearray()
    position = 0
    while position < len(buf):
        decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
        try:
            out += decompressor.decompress(buf[position:])
        except zlib.error:
            break
        if not decompressor.eof:
            break  # member truncated by the window; discard the remainder
        remaining = len(decompressor.unused_data)
        if remaining == 0:
            break
        position = len(buf) - remaining
    return bytes(out)


@dataclass
class VcfHeader:
    lines: list[str]
    contigs: dict[str, int | None] = field(default_factory=dict)
    samples: list[str] = field(default_factory=list)
    source: str = ""

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    def meta(self, key: str) -> list[str]:
        """Values of every ``##key=...`` line, in file order."""
        prefix = f"##{key}="
        return [line[len(prefix) :] for line in self.lines if line.startswith(prefix)]

    @property
    def gatk_command_lines(self) -> list[str]:
        return self.meta("GATKCommandLine")

    @property
    def meta_keys(self) -> list[str]:
        """Every ``##key`` present, in file order, without duplicates."""
        out: list[str] = []
        for line in self.lines:
            if not line.startswith("##") or "=" not in line:
                continue
            key = line[2:].split("=", 1)[0]
            if key not in out:
                out.append(key)
        return out

    def gatk_tool_ids(self) -> list[str]:
        """The ``ID=`` of each recorded GATK invocation, in file order."""
        out: list[str] = []
        for command in self.gatk_command_lines:
            match = re.match(r"<ID=([^,>]+)", command)
            if match and match.group(1) not in out:
                out.append(match.group(1))
        return out

    def tool_versions(self) -> dict[str, str]:
        """Pipeline component versions the header records.

        GATK stamps ``Version=`` on each command line; bcftools writes a
        ``##bcftools_<op>Version`` line per operation.
        """
        versions: dict[str, str] = {}
        for command in self.gatk_command_lines:
            match = re.search(r'Version="?([0-9][^",\s]*)', command)
            if match:
                versions.setdefault("gatk", match.group(1))
        for line in self.lines:
            match = re.match(r"##bcftools_\w+Version=(\S+)", line)
            if match:
                versions.setdefault("bcftools", match.group(1).split("+")[0])
        return versions

    def callers(self) -> set[str]:
        """Variant callers the header gives positive evidence for.

        Empty when nothing recognizable is recorded, which is a reason to
        say nothing rather than to guess.
        """
        found: set[str] = set()
        for line in self.lines:
            for caller, pattern in CALLER_EVIDENCE:
                if re.match(pattern, line):
                    found.add(caller)
        return found

    def gatk_argument(self, name: str) -> str | None:
        """First value of ``--name`` across the recorded GATK invocations.

        The header preserves the arguments the run actually used, which is
        how a config can be checked against what really happened.
        """
        pattern = re.compile(rf"--{re.escape(name)}\s+(?P<value>[^\s\"]+)")
        for command in self.gatk_command_lines:
            match = pattern.search(command)
            if match:
                return match.group("value")
        return None


def parse_vcf_header_lines(lines: list[str], source: str = "") -> VcfHeader:
    """Parse header lines. Split out so tests need no compression."""
    contigs: dict[str, int | None] = {}
    samples: list[str] = []
    for line in lines:
        contig = _CONTIG_RE.match(line)
        if contig:
            body = contig.group("body")
            identifier = _ID_RE.search(body)
            if identifier:
                length = _LENGTH_RE.search(body)
                contigs[identifier.group("id")] = int(length.group("length")) if length else None
            continue
        if line.startswith("#CHROM"):
            samples = line.rstrip("\r").split("\t")[9:]
    return VcfHeader(lines=lines, contigs=contigs, samples=samples, source=source)


def read_vcf_header(
    target: str | Path | ByteSource,
    *,
    initial_window: int = VCF_INITIAL_WINDOW,
    max_window: int = VCF_MAX_WINDOW,
) -> VcfHeader:
    """Read a bgzipped VCF's header, growing the window until complete."""
    source = as_source(target)
    window = initial_window
    while True:
        raw = source.fetch(0, window - 1)
        text = bgzf_inflate(raw).decode("utf-8", "replace")
        if "#CHROM" in text:
            lines: list[str] = []
            for line in text.split("\n"):
                lines.append(line)
                if line.startswith("#CHROM"):
                    break
            return parse_vcf_header_lines(lines, source=source.label)
        if len(raw) < window:
            raise HeaderError(
                f"reached end of object without a #CHROM line: {source.label}"
            )
        if window >= max_window:
            raise HeaderError(
                f"VCF header not complete within {max_window} bytes: {source.label}"
            )
        window = min(window * 4, max_window)


#: Header evidence that positively identifies a *variant caller*.
#:
#: The bcftools entry is deliberately restricted to ``bcftools_call``.
#: snpArcher concatenates its per-interval VCFs with ``bcftools concat``,
#: so every GATK-called VCF in the corpus also carries
#: ``##bcftools_concatCommand`` — reading that as the caller would
#: mislabel all 67 of them.
CALLER_EVIDENCE: tuple[tuple[str, str], ...] = (
    ("gatk", r"^##GATKCommandLine=<ID=(?:HaplotypeCaller|GenotypeGVCFs|Mutect2)\b"),
    ("gatk", r"^##source=(?:HaplotypeCaller|GenotypeGVCFs|Mutect2)\b"),
    ("bcftools", r"^##bcftools_callCommand="),
    ("bcftools", r"^##source=bcftools_call\b"),
    ("deepvariant", r"^##DeepVariant"),
    ("deepvariant", r"^##source=DeepVariant\b"),
    ("sentieon", r"^##source=(?:Sentieon|DNAscope|DNAseq)\b"),
    ("freebayes", r"^##source=freeBayes\b"),
)


@dataclass
class BamHeader:
    text: str
    references: list[tuple[str, int]] = field(default_factory=list)
    source: str = ""

    @property
    def reference_lengths(self) -> dict[str, int]:
        return dict(self.references)

    def records(self, tag: str) -> list[dict[str, str]]:
        """Parse ``@TAG`` lines into field dictionaries."""
        out: list[dict[str, str]] = []
        for line in self.text.split("\n"):
            if not line.startswith(f"@{tag}\t"):
                continue
            fields: dict[str, str] = {}
            for chunk in line.split("\t")[1:]:
                key, _, value = chunk.partition(":")
                if key:
                    fields[key] = value
            out.append(fields)
        return out

    @property
    def read_groups(self) -> list[dict[str, str]]:
        return self.records("RG")

    @property
    def programs(self) -> list[dict[str, str]]:
        return self.records("PG")

    @property
    def sample_names(self) -> set[str]:
        return {rg["SM"] for rg in self.read_groups if rg.get("SM")}

    @property
    def library_names(self) -> set[str]:
        return {rg["LB"] for rg in self.read_groups if rg.get("LB")}

    @property
    def bwa_reference_name(self) -> str | None:
        """Reference basename from the ``@PG`` bwa command line.

        snpArcher stages the reference as
        ``results/reference/<reference.name>.fa.gz``, so this recovers the
        name the run was configured with.
        """
        for program in self.programs:
            if program.get("PN") != "bwa" and program.get("ID") != "bwa":
                continue
            command = program.get("CL", "")
            match = _BWA_REF_RE.search(command)
            if match:
                return match.group("path").rsplit("/", 1)[-1]
        return None


def parse_bam_header_bytes(raw: bytes, source: str = "") -> BamHeader:
    """Parse a decompressed BAM header block.

    Raises :class:`HeaderError` if ``raw`` is truncated mid-header, which
    is the signal for the caller to refetch with a larger window.
    """
    if raw[:4] != BAM_MAGIC:
        raise HeaderError(f"not a BAM (bad magic): {source}")
    if len(raw) < 8:
        raise HeaderError(f"truncated before header length: {source}")
    (text_length,) = struct.unpack_from("<i", raw, 4)
    if text_length < 0:
        raise HeaderError(f"negative header text length: {source}")
    if len(raw) < 8 + text_length + 4:
        raise HeaderError(f"truncated within header text: {source}")

    text = raw[8 : 8 + text_length].decode("utf-8", "replace")
    offset = 8 + text_length
    (n_ref,) = struct.unpack_from("<i", raw, offset)
    offset += 4

    references: list[tuple[str, int]] = []
    for _ in range(n_ref):
        if offset + 4 > len(raw):
            raise HeaderError(f"truncated within reference list: {source}")
        (name_length,) = struct.unpack_from("<i", raw, offset)
        offset += 4
        if name_length <= 0 or offset + name_length + 4 > len(raw):
            raise HeaderError(f"truncated within reference list: {source}")
        name = raw[offset : offset + name_length - 1].decode("utf-8", "replace")
        (length,) = struct.unpack_from("<i", raw, offset + name_length)
        offset += name_length + 4
        references.append((name, length))

    return BamHeader(text=text, references=references, source=source)


def read_bam_header(
    target: str | Path | ByteSource,
    *,
    initial_window: int = BAM_INITIAL_WINDOW,
    max_window: int = BAM_MAX_WINDOW,
) -> BamHeader:
    """Read a BAM's header, growing the window until complete."""
    source = as_source(target)
    window = initial_window
    while True:
        raw = source.fetch(0, window - 1)
        inflated = bgzf_inflate(raw)
        try:
            return parse_bam_header_bytes(inflated, source=source.label)
        except HeaderError:
            if len(raw) < window:
                raise
            if window >= max_window:
                raise HeaderError(
                    f"BAM header not complete within {max_window} bytes: {source.label}"
                ) from None
            window = min(window * 4, max_window)
