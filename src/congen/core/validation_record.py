"""The record a validation leaves behind, and when it goes stale.

A validation is a claim about a specific pair of inputs — this metadata,
against that published data. The claim holds until one of them changes,
so a report records digests of what it saw and staleness is a pure
function of the current files.

**Digests rather than timestamps, because git does not preserve mtimes.**
A fresh clone stamps every file with checkout time, so "config.yaml is
newer than the report" is unreliable in exactly the places that matter.
"""

from __future__ import annotations

import enum
import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

from congen.core.findings import Finding, Severity
from congen.core.status import PublicationState, UploadStatus

REPORT_MARKDOWN = "VALIDATION.md"
REPORT_JSON = "validation.json"

#: Files whose content a validation depends on.
INPUT_FILES = ("config.yaml", "sample_sheet.csv", "README.txt")


class ReportState(enum.Enum):
    PASS = "PASS"
    PASS_WITH_WARNINGS = "PASS WITH WARNINGS"
    #: Clean apart from actionable tidying — "fine, some cleanup to do".
    PASS_WITH_NOTES = "PASS WITH NOTES"
    FAIL = "FAIL"
    #: Cannot be validated yet: no published data, or an incomplete upload.
    #: An expected state, not a failure — metadata routinely lands first.
    PENDING = "PENDING"
    #: Inputs changed after the recorded verdict was produced.
    STALE = "STALE"

    def __str__(self) -> str:
        return self.value


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _sha256(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def file_digest(path: Path) -> str | None:
    try:
        return _sha256(path.read_bytes())
    except (FileNotFoundError, OSError):
        return None


def input_digests(species_dir: Path) -> dict[str, str]:
    """Digest every input file that exists. Absent files are simply absent."""
    out: dict[str, str] = {}
    for name in INPUT_FILES:
        digest = file_digest(species_dir / name)
        if digest is not None:
            out[name] = digest
    return out


def data_digests(inventory, accession: str | None) -> dict:
    """Describe the published data precisely enough to notice a change."""
    if inventory is None or not getattr(inventory, "exists", False):
        return {"published": False, "accession": accession}

    objects: dict[str, str] = {}
    for name in ("raw.vcf.gz", "raw.vcf.gz.tbi", "filtered.vcf.gz", "sample_sheet.csv"):
        subdir = ("vcfs",) if name.endswith((".vcf.gz", ".tbi")) else ()
        obj = inventory.object(*subdir, name)
        if obj:
            objects[name] = f"etag:{obj.etag}"

    bams = inventory.bam_objects
    bam_fingerprint = _sha256(
        "\n".join(f"{n}:{o.etag}" for n, o in sorted(bams.items())).encode()
    )
    return {
        "published": True,
        "accession": accession,
        "objects": objects,
        "bams": {"count": len(bams), "digest": bam_fingerprint},
    }


def catalog_digest(checks: Iterable) -> str:
    """Fingerprint the checks that ran.

    Over `(id, severity)` pairs rather than the release version: adding a
    check or changing a severity means an old verdict no longer says what
    it said, while a bugfix release leaves every verdict identical.
    """
    pairs = sorted(f"{c.id}:{c.severity.value}" for c in checks)
    return _sha256("\n".join(pairs).encode())


def derive_state(findings: Sequence[Finding], status: UploadStatus | None) -> ReportState:
    """Errors outrank everything; an incomplete upload outranks a clean pass."""
    if any(f.severity is Severity.ERROR for f in findings):
        return ReportState.FAIL
    if status is None or status.state is not PublicationState.COMPLETE:
        # Nothing published, or published in part: validation could not
        # finish, so a pass would overstate what was checked.
        return ReportState.PENDING
    if any(f.severity is Severity.WARN for f in findings):
        return ReportState.PASS_WITH_WARNINGS
    if any(f.severity is Severity.INFO for f in findings):
        return ReportState.PASS_WITH_NOTES
    return ReportState.PASS


@dataclass
class ValidationRecord:
    subject: str
    state: ReportState
    validated_at: str
    tool_version: str
    catalog: str
    checks_run: list[str] = field(default_factory=list)
    checks_available: int = 0
    inputs: dict[str, str] = field(default_factory=dict)
    data: dict = field(default_factory=dict)
    findings: list[dict] = field(default_factory=list)
    status: dict | None = None
    #: The verdict this one replaced, kept when a report is stamped STALE.
    superseded: dict | None = None
    stale_reason: str | None = None

    @property
    def is_partial_selection(self) -> bool:
        return bool(self.checks_available) and len(self.checks_run) < self.checks_available

    def as_dict(self) -> dict:
        payload = asdict(self)
        payload["state"] = self.state.value
        return payload

    @classmethod
    def from_dict(cls, payload: dict) -> ValidationRecord:
        payload = dict(payload)
        payload["state"] = ReportState(payload["state"])
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in payload.items() if k in known})

    def write_json(self, path: Path) -> bool:
        from congen.core.metadata.writers import write_if_changed

        return write_if_changed(path, json.dumps(self.as_dict(), indent=2) + "\n")


def load_record(path: Path) -> ValidationRecord | None:
    try:
        return ValidationRecord.from_dict(json.loads(path.read_text("utf-8")))
    except (FileNotFoundError, OSError, json.JSONDecodeError, KeyError, ValueError):
        return None


def _relative_finding(finding: Finding, root: Path | None) -> dict:
    """Serialize a finding with its path relative to the metadata root.

    These files are committed, so an absolute path would bake whoever ran
    the tool into the repository.
    """
    payload = finding.as_dict()
    if root and payload.get("path"):
        try:
            payload["path"] = str(Path(payload["path"]).relative_to(root))
        except ValueError:
            pass
    return payload


def build_record(
    *,
    subject: str,
    findings: Sequence[Finding],
    status: UploadStatus | None,
    species_dir: Path,
    inventory,
    accession: str | None,
    checks_run: Sequence[str],
    checks_available: int,
    catalog: str,
    tool_version: str,
    validated_at: str | None = None,
    root: Path | None = None,
) -> ValidationRecord:
    reportable = [f for f in findings if f.severity is not Severity.SKIPPED]
    return ValidationRecord(
        subject=subject,
        state=derive_state(reportable, status),
        validated_at=validated_at or utc_now(),
        tool_version=tool_version,
        catalog=catalog,
        checks_run=sorted(checks_run),
        checks_available=checks_available,
        inputs=input_digests(species_dir),
        data=data_digests(inventory, accession),
        findings=[
            _relative_finding(f, root)
            for f in sorted(findings, key=lambda f: (f.severity.rank, f.id))
        ],
        status=status.as_dict() if status else None,
    )


@dataclass(frozen=True)
class Staleness:
    stale: bool
    reason: str | None = None

    def __bool__(self) -> bool:
        return self.stale


FRESH = Staleness(False)


def assess(
    record: ValidationRecord | None,
    *,
    species_dir: Path,
    catalog: str | None = None,
    published_accessions: set[str] | None = None,
) -> Staleness:
    """Decide whether a species needs revalidating.

    Deliberately cheap: input digests are local reads, and the published
    accession set is one bucket listing shared across the corpus. The
    expensive per-species data digests are only compared when a caller
    supplies them via :func:`assess_data`.
    """
    if record is None:
        return Staleness(True, "no previous report")

    current = input_digests(species_dir)
    if current != record.inputs:
        changed = sorted(
            set(current) ^ set(record.inputs)
            | {k for k in set(current) & set(record.inputs) if current[k] != record.inputs[k]}
        )
        return Staleness(True, f"{', '.join(changed)} changed since validation")

    if catalog is not None and record.catalog != catalog:
        return Staleness(True, "the check catalog changed")

    if published_accessions is not None and not record.data.get("published"):
        accession = record.data.get("accession")
        if accession and accession in published_accessions:
            return Staleness(True, "data has been published since validation")

    if record.state in (ReportState.FAIL, ReportState.STALE):
        return Staleness(True, f"last validation was {record.state.value}")
    if any(f["severity"] == "warn" for f in record.findings):
        return Staleness(True, "last validation raised warnings")

    return FRESH


def assess_data(record: ValidationRecord, inventory, accession: str | None) -> Staleness:
    """Compare published data against what the record saw."""
    current = data_digests(inventory, accession)
    if current != record.data:
        return Staleness(True, "published data changed since validation")
    return FRESH


def mark_stale(record: ValidationRecord, reason: str) -> ValidationRecord:
    """Supersede a verdict without pretending to a new one.

    Used when validation cannot run — no network, or data that does not
    exist yet. The old result is kept but can no longer be mistaken for
    current.
    """
    if record.state is ReportState.STALE:
        return record  # already superseded; do not nest
    superseded = record.as_dict()
    superseded.pop("superseded", None)
    return ValidationRecord(
        subject=record.subject,
        state=ReportState.STALE,
        validated_at=record.validated_at,
        tool_version=record.tool_version,
        catalog=record.catalog,
        checks_run=record.checks_run,
        checks_available=record.checks_available,
        inputs=record.inputs,
        data=record.data,
        findings=record.findings,
        status=record.status,
        superseded=superseded,
        stale_reason=reason,
    )
