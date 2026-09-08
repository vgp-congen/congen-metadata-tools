"""Publication status: how far along a species' upload is.

This is deliberately *not* a finding. Findings answer "what is wrong?";
status answers "how far along is this?" Those are different questions,
and an earlier version of the validator answered the second in the
vocabulary of the first — which is why no severity ever felt right for
"no data published yet" or "this optional file is absent".

Nothing here carries a severity or a policy. It is descriptive, which is
what lets the validator, a future status report and the readme generator
all share it.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field

#: Artifacts a finished publication is expected to have.
REQUIRED_ARTIFACTS = ("raw_vcf", "raw_vcf_index", "bams", "qc", "callable_sites")

#: Artifacts that are legitimately optional on GenomeArk today. Recorded,
#: never judged: `filtered.vcf.gz` is a default GATK output some snpArcher
#: versions omitted, and the READMEs are not yet mandatory.
OPTIONAL_ARTIFACTS = ("filtered_vcf", "published_readme", "repo_readme")


class PublicationState(enum.Enum):
    #: Nothing published for this species. A normal, expected state.
    ABSENT = "absent"
    #: Some required artifacts published, some not.
    PARTIAL = "partial"
    #: Every required artifact present.
    COMPLETE = "complete"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class UploadStatus:
    subject: str
    state: PublicationState
    #: The accession the data was actually found under, which may be the
    #: GCA/GCF counterpart of what the config declares.
    accession: str | None = None
    declared_accession: str | None = None
    present: frozenset[str] = field(default_factory=frozenset)
    missing: tuple[str, ...] = ()
    n_sheet_samples: int = 0
    n_bams: int = 0
    n_vcf_samples: int | None = None

    @property
    def accession_differs(self) -> bool:
        """True when the data sits under a different accession than declared."""
        return bool(
            self.accession and self.declared_accession and self.accession != self.declared_accession
        )

    @property
    def is_complete(self) -> bool:
        return self.state is PublicationState.COMPLETE

    def has(self, artifact: str) -> bool:
        return artifact in self.present

    def as_dict(self) -> dict:
        return {
            "subject": self.subject,
            "state": self.state.value,
            "accession": self.accession,
            "declared_accession": self.declared_accession,
            "accession_differs": self.accession_differs,
            "present": sorted(self.present),
            "missing": list(self.missing),
            "counts": {
                "sheet_samples": self.n_sheet_samples,
                "bams": self.n_bams,
                "vcf_samples": self.n_vcf_samples,
            },
        }


def build_upload_status(
    *,
    subject: str,
    declared_accession: str | None,
    resolved_accession: str | None,
    inventory=None,
    sheet_sample_count: int = 0,
    vcf_sample_count: int | None = None,
    repo_readme: bool = False,
) -> UploadStatus:
    """Describe one species' publication state.

    Takes primitives rather than a tool's context object, so any tool can
    call it.
    """
    if inventory is None or not getattr(inventory, "exists", False):
        return UploadStatus(
            subject=subject,
            state=PublicationState.ABSENT,
            accession=resolved_accession,
            declared_accession=declared_accession,
            present=frozenset({"repo_readme"} if repo_readme else set()),
            missing=REQUIRED_ARTIFACTS,
            n_sheet_samples=sheet_sample_count,
        )

    present: set[str] = set()
    if inventory.object("vcfs", "raw.vcf.gz"):
        present.add("raw_vcf")
    if inventory.object("vcfs", "raw.vcf.gz.tbi"):
        present.add("raw_vcf_index")
    if inventory.bam_objects:
        present.add("bams")
    if "qc" in inventory.subdirs:
        present.add("qc")
    if "callable_sites" in inventory.subdirs:
        present.add("callable_sites")
    if "filtered.vcf.gz" in inventory.vcf_names:
        present.add("filtered_vcf")
    if inventory.object("README.txt"):
        present.add("published_readme")
    if repo_readme:
        present.add("repo_readme")

    missing = tuple(name for name in REQUIRED_ARTIFACTS if name not in present)
    state = PublicationState.COMPLETE if not missing else PublicationState.PARTIAL

    return UploadStatus(
        subject=subject,
        state=state,
        accession=resolved_accession,
        declared_accession=declared_accession,
        present=frozenset(present),
        missing=missing,
        n_sheet_samples=sheet_sample_count,
        n_bams=len(inventory.bam_objects),
        n_vcf_samples=vcf_sample_count,
    )
