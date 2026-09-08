"""Renderers for a :class:`congen.core.findings.Report`."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from congen.core.findings import Finding, Report, Severity

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

    lines.append("")
    lines.append(_summary_line(report))
    return "\n".join(lines)


def render_json(report: Report, *, extra: dict | None = None) -> str:
    payload = {
        "subjects": report.subjects,
        "checks_run": report.checks_run,
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
