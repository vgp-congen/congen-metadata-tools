"""The validate tool's check registry."""

from __future__ import annotations

from congen.core.findings import CheckRegistry, Finding, Location, Severity

registry = CheckRegistry()
check = registry.register

__all__ = ["Finding", "Location", "Severity", "check", "registry"]
