"""The validate tool's check registry."""

from __future__ import annotations

from congen.core.findings import CheckRegistry, Finding, Location, Severity

registry = CheckRegistry()

#: Withdrawn IDs, never to be reused. All five described a *state* rather
#: than a defect and now live in the report's status block instead:
#:
#: * ``G002`` data found under the GCA/GCF counterpart -> status.accession
#: * ``G003`` nothing published yet                    -> state: absent
#: * ``G015`` missing subdirectories                   -> status.missing
#: * ``G016`` filtered.vcf.gz present                  -> optional artifact
#: * ``R021`` no README.txt                            -> optional artifact
#:
#: ``P004`` and ``P005`` were the postprocess drift checks, dropped once
#: it was clear ``filtered.vcf.gz`` says nothing about the config.
registry.retire("G002", "G003", "G015", "G016", "R021", "P004", "P005")

check = registry.register

__all__ = ["Finding", "Location", "Severity", "check", "registry"]
