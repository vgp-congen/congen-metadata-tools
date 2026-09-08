"""Data models for the congen-metadata repository.

These are plain dataclasses rather than pydantic models on purpose. The
loaders must be *tolerant*: a malformed sample sheet has to come back with
its problems described so a tool can report them, not raise on the first
bad row. Validation-on-construction is the opposite of what is needed
here, so the schema checking lives in the loaders and the models stay
dumb containers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ACCESSION_RE = re.compile(r"^GC[AF]_\d{9}\.\d+$")
BIOSAMPLE_RE = re.compile(r"^SAM(N|EA|D)\d+$")
RUN_RE = re.compile(r"^[SED]RR\d+$")
#: SRA *experiment* accessions. The tooling resolves these, but one
#: experiment can expand to several runs, so they are less precise than a
#: run accession rather than invalid.
EXPERIMENT_RE = re.compile(r"^[SED]RX\d+$")
#: Either form.
SRA_ACCESSION_RE = re.compile(r"^[SED]R[RX]\d+$")

#: Input types snpArcher understands.
INPUT_TYPES = frozenset({"srr", "fastq", "bam"})


@dataclass(frozen=True)
class LoadIssue:
    """A problem found while loading a file.

    Deliberately tool-agnostic: ``code`` is a stable loader-level string,
    which a tool maps onto its own vocabulary (the validator maps these to
    check IDs). Loaders do not know about check IDs or severities.
    """

    code: str
    message: str
    path: Path | None = None
    line: int | None = None


@dataclass(frozen=True)
class SampleRow:
    sample_id: str
    input_type: str
    input: str
    library_id: str | None = None
    extra: dict[str, str] = field(default_factory=dict)
    line: int | None = None

    @property
    def is_run_accession(self) -> bool:
        return bool(RUN_RE.match(self.input))

    @property
    def is_experiment_accession(self) -> bool:
        return bool(EXPERIMENT_RE.match(self.input))

    @property
    def is_sra_accession(self) -> bool:
        return bool(SRA_ACCESSION_RE.match(self.input))

    @property
    def is_local_path(self) -> bool:
        return "/" in self.input or self.input.startswith("~")


@dataclass
class SampleSheet:
    path: Path
    columns: list[str]
    rows: list[SampleRow]
    issues: list[LoadIssue] = field(default_factory=list)
    #: Line terminator the file actually used, so writers can round-trip it.
    line_terminator: str = "\n"

    @property
    def sample_ids(self) -> list[str]:
        """Every row's sample_id, in file order, including repeats."""
        return [r.sample_id for r in self.rows]

    @property
    def unique_sample_ids(self) -> set[str]:
        """The biosamples in this sheet.

        This — not ``len(rows)`` — is what every sample comparison must use.
        42 of 79 sheets repeat a sample_id because snpArcher merges multiple
        sequencing runs per biosample, so row counts disagree with sample
        counts on more than half the corpus.
        """
        return {r.sample_id for r in self.rows}

    @property
    def runs_by_sample(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for row in self.rows:
            out.setdefault(row.sample_id, []).append(row.input)
        return out

    def line_of(self, sample_id: str) -> int | None:
        for row in self.rows:
            if row.sample_id == sample_id:
                return row.line
        return None


@dataclass(frozen=True)
class ReferenceSpec:
    """The ``reference:`` block of a snpArcher config."""

    name: str | None
    source: str | None

    @property
    def is_accession(self) -> bool:
        return bool(self.source and ACCESSION_RE.match(self.source))

    @property
    def accession(self) -> str | None:
        return self.source if self.is_accession else None

    @property
    def is_local_path(self) -> bool:
        return bool(self.source) and (
            self.source.startswith(("/", "./", "~")) or self.source.startswith("file:")
        )

    @property
    def is_url(self) -> bool:
        return bool(self.source) and self.source.startswith(("http://", "https://", "ftp://"))

    def counterpart(self) -> str | None:
        """The GCF form of a GCA accession, or vice versa.

        The same assembly is published under both namespaces; which one a
        config names is a separate question from whether it is the right
        assembly.
        """
        acc = self.accession
        if not acc:
            return None
        prefix = "GCF" if acc.startswith("GCA") else "GCA"
        return f"{prefix}_{acc[4:]}"

    def same_assembly_as(self, other: str | None) -> bool:
        """True if ``other`` names this assembly in either namespace."""
        if not other or not self.accession:
            return False
        return other in {self.accession, self.counterpart()}


@dataclass
class SpeciesConfig:
    path: Path
    #: Round-trip loaded mapping, so writers preserve comments and ordering.
    data: Any
    reference: ReferenceSpec
    issues: list[LoadIssue] = field(default_factory=list)

    def get(self, *keys: str, default: Any = None) -> Any:
        """Nested lookup that tolerates missing or non-mapping levels."""
        node: Any = self.data
        for key in keys:
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node

    @property
    def ploidy(self) -> Any:
        return self.get("variant_calling", "ploidy")

    @property
    def het_prior(self) -> Any:
        return self.get("variant_calling", "gatk", "het_prior")

    @property
    def caller(self) -> Any:
        return self.get("variant_calling", "tool")


@dataclass
class ReadmeInfo:
    path: Path
    text: str
    species: str | None = None
    accession: str | None = None
    bioprojects: list[str] = field(default_factory=list)
    issues: list[LoadIssue] = field(default_factory=list)


@dataclass
class SpeciesMetadata:
    """Everything the repo says about one species."""

    slug: str
    clade: str
    path: Path
    config: SpeciesConfig
    sheet: SampleSheet
    readme: ReadmeInfo | None = None

    @property
    def key(self) -> str:
        """``clade/slug``, the identifier used in reports and on the CLI."""
        return f"{self.clade}/{self.slug}"

    @property
    def reference(self) -> ReferenceSpec:
        return self.config.reference

    @property
    def issues(self) -> list[LoadIssue]:
        out = [*self.config.issues, *self.sheet.issues]
        if self.readme:
            out.extend(self.readme.issues)
        return out
