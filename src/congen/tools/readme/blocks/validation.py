"""The validation block — can I trust this?

Reads `validation.json`. It does not re-run validation: duplicating the
findings would create two places for them to go stale.

Renders as a GitHub alert so it reads as a gate rather than as prose. The
callout also carries the gate-B provenance line, which is what preserves
truncation's one real benefit — a binary trust signal at the top of the
file — while still letting the document body be useful.
"""

from __future__ import annotations

from congen.core.status import PublicationState
from congen.core.validation_record import ReportState

from ..gate import Blocker, Mode, Provenance
from ..format import alert, sentence_list
from . import Context, Rendered

CHECKS_HREF = "../../../CHECKS.md"

#: Report state to alert kind. `PENDING` is a NOTE, not a warning:
#: metadata routinely lands before the pipeline run, and that is an
#: expected state rather than a problem with anything.
ALERT_FOR_STATE = {
    ReportState.PASS: "TIP",
    ReportState.PASS_WITH_WARNINGS: "NOTE",
    ReportState.PASS_WITH_NOTES: "NOTE",
    ReportState.PENDING: "NOTE",
    ReportState.STALE: "WARNING",
    ReportState.FAIL: "CAUTION",
}

#: Why a truncated document is truncated, in the words a reader needs.
#: `FAIL` and `PENDING` want different text in the same shape: one is a
#: defect, the other is a schedule.
BLOCKER_BLURB = {
    Blocker.NO_RECORD: "This species has not been validated yet.",
    Blocker.STALE_RECORD: (
        "The metadata changed after it was last validated, so the verdict "
        "below no longer describes these files."
    ),
    Blocker.INPUTS_CHANGED: (
        "The metadata has changed since it was validated, so nothing here can "
        "be relied on until it is revalidated."
    ),
    Blocker.NO_DATA: (
        "No data has been published for this species yet. This is an expected "
        "state: metadata is committed before the pipeline runs."
    ),
    Blocker.INCOMPLETE_UPLOAD: (
        "The upload is incomplete, so this dataset is not ready to use."
    ),
    Blocker.VALIDATION_ERRORS: (
        "Validation found errors. **Do not use this dataset** until they are "
        "resolved."
    ),
    Blocker.ACCESSION_MOVED: (
        "The declared reference assembly no longer matches the published data, "
        "so nothing here would describe the same dataset it links to."
    ),
}

#: Most severe first. Only the leading blocker gets a full sentence; the
#: rest are named compactly. Without an order, `sturnus-vulgaris` reads
#: "this is an expected state" immediately above "validation found
#: errors", which is two blurbs written for different situations
#: colliding.
BLOCKER_PRECEDENCE = (
    Blocker.VALIDATION_ERRORS,
    Blocker.INPUTS_CHANGED,
    Blocker.STALE_RECORD,
    Blocker.ACCESSION_MOVED,
    Blocker.INCOMPLETE_UPLOAD,
    Blocker.NO_DATA,
    Blocker.NO_RECORD,
)

#: A short name per blocker, for the secondary list.
BLOCKER_SHORT = {
    Blocker.NO_RECORD: "not validated",
    Blocker.STALE_RECORD: "the verdict is stale",
    Blocker.INPUTS_CHANGED: "the metadata has changed since validation",
    Blocker.NO_DATA: "no data published yet",
    Blocker.INCOMPLETE_UPLOAD: "the upload is incomplete",
    Blocker.VALIDATION_ERRORS: "validation errors",
    Blocker.ACCESSION_MOVED: "the reference assembly has moved",
}


def leading_blocker(verdict):
    """The blocker whose wording should lead. `None` for a full document."""
    for code in BLOCKER_PRECEDENCE:
        for reason in verdict.blockers:
            if reason.code is code:
                return reason
    return None

PROVENANCE_LINE = {
    Provenance.MISSING: (
        "**Provenance incomplete** — no reviewed list of contributing "
        "BioProjects exists for this species, so it cannot yet be cited."
    ),
    Provenance.WRONG: (
        "**Provenance incorrect** — the recorded list of contributing "
        "BioProjects is known to be wrong, so it cannot be used for citation."
    ),
}


def render(context: Context) -> Rendered:
    record = context.record
    verdict = context.verdict

    if record is None:
        return Rendered(
            alert(
                "WARNING",
                [
                    "**UNVALIDATED**",
                    "",
                    BLOCKER_BLURB[Blocker.NO_RECORD],
                    "",
                    f"Check definitions: [`CHECKS.md`]({CHECKS_HREF}).",
                ],
            )
        )

    kind = ALERT_FOR_STATE.get(record.state, "WARNING")
    if verdict.is_full and not verdict.cited:
        # The data is sound; the citation chain is not. Downgrade a TIP so
        # the top of the file is never greener than the document below it.
        kind = "WARNING" if kind == "TIP" else kind

    headline = [f"**{record.state.value}** · validated {record.validated_at[:10]}"]
    accession = (record.data or {}).get("accession")
    if accession:
        headline[0] += f" · `{accession}`"

    body = [*headline, ""]

    if verdict.is_full:
        body.append(_clean_line(record))
    else:
        body += _truncated_body(verdict)

    # Only a full document has a References section for this to point at,
    # and a reader who cannot use the dataset at all does not need to be
    # told they also cannot cite it.
    if verdict.is_full and not verdict.cited:
        line = PROVENANCE_LINE[verdict.provenance]
        fired = sentence_list([f"`{check}`" for check in verdict.fired])
        if fired:
            line += f" ({fired})"
        body += ["", line, "", "See [References](#references)."]

    disagreement = _sample_disagreement(record.status or {})
    if disagreement:
        body += ["", disagreement]

    body += [
        "",
        f"Full report: [`VALIDATION.md`](VALIDATION.md) · "
        f"check definitions: [`CHECKS.md`]({CHECKS_HREF}) · "
        f"baseline description: [`README.txt`](README.txt)",
    ]
    return Rendered(alert(kind, body))


def _truncated_body(verdict) -> list[str]:
    leading = leading_blocker(verdict)
    if leading is None:  # pragma: no cover - a truncated verdict always has one
        return ["This dataset is not ready to use."]

    sentence = BLOCKER_BLURB.get(leading.code, str(leading.code))
    if leading.detail and leading.code is Blocker.VALIDATION_ERRORS:
        ids = sentence_list([f"`{i.strip()}`" for i in leading.detail.split(",")])
        sentence = sentence.replace("errors.", f"errors ({ids}).")
    elif leading.detail and leading.code is not Blocker.NO_DATA:
        # NO_DATA's detail ("nothing published on GenomeArk") only repeats
        # its own sentence.
        sentence = f"{sentence.rstrip('.')} — {leading.detail}."

    others = [
        BLOCKER_SHORT[reason.code]
        for reason in verdict.blockers
        if reason.code is not leading.code and reason.code in BLOCKER_SHORT
    ]
    out = [sentence]
    if others:
        out += ["", f"Also: {sentence_list(others)}."]
    return out


def _clean_line(record) -> str:
    tally = []
    for severity, noun in (("warn", "warning"), ("info", "note")):
        count = sum(1 for f in record.findings if f["severity"] == severity)
        if count:
            tally.append(f"{count} {noun}{'s' if count != 1 else ''}")
    suffix = f", with {sentence_list(tally)} to note" if tally else ""
    return f"Metadata agrees with the data published on GenomeArk{suffix}."


def _sample_disagreement(status: dict) -> str | None:
    """The sheet/BAM/VCF triple, but only when it means something.

    Two guards, and both are load-bearing.

    When the three agree, the number is already in the document twice.
    When they disagree it is the most important fact on the page — the
    `anser-albifrons` case, where a sample sits in the BAMs and the VCF
    but not the sheet.

    And it is only asked **once the upload is complete**. During a partial
    or absent publication the BAM set is by definition not final, so the
    comparison measures how far the upload got rather than whether the
    metadata is right: an unpublished species would otherwise report
    "215 in the sheet, 0 BAMs" as a disagreement. This is the same guard
    the validator puts on `S002` and `S003`, for the same reason.
    """
    if status.get("state") != PublicationState.COMPLETE.value:
        return None
    counts = status.get("counts") or {}
    values = [counts.get(key) for key in ("sheet_samples", "bams", "vcf_samples")]
    present = [value for value in values if value is not None]
    if len(present) < 2 or len(set(present)) < 2:
        return None
    sheet, bams, vcf = (
        "—" if value is None else str(value) for value in values
    )
    return (
        f"**Sample counts disagree**: {sheet} in the sheet · {bams} BAMs · "
        f"{vcf} in the VCF."
    )
