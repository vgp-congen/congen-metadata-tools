"""The document's blocks, and the contract they share.

A block is a plain function of the context returning `Rendered`: its
markdown lines, and the reference-style link definitions it used. Only
the renderer knows where definitions go — the foot of the document — and
any block may cite any published file, so collecting them centrally is
the only arrangement that works.

Blocks have no `needs` declaration and no skip states. An earlier design
gave every block both, mirroring the validator's `SKIPPED`, and it was
the wrong shape: a document three-fifths full of apologies is worse than
one that says a single clear thing. The gates decide what gets rendered,
so a block that runs at all has its inputs.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from congen.core.metadata.citations import CitationIndex
from congen.core.metadata.models import SpeciesMetadata
from congen.core.metadata.vgp import VgpReferenceList
from congen.core.validation_record import ValidationRecord

from ..gate import Verdict
from ..record import DatasetRecord


@dataclass(frozen=True)
class Context:
    """Everything a block may read. All local; nothing here is remote."""

    species: SpeciesMetadata
    dataset: DatasetRecord
    verdict: Verdict
    record: ValidationRecord | None = None
    vgp: VgpReferenceList | None = None
    #: The curated citations, read from the two files in `references/`.
    #: Local, so rendering stays offline.
    citations: CitationIndex | None = None
    #: Object path -> reference tag, assigned once by the renderer so a
    #: block can cite a file without knowing how it will be linked.
    refs: dict[str, str] = field(default_factory=dict)

    def link(self, path: str, text: str | None = None) -> str:
        """A reference-style link to a published object, or plain text.

        Falls back to unlinked text when the object is not published, so
        a block never has to guard every citation.
        """
        label = text or f"`{path.rsplit('/', 1)[-1]}`"
        tag = self.refs.get(path)
        return f"[{label}][{tag}]" if tag else label

    def has(self, path: str) -> bool:
        return path in self.dataset.objects

    def bioprojects(self) -> dict[str, int]:
        """BioProject -> samples it contributed to *this* species.

        The union of what `README.txt` declares and what the SRA mapping
        attributes. When gate B passes those agree by construction, since
        `E002`/`E003` would otherwise block the block — so the union is
        belt-and-braces rather than a real choice. A declared project
        that contributed no runs shows zero, which is what `E003` is.
        """
        counts: dict[str, int] = {}
        for projects in self.dataset.bioprojects_by_sample(self.species.sheet).values():
            for project in projects:
                counts[project] = counts.get(project, 0) + 1
        declared = self.species.readme.bioprojects if self.species.readme else []
        for project in declared:
            counts.setdefault(project, 0)
        return counts


@dataclass(frozen=True)
class Rendered:
    lines: list[str] = field(default_factory=list)
    #: Reference tag -> URL, merged by the renderer.
    refs: dict[str, str] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.lines)


EMPTY = Rendered()
