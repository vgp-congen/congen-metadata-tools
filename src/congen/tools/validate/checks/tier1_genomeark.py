"""Tier 1 — is the data on GenomeArk, and is it complete?"""

from __future__ import annotations

from congen.core.findings import Finding, Location, Severity
from congen.core.remote.genomeark import EXPECTED_SUBDIRS
from congen.tools.validate.context import (
    CONFIG,
    FILTERED_VCF,
    RAW_VCF,
    S3,
    Context,
)
from congen.tools.validate.registry import check

#: `qc/ind_filter_list.txt` is legitimately empty when no individual is
#: filtered — the only zero-byte object anywhere in the corpus. Emptiness
#: checks therefore cover the data directories, where zero bytes always
#: means a broken upload.
EMPTINESS_SCOPE = ("bams", "vcfs", "<top>")


@check(
    id="G001",
    tier="G",
    severity=Severity.ERROR,
    summary="the accession has a prefix on GenomeArk",
    needs=(CONFIG,),
)
def accession_exists_on_s3(context: Context) -> list[Finding]:
    # G003 covers "nothing published at all"; this fires only when data
    # was expected and the lookup itself found nothing usable.
    if context.resolved_accession and not (context.inventory and context.inventory.exists):
        return [
            Finding(
                id="G001",
                severity=Severity.ERROR,
                subject=context.subject,
                message=f"{context.resolved_accession} exists but published no objects",
            )
        ]
    return []


@check(
    id="G002",
    tier="G",
    severity=Severity.WARN,
    summary="data is published under the accession the config declares",
    needs=(S3,),
)
def accession_matches_publication(context: Context) -> list[Finding]:
    if not context.accession_was_flipped:
        return []
    return [
        Finding(
            id="G002",
            severity=Severity.WARN,
            subject=context.subject,
            message=(
                f"data is published under {context.resolved_accession}, "
                f"but the config declares {context.declared_accession}"
            ),
            detail="the same assembly in the other accession namespace; see F021",
            location=Location(context.config.path),
        )
    ]


@check(
    id="G003",
    tier="G",
    severity=Severity.WARN,
    summary="the species has data published on GenomeArk",
    needs=(CONFIG,),
)
def data_is_published(context: Context) -> list[Finding]:
    """A pending run is a normal state, but it should not be silent.

    Worded as information rather than failure, and it is what makes every
    downstream tier report SKIPPED instead of erroring.
    """
    if context.resolved_accession:
        return []
    return [
        Finding(
            id="G003",
            severity=Severity.WARN,
            subject=context.subject,
            message="no data published on GenomeArk yet",
            detail=f"looked for {context.declared_accession} and its GCA/GCF counterpart",
        )
    ]


@check(
    id="G010",
    tier="G",
    severity=Severity.ERROR,
    summary="vcfs/raw.vcf.gz is present",
    needs=(S3,),
)
def raw_vcf_present(context: Context) -> list[Finding]:
    assert context.inventory
    if context.inventory.object("vcfs", RAW_VCF):
        return []
    return [
        Finding(
            id="G010",
            severity=Severity.ERROR,
            subject=context.subject,
            message=f"no vcfs/{RAW_VCF}",
            # Say what is there, not what it means. Whether this is an
            # upload in progress or something else is not ours to infer.
            detail=(
                f"{len(context.inventory.bam_objects)} BAM(s) present, no VCF"
                if context.inventory.bam_objects
                else "nothing in vcfs/"
            ),
        )
    ]


@check(
    id="G011",
    tier="G",
    severity=Severity.ERROR,
    summary="the raw VCF has an index",
    needs=(S3,),
)
def raw_vcf_index_present(context: Context) -> list[Finding]:
    assert context.inventory
    vcf = context.inventory.object("vcfs", RAW_VCF)
    if not vcf:
        return []  # G010 owns this
    index = context.inventory.object("vcfs", f"{RAW_VCF}.tbi")
    if not index:
        return [
            Finding(
                id="G011",
                severity=Severity.ERROR,
                subject=context.subject,
                message=f"no vcfs/{RAW_VCF}.tbi",
            )
        ]
    # Presence is all that can honestly be checked here. An earlier
    # version also compared LastModified, but S3 timestamps record upload
    # order, not generation order: hirundo-rustica's index is stamped one
    # second before its VCF simply because that is the order they were
    # pushed. That cannot distinguish a stale index from a normal upload,
    # so the comparison is gone rather than given a fudge factor.
    return []


@check(
    id="G012",
    tier="G",
    severity=Severity.ERROR,
    summary="bams/ is not empty",
    needs=(S3,),
)
def bams_present(context: Context) -> list[Finding]:
    assert context.inventory
    if context.inventory.bam_objects:
        return []
    return [
        Finding(
            id="G012",
            severity=Severity.ERROR,
            subject=context.subject,
            message="no BAMs published",
        )
    ]


@check(
    id="G013",
    tier="G",
    severity=Severity.ERROR,
    summary="every BAM has an index",
    needs=(S3,),
)
def bams_are_indexed(context: Context) -> list[Finding]:
    assert context.inventory
    indexes = context.inventory.bam_index_names
    missing = sorted(
        name
        for name in context.inventory.bam_objects
        if f"{name}.bam.csi" not in indexes and f"{name}.bam.bai" not in indexes
    )
    if not missing:
        return []
    shown = ", ".join(missing[:5])
    more = f" (+{len(missing) - 5} more)" if len(missing) > 5 else ""
    return [
        Finding(
            id="G013",
            severity=Severity.ERROR,
            subject=context.subject,
            message=f"{len(missing)} BAM(s) have no .csi or .bai index",
            detail=f"{shown}{more}",
        )
    ]


@check(
    id="G014",
    tier="G",
    severity=Severity.ERROR,
    summary="no zero-byte data objects",
    needs=(S3,),
)
def no_empty_data_objects(context: Context) -> list[Finding]:
    assert context.inventory
    inventory = context.inventory
    suspects = []
    for listing in (inventory.top, inventory.bams, inventory.vcfs):
        if listing:
            suspects.extend(o for o in listing.objects if o.is_empty)
    if not suspects:
        return []
    return [
        Finding(
            id="G014",
            severity=Severity.ERROR,
            subject=context.subject,
            message=f"{len(suspects)} zero-byte object(s) in the data directories",
            detail=", ".join(sorted(o.key.split("/", 4)[-1] for o in suspects)),
        )
    ]


@check(
    id="G015",
    tier="G",
    severity=Severity.WARN,
    summary="the expected subdirectories are present",
    needs=(S3,),
)
def expected_subdirs_present(context: Context) -> list[Finding]:
    assert context.inventory
    missing = [name for name in EXPECTED_SUBDIRS if name not in context.inventory.subdirs]
    if not missing:
        return []
    return [
        Finding(
            id="G015",
            severity=Severity.WARN,
            subject=context.subject,
            message=f"missing subdirector{'y' if len(missing) == 1 else 'ies'}: {', '.join(missing)}",
        )
    ]


@check(
    id="G016",
    tier="G",
    severity=Severity.INFO,
    summary="records whether filtered.vcf.gz is present",
    needs=(S3,),
)
def filtered_vcf_inventory(context: Context) -> list[Finding]:
    """Recorded, never judged.

    ``filtered.vcf.gz`` is a default GATK output that some snpArcher
    versions omitted, so its presence says nothing about the config or
    the validity of the run. Deliberately uncorrelated with
    ``modules.postprocess.enabled``; a future filtering pipeline owns
    this file.
    """
    assert context.inventory
    present = FILTERED_VCF in context.inventory.vcf_names
    return [
        Finding(
            id="G016",
            severity=Severity.INFO,
            subject=context.subject,
            message=f"{FILTERED_VCF} {'present' if present else 'absent'}",
        )
    ]
