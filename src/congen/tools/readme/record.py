"""``dataset.json`` — the harvested facts a README is rendered from.

The rule that decides what belongs here: **exactly what cannot be
recomputed offline, and nothing else.** Sample IDs and sheet counts stay
out — the sample sheet already has them, the loaders already read them
tolerantly, and a second copy is only a way for the two to disagree.

Because this file is committed, rendering becomes a pure function of
local inputs, and "is `README.md` out of date?" is answered by
re-rendering and comparing. That is why the readme generator needs none
of the digest machinery `validation_record` carries: a validation report
is a *claim* about inputs that have since moved on, whereas a README is a
*rendering*, and a rendering's staleness is testable by re-rendering.

What this file does carry are the ETags and object list it harvested, so
the other question — has GenomeArk moved on? — stays answerable with a
few conditional requests instead of a re-harvest.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

#: Written into each species directory beside `validation.json`.
RECORD_JSON = "dataset.json"

#: Bumped only when a field changes meaning or disappears. Adding a field
#: is not a break: the renderer tolerates its absence, which is what lets
#: a later phase add the SRA mapping without invalidating what is already
#: committed.
SCHEMA_VERSION = 1

#: Fields excluded when deciding whether a re-harvest changed anything.
#: They describe the act of harvesting, not the dataset.
VOLATILE_FIELDS = frozenset({"harvested_at", "tool_version"})


@dataclass
class Assembly:
    """From the NCBI Datasets API, keyed by accession. Effectively static."""

    organism_name: str | None = None
    common_name: str | None = None
    tax_id: int | None = None
    assembly_name: str | None = None
    assembly_level: str | None = None
    paired_accession: str | None = None


@dataclass
class VcfProvenance:
    """Read from the VCF header over HTTP Range — what actually ran.

    Preferred over `config.yaml` wherever both speak: the config records
    an intent, the header records an execution.
    """

    samples: list[str] = field(default_factory=list)
    n_contigs: int | None = None
    #: The variant callers named in the header, from `VcfHeader.callers()`.
    #: Not the same as every tool in `tool_versions` — `bcftools` appears
    #: beside `gatk` in every corpus VCF, and it is not a caller.
    callers: list[str] = field(default_factory=list)
    tool_versions: dict[str, str] = field(default_factory=dict)
    ploidy: str | None = None
    het_prior: str | None = None


@dataclass
class Cohort:
    """`callable_sites/coverage_thresholds.tsv`.

    `min_coverage` / `max_coverage` bound **site** depth when building the
    callable-sites mask. They are not per-sample cutoffs; see the note in
    `core.remote.qc.CoverageThresholds`.
    """

    mean_coverage: float | None = None
    min_coverage: float | None = None
    max_coverage: float | None = None
    #: Cohort variant sites, from `individuals.imiss` `N_DATA`.
    n_sites: int | None = None


@dataclass
class DatasetRecord:
    subject: str
    schema: int = SCHEMA_VERSION
    harvested_at: str = ""
    tool_version: str = ""
    #: The accession the data was actually found under. Recorded because
    #: the render must fail loudly rather than describe one accession
    #: while linking another — `grus-americana` and `sturnus-vulgaris`
    #: both declare something other than where their data sits.
    accession: str | None = None
    declared_accession: str | None = None
    prefix: str | None = None
    published: bool = False
    #: Object path relative to the accession prefix -> {size, etag}.
    objects: dict[str, dict] = field(default_factory=dict)
    #: Prefixes a delimited listing returns without descending into, by
    #: parent directory. Every size derived from `objects` is therefore a
    #: lower bound, and the document has to say so: one species'
    #: `callable_loci.zarr/` alone runs to thousands of objects.
    opaque_subdirs: dict[str, list[str]] = field(default_factory=dict)
    assembly: dict = field(default_factory=dict)
    vcf: dict = field(default_factory=dict)
    cohort: dict = field(default_factory=dict)
    #: Sample -> metric -> value, merged from the four per-sample QC
    #: tables. Per-sample values are kept even though the document only
    #: summarises them, so a change of mind about what to show never
    #: requires a re-harvest. That is the whole point of the split.
    samples: dict[str, dict] = field(default_factory=dict)
    #: Non-fatal problems met while harvesting, so a thin record explains
    #: itself rather than looking like a clean one.
    notes: list[str] = field(default_factory=list)

    @property
    def accession_matches(self) -> bool:
        return not (
            self.accession
            and self.declared_accession
            and self.accession != self.declared_accession
        )

    def size_of(self, *prefixes: str) -> int:
        """Total bytes of listed objects under any of `prefixes`.

        A lower bound wherever `opaque_subdirs` names something below it.
        """
        return sum(
            meta.get("size", 0)
            for path, meta in self.objects.items()
            if not prefixes or any(path.startswith(p) for p in prefixes)
        )

    def count_of(self, *prefixes: str) -> int:
        return sum(
            1
            for path in self.objects
            if not prefixes or any(path.startswith(p) for p in prefixes)
        )

    def metric(self, name: str) -> dict[str, float]:
        """Sample -> value for one QC metric, omitting samples lacking it."""
        return {
            sample: values[name]
            for sample, values in self.samples.items()
            if isinstance(values.get(name), (int, float))
        }

    def as_dict(self) -> dict:
        return asdict(self)

    def substance(self) -> dict:
        """Everything except when it was harvested and by what.

        What a refresh compares. Without this, `harvested_at` alone makes
        every record differ on every run, so a harvest that finds nothing
        new still produces a 79-file diff and buries the one species that
        actually moved.
        """
        return {k: v for k, v in self.as_dict().items() if k not in VOLATILE_FIELDS}

    @classmethod
    def from_dict(cls, payload: dict) -> DatasetRecord:
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in payload.items() if k in known})

    def render_json(self) -> str:
        return json.dumps(self.as_dict(), indent=2, sort_keys=True) + "\n"

    def write_json(self, path: Path) -> bool:
        """Write only if the substance changed, and say whether it did.

        When it has not, the previous `harvested_at` and `tool_version`
        are kept. That makes them mean *when this content was first
        observed* rather than *when we last looked*, which is both the
        more useful reading and the one that keeps a diff honest.
        """
        from congen.core.metadata.writers import write_if_changed

        previous = load_record(path)
        if previous is not None and previous.substance() == self.substance():
            self.harvested_at = previous.harvested_at
            self.tool_version = previous.tool_version
        return write_if_changed(path, self.render_json())


def load_record(path: Path) -> DatasetRecord | None:
    try:
        return DatasetRecord.from_dict(json.loads(path.read_text("utf-8")))
    except (FileNotFoundError, OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None
