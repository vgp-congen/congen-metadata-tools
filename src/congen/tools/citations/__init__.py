"""``congen citations`` — curate the citations a README depends on.

A separate tool from `readme` rather than a flag on it: it writes a
different file, on a different cadence, and its network dependency is
Europe PMC rather than GenomeArk.
"""

from congen.core.tools import Tool
from congen.tools.citations.cli import citations

tool = Tool(
    name="citations",
    summary="Propose and review publication citations for contributing BioProjects",
    writes_metadata=True,
    needs_network=True,
    command=citations,
)

__all__ = ["tool", "citations"]
