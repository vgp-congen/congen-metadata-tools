"""Importing this package registers the whole check catalog."""

from congen.tools.validate.checks import (  # noqa: F401
    tier0_repo,
    tier1_genomeark,
    tier2_samples,
    tier3a_reference,
    tier3b_canonical,
    tier4_provenance,
)
