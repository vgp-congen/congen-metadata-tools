"""Findings, and the registry that produces them.

A check is a pure function over a prefetched context. It declares which
slices of context it needs, which buys two things: the gather phase
fetches only what the selected checks require, and a check whose inputs
are unavailable reports SKIPPED rather than ERROR — so a species with
BAMs but no VCF yet yields one honest finding about the missing VCF
instead of a cascade of false sample mismatches.

Check IDs are public API. CI configs suppress them by ID, so retire an
ID rather than renumbering it.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Protocol, Sequence


class Severity(enum.Enum):
    ERROR = "error"
    WARN = "warn"
    INFO = "info"
    SKIPPED = "skipped"

    @property
    def rank(self) -> int:
        """Sort order for reports: worst first."""
        return _SEVERITY_RANK[self]

    def __str__(self) -> str:
        return self.value


_SEVERITY_RANK = {
    Severity.ERROR: 0,
    Severity.WARN: 1,
    Severity.INFO: 2,
    Severity.SKIPPED: 3,
}


@dataclass(frozen=True)
class Location:
    """Where in the tree a finding points, for CI annotations."""

    path: Path
    line: int | None = None

    def __str__(self) -> str:
        return f"{self.path}:{self.line}" if self.line else str(self.path)


@dataclass(frozen=True)
class Finding:
    id: str
    severity: Severity
    subject: str
    message: str
    detail: str | None = None
    location: Location | None = None

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "severity": self.severity.value,
            "subject": self.subject,
            "message": self.message,
            "detail": self.detail,
            "path": str(self.location.path) if self.location else None,
            "line": self.location.line if self.location else None,
        }


class CheckContext(Protocol):
    """What every check receives.

    ``available`` names the context slices that were successfully
    gathered; ``needs`` on a check is matched against it.
    """

    subject: str
    available: set[str]


@dataclass(frozen=True)
class Check:
    id: str
    tier: str
    severity: Severity
    summary: str
    needs: tuple[str, ...]
    func: Callable[[CheckContext], Iterable[Finding]]

    def missing(self, context: CheckContext) -> tuple[str, ...]:
        return tuple(n for n in self.needs if n not in context.available)


class CheckRegistry:
    """Holds the check catalog. One instance per tool."""

    def __init__(self) -> None:
        self._checks: dict[str, Check] = {}

    def register(
        self,
        *,
        id: str,
        tier: str,
        severity: Severity,
        summary: str,
        needs: Sequence[str] = (),
    ):
        def decorator(func):
            if id in self._checks:
                raise ValueError(f"check {id} is already registered")
            self._checks[id] = Check(
                id=id,
                tier=tier,
                severity=severity,
                summary=summary,
                needs=tuple(needs),
                func=func,
            )
            return func

        return decorator

    def __len__(self) -> int:
        return len(self._checks)

    def __contains__(self, check_id: object) -> bool:
        return check_id in self._checks

    def get(self, check_id: str) -> Check | None:
        return self._checks.get(check_id)

    @property
    def all(self) -> list[Check]:
        return sorted(self._checks.values(), key=lambda c: c.id)

    def select(
        self,
        *,
        only: Sequence[str] | None = None,
        skip: Sequence[str] | None = None,
    ) -> list[Check]:
        """Choose checks by ID or ID prefix.

        ``only=["S", "F020"]`` takes every tier-S check plus F020;
        ``skip`` removes matches from whatever ``only`` produced.
        """
        chosen = self.all
        # An all-blank pattern list means "no filter", never "match
        # nothing": silently selecting zero checks would let a typo'd
        # --only make a CI job pass without validating anything.
        include = tuple(p.strip() for p in (only or ()) if p.strip())
        if include:
            chosen = [c for c in chosen if c.id.startswith(include)]
        exclude = tuple(p.strip() for p in (skip or ()) if p.strip())
        if exclude:
            chosen = [c for c in chosen if not c.id.startswith(exclude)]
        return chosen

    def required_context(self, checks: Iterable[Check]) -> set[str]:
        """Union of the context slices ``checks`` declare."""
        needed: set[str] = set()
        for check in checks:
            needed.update(check.needs)
        return needed

    def run(
        self,
        context: CheckContext,
        checks: Iterable[Check] | None = None,
    ) -> list[Finding]:
        """Run checks against one context, in ID order.

        A check that raises produces an ERROR finding rather than taking
        the whole run down: one malformed species should not hide the
        other 78.
        """
        out: list[Finding] = []
        for check in checks if checks is not None else self.all:
            missing = check.missing(context)
            if missing:
                out.append(
                    Finding(
                        id=check.id,
                        severity=Severity.SKIPPED,
                        subject=context.subject,
                        message=f"skipped: {check.summary}",
                        detail=f"needs {', '.join(missing)}",
                    )
                )
                continue
            try:
                out.extend(check.func(context))
            except Exception as exc:  # noqa: BLE001 - reported, not raised
                out.append(
                    Finding(
                        id=check.id,
                        severity=Severity.ERROR,
                        subject=context.subject,
                        message=f"check raised {type(exc).__name__}: {exc}",
                        detail=check.summary,
                    )
                )
        return out


@dataclass
class Report:
    """A run's findings plus what was run."""

    findings: list[Finding] = field(default_factory=list)
    subjects: list[str] = field(default_factory=list)
    checks_run: list[str] = field(default_factory=list)

    def extend(self, findings: Iterable[Finding]) -> None:
        self.findings.extend(findings)

    def counts(self) -> dict[Severity, int]:
        out = {severity: 0 for severity in Severity}
        for finding in self.findings:
            out[finding.severity] += 1
        return out

    def of(self, severity: Severity) -> list[Finding]:
        return [f for f in self.findings if f.severity is severity]

    @property
    def errors(self) -> list[Finding]:
        return self.of(Severity.ERROR)

    @property
    def warnings(self) -> list[Finding]:
        return self.of(Severity.WARN)

    def by_subject(self) -> dict[str, list[Finding]]:
        out: dict[str, list[Finding]] = {s: [] for s in self.subjects}
        for finding in self.findings:
            out.setdefault(finding.subject, []).append(finding)
        for findings in out.values():
            findings.sort(key=lambda f: (f.severity.rank, f.id))
        return out

    def exit_code(self, *, strict: bool = False) -> int:
        """0 clean or warnings only, 1 on errors (or warnings if strict)."""
        if self.errors:
            return 1
        if strict and self.warnings:
            return 1
        return 0
