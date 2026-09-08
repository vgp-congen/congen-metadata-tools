"""Renderers for a :class:`congen.core.findings.Report`."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from congen.core.findings import Finding, Report, Severity
from congen.core.status import OPTIONAL_ARTIFACTS, PublicationState

SEVERITY_LABEL = {
    Severity.ERROR: "error",
    Severity.WARN: "warn",
    Severity.INFO: "info",
    Severity.SKIPPED: "skip",
}

#: GitHub only understands these three.
GITHUB_LEVEL = {
    Severity.ERROR: "error",
    Severity.WARN: "warning",
    Severity.INFO: "notice",
}


def _summary_line(report: Report) -> str:
    counts = report.counts()
    parts = [
        f"{counts[severity]} {SEVERITY_LABEL[severity]}"
        for severity in (Severity.ERROR, Severity.WARN, Severity.INFO)
        if counts[severity]
    ]
    if counts[Severity.SKIPPED]:
        parts.append(f"{counts[Severity.SKIPPED]} skipped")
    subjects = len(report.subjects)
    scope = f"{subjects} species" if subjects != 1 else report.subjects[0]
    return f"{scope}: {', '.join(parts) if parts else 'clean'}"


def _location_text(finding: Finding, root: Path | None) -> str:
    """Render a location, relative to ``root`` when it sits underneath."""
    assert finding.location is not None
    path = finding.location.path
    if root:
        try:
            path = path.relative_to(root)
        except ValueError:
            pass
    line = finding.location.line
    return f"{path}:{line}" if line else str(path)


#: How many species to name inline before summarizing.
MAX_NAMED = 8

STATE_ORDER = (PublicationState.COMPLETE, PublicationState.PARTIAL, PublicationState.ABSENT)


def render_status(report: Report) -> str:
    """The publication-state block.

    Separate from findings because these are descriptions, not defects: a
    species awaiting data is a normal state, and so is an absent optional
    artifact.
    """
    if not report.statuses:
        return ""

    lines = ["publication status"]
    counts = report.status_counts()
    for state in STATE_ORDER:
        count = counts[state]
        if not count:
            continue
        subjects = report.subjects_in_state(state)
        if state is PublicationState.COMPLETE:
            lines.append(f"  {state.value:9s} {count:3d}")
            continue
        names = ", ".join(s.split("/")[-1] for s in subjects[:MAX_NAMED])
        more = f" (+{len(subjects) - MAX_NAMED} more)" if len(subjects) > MAX_NAMED else ""
        lines.append(f"  {state.value:9s} {count:3d}   {names}{more}")

    published = [s for s in report.statuses if s.state is not PublicationState.ABSENT]
    if published:
        counted = [
            f"{artifact} {sum(1 for s in published if s.has(artifact))}/{len(published)}"
            for artifact in OPTIONAL_ARTIFACTS
        ]
        lines.append(f"  optional: {', '.join(counted)}")

    differing = [s for s in report.statuses if s.accession_differs]
    for status in differing:
        lines.append(
            f"  note: {status.subject} data is under {status.accession}, "
            f"config declares {status.declared_accession}"
        )
    return "\n".join(lines)


def render_human(
    report: Report,
    *,
    show_skipped: bool = False,
    show_info: bool = True,
    root: Path | None = None,
) -> str:
    """Group findings by species, worst first within each."""
    lines: list[str] = []
    hidden = {Severity.SKIPPED} if not show_skipped else set()
    if not show_info:
        hidden.add(Severity.INFO)

    for subject, findings in report.by_subject().items():
        visible = [f for f in findings if f.severity not in hidden]
        skipped = sum(1 for f in findings if f.severity is Severity.SKIPPED)
        if not visible:
            note = f"  ok{f' ({skipped} skipped)' if skipped else ''}"
            lines.append(f"{subject}\n{note}")
            continue

        lines.append(subject)
        for finding in visible:
            label = SEVERITY_LABEL[finding.severity]
            lines.append(f"  {label:5s} {finding.id}  {finding.message}")
            if finding.detail:
                lines.append(f"              {finding.detail}")
            if finding.location:
                lines.append(f"              at {_location_text(finding, root)}")
        if skipped and not show_skipped:
            lines.append(f"              ({skipped} check(s) skipped)")

    status_block = render_status(report)
    if status_block:
        lines.append("")
        lines.append(status_block)
    lines.append("")
    lines.append(_summary_line(report))
    return "\n".join(lines)


def render_json(report: Report, *, extra: dict | None = None) -> str:
    payload = {
        "subjects": report.subjects,
        "checks_run": report.checks_run,
        "statuses": [status.as_dict() for status in report.statuses],
        "status_counts": {
            state.value: count for state, count in report.status_counts().items()
        },
        "counts": {
            severity.value: count for severity, count in report.counts().items()
        },
        "findings": [finding.as_dict() for finding in report.findings],
    }
    if extra:
        payload.update(extra)
    return json.dumps(payload, indent=2, sort_keys=False)


def _escape(value: str) -> str:
    """GitHub workflow-command escaping for annotation properties."""
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _escape_data(value: str) -> str:
    return (
        value.replace("%", "%25")
        .replace("\r", "%0D")
        .replace("\n", "%0A")
        .replace(":", "%3A")
        .replace(",", "%2C")
    )


def render_github(findings: Iterable[Finding], *, root: Path | None = None) -> str:
    """Workflow commands, so CI comments land on the offending line.

    Paths must be relative to the workspace root for GitHub to anchor an
    annotation, so ``root`` is not cosmetic here.
    """
    lines: list[str] = []
    for finding in findings:
        level = GITHUB_LEVEL.get(finding.severity)
        if not level:
            continue  # skipped findings are not annotations
        properties = [f"title={_escape_data(f'{finding.id} {finding.subject}')}"]
        if finding.location:
            properties.append(f"file={_escape_data(_location_text(finding, root).rsplit(':', 1)[0] if finding.location.line else _location_text(finding, root))}")
            if finding.location.line:
                properties.append(f"line={finding.location.line}")
        message = finding.message
        if finding.detail:
            message = f"{message} — {finding.detail}"
        lines.append(f"::{level} {','.join(properties)}::{_escape(message)}")
    return "\n".join(lines)
