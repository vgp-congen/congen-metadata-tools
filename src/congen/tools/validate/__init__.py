"""``congen validate`` — cross-check metadata against GenomeArk."""

from congen.core.tools import Tool
from congen.tools.validate.cli import validate

tool = Tool(
    name="validate",
    summary="Cross-check species metadata against GenomeArk outputs",
    writes_metadata=False,
    needs_network=True,
    command=validate,
)

__all__ = ["tool", "validate"]
