"""Tool registration and discovery.

Tools are found through the ``congen.tools`` entry-point group, so a tool
can later move to its own distribution without the dispatcher changing.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib.metadata import entry_points
from typing import Callable

ENTRY_POINT_GROUP = "congen.tools"


@dataclass(frozen=True)
class Tool:
    name: str
    summary: str
    #: Whether this tool writes into congen-metadata. The validator does
    #: not; the readme generator will. CI can refuse write-capable tools
    #: by policy rather than by convention.
    writes_metadata: bool
    needs_network: bool
    command: Callable | None = None


def discover_tools() -> list[Tool]:
    found: list[Tool] = []
    for entry in entry_points(group=ENTRY_POINT_GROUP):
        try:
            tool = entry.load()
        except Exception:  # noqa: BLE001 - a broken tool must not break the CLI
            continue
        if isinstance(tool, Tool):
            found.append(tool)
    return sorted(found, key=lambda t: t.name)
