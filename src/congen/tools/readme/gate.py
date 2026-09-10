"""The two gates: may this document be written, and may it cite?

Two independent preconditions, because they are different questions with
different owners and different fixes.

**Gate A — is there a dataset to describe?** A complete publication, no
error findings, a validation record that still describes the files on
disk. Failing it truncates the document: *do not use this data*.

**Gate B — is the provenance chain sound?** Failing it blocks the
References block only: *do not publish on this yet*. The dataset is
usable; what is missing is the credit owed to whoever generated the
reads.

The split is the validator's findings/status distinction one level up.
Severity measures metadata hygiene and `status.state` measures whether a
dataset exists, and neither alone answers whether a document should be
written.

**This module asserts a policy the validator deliberately does not.**
`G017` is a *warning* in `congen validate`, because the sequence data is
sound — and that is the right call there. It is disqualifying *for
publication* because `README.txt` is the sole source of the bioproject
list, and therefore of every citation. Severity and publishability are
different axes; this is where the second one is decided, written down
rather than hidden inside a filter.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from pathlib import Path

from congen.core.validation_record import (
    INPUT_FILES,
    ReportState,
    ValidationRecord,
    file_digest,
)
from congen.core.status import PublicationState

#: Checks whose outcome decides whether citations may be rendered.
#:
#: Public API, with the same standing as the check IDs themselves: this
#: list belongs in one place, is named in the document a failure blocks,
#: and must be retired rather than renumbered. A renumbered ID would
#: silently stop gating.
GATING_CHECKS: tuple[str, ...] = ("G017", "G018", "R020", "E001", "E002", "E003")

#: The subset that says the bioproject list is *demonstrably wrong* rather
#: than merely absent or unverifiable. Only these justify the stronger
#: rendering, and only when they actually fired — for a species with no
#: `README.txt` they are *skipped*, which is a consequence of `G017`, not
#: evidence that anything is incorrect.
WRONGNESS_CHECKS: frozenset[str] = frozenset({"E001", "E002", "E003"})


class Mode(enum.Enum):
    """How much document gets written."""

    FULL = "full"
    TRUNCATED = "truncated"

    def __str__(self) -> str:
        return self.value


class CheckOutcome(enum.Enum):
    PASSED = "passed"
    FIRED = "fired"
    #: Ran, but its inputs were unavailable. Not a pass: "we could not
    #: check" is not "we checked".
    SKIPPED = "skipped"
    #: Never selected for the run at all — the tier-5 case, since
    #: `E001`-`E003` need `--check-sra`.
    NOT_RUN = "not_run"

    @property
    def is_sound(self) -> bool:
        return self is CheckOutcome.PASSED

    def __str__(self) -> str:
        return self.value


class Blocker(enum.Enum):
    """Why gate A failed. Codes, not prose — the renderer owns wording."""

    NO_RECORD = "no_record"
    STALE_RECORD = "stale_record"
    INPUTS_CHANGED = "inputs_changed"
    NO_DATA = "no_data"
    INCOMPLETE_UPLOAD = "incomplete_upload"
    VALIDATION_ERRORS = "validation_errors"
    #: The config now names a different accession than the harvest
    #: describes, so the document would link one dataset and describe
    #: another.
    ACCESSION_MOVED = "accession_moved"

    def __str__(self) -> str:
        return self.value


class Provenance(enum.Enum):
    """How the bioproject list stands, when gate B fails."""

    #: No reviewed list exists, or its soundness could not be established.
    MISSING = "missing"
    #: A list exists and is demonstrably incorrect.
    WRONG = "wrong"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class Reason:
    code: Blocker
    detail: str = ""

    def __str__(self) -> str:
        return f"{self.code}: {self.detail}" if self.detail else str(self.code)


@dataclass(frozen=True)
class CheckState:
    check: str
    outcome: CheckOutcome
    message: str = ""


@dataclass(frozen=True)
class Verdict:
    mode: Mode
    blockers: tuple[Reason, ...] = ()
    gating: tuple[CheckState, ...] = ()

    @property
    def is_full(self) -> bool:
        return self.mode is Mode.FULL

    @property
    def unsound(self) -> tuple[CheckState, ...]:
        """Gating checks that did not run clean, in `GATING_CHECKS` order."""
        return tuple(state for state in self.gating if not state.outcome.is_sound)

    @property
    def cited(self) -> bool:
        """Whether the References block may render.

        Requires every gating check to have **run and passed**. A skipped
        check and an unrun check are both *unknown*, and unknown is not
        sound — which is not a hypothetical distinction: tier 5 is opt-in,
        so `validate` without `--check-sra` leaves `E001`-`E003` unrun, and
        `R020` is skipped for every species lacking a `README.txt`.
        """
        return not self.unsound

    @property
    def provenance(self) -> Provenance | None:
        """Which of the two blocked renderings applies.

        `WRONG` only when a wrongness check actually fired. For a species
        with no `README.txt`, `E002`/`E003` are skipped rather than fired,
        and calling that "the list is incorrect" would assert something
        nobody established.
        """
        if self.cited:
            return None
        fired_wrong = any(
            state.check in WRONGNESS_CHECKS and state.outcome is CheckOutcome.FIRED
            for state in self.gating
        )
        return Provenance.WRONG if fired_wrong else Provenance.MISSING

    @property
    def fired(self) -> tuple[str, ...]:
        """Gating checks that actually fired, as opposed to being unknown.

        The reason to name in a document: for a species with no README,
        `G017` fired and `R020`/`E002`/`E003` are downstream consequences.
        """
        return tuple(
            state.check for state in self.gating if state.outcome is CheckOutcome.FIRED
        )

    def has(self, code: Blocker) -> bool:
        return any(reason.code is code for reason in self.blockers)


def _severity_by_id(record: ValidationRecord) -> dict[str, str]:
    return {finding["id"]: finding["severity"] for finding in record.findings}


def changed_inputs(species_dir: Path, record: ValidationRecord) -> list[str]:
    """Repo files whose content no longer matches the validated digest.

    Three hashes of small files, offline. Mandatory rather than advisory
    because the *mode* is derived from this record: a stale one does not
    merely misreport a verdict, it can render a full document with
    download links and statistics for a species that would now fail.
    """
    out = []
    for name in record.inputs:
        if name not in INPUT_FILES:
            continue
        if file_digest(species_dir / name) != record.inputs[name]:
            out.append(name)
    return sorted(out)


def gating_states(record: ValidationRecord) -> tuple[CheckState, ...]:
    severities = _severity_by_id(record)
    ran = set(record.checks_run)
    states = []
    for check in GATING_CHECKS:
        if check not in ran:
            outcome = CheckOutcome.NOT_RUN
        elif severities.get(check) == "skipped":
            outcome = CheckOutcome.SKIPPED
        elif check in severities:
            outcome = CheckOutcome.FIRED
        else:
            outcome = CheckOutcome.PASSED
        message = ""
        if outcome is CheckOutcome.FIRED:
            message = next(
                (f.get("message", "") for f in record.findings if f["id"] == check), ""
            )
        states.append(CheckState(check=check, outcome=outcome, message=message))
    return tuple(states)


def evaluate(
    species_dir: Path,
    record: ValidationRecord | None,
    *,
    harvested_accession: str | None = None,
) -> Verdict:
    """Decide the document mode and whether citations may render.

    `harvested_accession` is `dataset.json`'s, checked against the
    record's so the document can never describe one accession while
    linking another. `grus-americana` and `sturnus-vulgaris` both declare
    something other than where their data sits, so this is not theoretical.
    """
    if record is None:
        return Verdict(
            mode=Mode.TRUNCATED,
            blockers=(Reason(Blocker.NO_RECORD, "no validation.json"),),
        )

    blockers: list[Reason] = []

    if record.state is ReportState.STALE:
        blockers.append(
            Reason(Blocker.STALE_RECORD, record.stale_reason or "inputs changed")
        )

    changed = changed_inputs(species_dir, record)
    if changed:
        blockers.append(Reason(Blocker.INPUTS_CHANGED, ", ".join(changed)))

    status = record.status or {}
    state = status.get("state")
    if state == PublicationState.ABSENT.value:
        blockers.append(Reason(Blocker.NO_DATA, "nothing published on GenomeArk"))
    elif state != PublicationState.COMPLETE.value:
        missing = ", ".join(status.get("missing") or ()) or "unknown"
        blockers.append(Reason(Blocker.INCOMPLETE_UPLOAD, f"missing {missing}"))

    errors = sorted(
        {f["id"] for f in record.findings if f["severity"] == "error"}
    )
    if errors:
        blockers.append(Reason(Blocker.VALIDATION_ERRORS, ", ".join(errors)))

    recorded_accession = (record.data or {}).get("accession")
    if harvested_accession and recorded_accession and harvested_accession != recorded_accession:
        blockers.append(
            Reason(
                Blocker.ACCESSION_MOVED,
                f"harvested {harvested_accession}, validated {recorded_accession}",
            )
        )

    return Verdict(
        mode=Mode.TRUNCATED if blockers else Mode.FULL,
        blockers=tuple(blockers),
        gating=gating_states(record),
    )
