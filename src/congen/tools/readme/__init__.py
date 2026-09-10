"""``congen readme`` — generate a per-species README.md in congen-metadata.

`writes_metadata=True` is what the registration field was added for: the
validator does not write, and this tool's whole purpose is to.
"""

from congen.core.tools import Tool
from congen.tools.readme.cli import readme

tool = Tool(
    name="readme",
    summary="Generate a per-species README.md from GenomeArk and the QC tables",
    writes_metadata=True,
    needs_network=True,
    command=readme,
)

__all__ = ["tool", "readme"]
