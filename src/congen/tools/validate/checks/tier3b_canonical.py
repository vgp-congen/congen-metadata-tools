"""Tier 3b — reference canonicality.

Tier 3a (milestone 3) proves the published data is self-consistent with
whatever config.yaml declares. It cannot tell you the config declares the
wrong assembly. The VGP reference-genome list can: it is the authority on
which assembly a species should be called against.
"""

from __future__ import annotations

from congen.core.findings import Finding, Location, Severity
from congen.core.metadata.vgp import slugify
from congen.tools.validate.context import CONFIG, NCBI, VGP, Context
from congen.tools.validate.registry import check


def _assembly_label(context: Context) -> str:
    """Describe the declared assembly, using NCBI when it is available."""
    info = context.ncbi_info
    if not info:
        return ""
    bits = [b for b in (info.assembly_name, info.assembly_level) if b]
    return f" ({', '.join(bits)})" if bits else ""


@check(
    id="F020",
    tier="F",
    severity=Severity.ERROR,
    summary="reference.source is the VGP main-haplotype assembly",
    needs=(VGP, CONFIG),
)
def reference_is_the_vgp_assembly(context: Context) -> list[Finding]:
    """The config names an assembly that is not the VGP reference.

    Not a GCA/GCF namespace variant of the right one — see `F021` for
    that — but a different assembly altogether, so any run against it
    used the wrong genome.
    """
    entry = context.vgp_entry
    declared = context.declared_accession
    if not entry or not declared:
        return []
    if context.species.reference.same_assembly_as(entry.accession):
        return []  # either exact, or the accession-form case F021 owns
    return [
        Finding(
            id="F020",
            severity=Severity.ERROR,
            subject=context.subject,
            message=(
                f"config declares {declared}{_assembly_label(context)}, "
                f"but the VGP assembly for {entry.scientific_name} is {entry.accession}"
            ),
            detail="a different assembly, not an accession-namespace variant",
            location=Location(context.config.path),
        )
    ]


@check(
    id="F021",
    tier="F",
    severity=Severity.ERROR,
    summary="reference.source uses the canonical accession form",
    needs=(VGP, CONFIG),
)
def reference_uses_canonical_accession(context: Context) -> list[Finding]:
    """The GCA/GCF counterpart of the right assembly is still wrong.

    An error rather than a warning because the VGP list is an authority
    and the fix is mechanical: normalize to the listed accession. Before
    the list existed this could only be a warning.
    """
    entry = context.vgp_entry
    declared = context.declared_accession
    if not entry or not declared or declared == entry.accession:
        return []
    if not context.species.reference.same_assembly_as(entry.accession):
        return []  # F020 owns a genuinely different assembly

    detail = "same assembly, non-canonical accession namespace"
    info = context.ncbi_info
    if info and info.names_same_assembly_as(entry.accession):
        detail += "; confirmed by NCBI paired_assembly"
    return [
        Finding(
            id="F021",
            severity=Severity.ERROR,
            subject=context.subject,
            message=f"config declares {declared}; the VGP accession is {entry.accession}",
            detail=detail,
            location=Location(context.config.path),
        )
    ]


@check(
    id="F022",
    tier="F",
    severity=Severity.ERROR,
    summary="the species is in the VGP reference list",
    needs=(VGP,),
)
def species_is_in_the_vgp_list(context: Context) -> list[Finding]:
    """Every congen species is by definition a VGP species.

    An error rather than a warning, even though it fires on nothing
    today: all 79 species resolve. If it ever fires, either the list is
    stale or the species does not belong in the corpus, and both want
    attention rather than a line in the warnings.
    """
    if context.vgp_entry:
        return []
    assert context.vgp_list
    candidates = context.vgp_list.candidates_for_slug(context.species.slug)
    if candidates:
        names = ", ".join(f"{c.scientific_name} ({c.accession})" for c in candidates)
        return [
            Finding(
                id="F022",
                severity=Severity.ERROR,
                subject=context.subject,
                message=f"{context.species.slug} matches {len(candidates)} VGP entries",
                detail=names,
            )
        ]
    return [
        Finding(
            id="F022",
            severity=Severity.ERROR,
            subject=context.subject,
            message=f"{context.species.slug} has no entry in the VGP reference list",
        )
    ]


@check(
    id="F023",
    tier="F",
    severity=Severity.WARN,
    summary="the VGP species name agrees with the directory and reference.name",
    needs=(VGP, CONFIG),
)
def species_names_agree(context: Context) -> list[Finding]:
    entry = context.vgp_entry
    if not entry:
        return []

    out: list[Finding] = []
    expected = slugify(entry.scientific_name)
    actual = context.species.slug
    if actual not in {expected, entry.binomial_slug}:
        out.append(
            Finding(
                id="F023",
                severity=Severity.WARN,
                subject=context.subject,
                message=(
                    f"directory {actual!r} does not match VGP name "
                    f"{entry.scientific_name!r}"
                ),
            )
        )

    name = context.species.reference.name
    if name:
        from congen.core.metadata.models import ACCESSION_RE

        if not ACCESSION_RE.match(name):  # R003 owns the accession case
            declared_slug = slugify(name.replace("_", " "))
            if declared_slug not in {expected, entry.binomial_slug}:
                out.append(
                    Finding(
                        id="F023",
                        severity=Severity.WARN,
                        subject=context.subject,
                        message=(
                            f"reference.name {name!r} does not match VGP name "
                            f"{entry.scientific_name!r}"
                        ),
                        location=Location(context.config.path),
                    )
                )
    return out


@check(
    id="F024",
    tier="F",
    severity=Severity.WARN,
    summary="the assembly's NCBI taxid matches the VGP list",
    needs=(VGP, NCBI),
)
def taxid_matches_vgp(context: Context) -> list[Finding]:
    """The VGP list's `QID` column holds NCBI taxonomy IDs, not Wikidata QIDs."""
    entry = context.vgp_entry
    info = context.ncbi_info
    if not entry or not info or entry.ncbi_taxid is None or info.tax_id is None:
        return []
    if entry.ncbi_taxid == info.tax_id:
        return []
    return [
        Finding(
            id="F024",
            severity=Severity.WARN,
            subject=context.subject,
            message=(
                f"{info.accession} has NCBI taxid {info.tax_id} "
                f"({info.organism_name}), VGP list says {entry.ncbi_taxid}"
            ),
        )
    ]
