"""Assemble the blocks into `README.md`.

Two invariants this module exists to hold.

**Determinism.** Render touches no clock, no network, no git state and no
environment. Every date shown comes from an input — the validation
record, never `now()` — and no tool version or timestamp appears in the
body, because a rendered version string would diff all 79 documents on
every release and bury the real changes. Testable: a render with sockets
disabled must succeed.

**Idempotence.** `render(inputs, existing) -> text` is a merge, not a
pure text function, because the trailing region below the managed markers
belongs to whoever wrote it. Rendering twice must change nothing.

The generated body lives in **one** managed region. An earlier design used
one marker pair per block, which made an orphaned block possible: a
species moving from full to truncated would have kept its download links
and coverage statistics under a red banner unless the renderer actively
reconciled which markers should exist. With one pair, a mode transition is
a content replacement and an orphan is unrepresentable.
"""

from __future__ import annotations

from pathlib import Path

from congen.core.metadata.models import SpeciesMetadata
from congen.core.metadata.vgp import VgpReferenceList
from congen.core.metadata.writers import ManagedBlock, render_managed_blocks, would_change
from congen.core.remote.genomeark import BASE_URL
from congen.core.validation_record import ValidationRecord

from .blocks import Context, Rendered
from .blocks import description as description_block
from .blocks import links as links_block
from .blocks import references as references_block
from .blocks import stats as stats_block
from .blocks import validation as validation_block
from .format import italic_binomial
from .gate import Mode, Verdict
from .record import DatasetRecord

#: The generated document. One constant with a CLI override, so the name
#: stays a one-line change.
OUTPUT_NAME = "README.md"

#: The single managed region. Everything generated is inside it; anything
#: below the end marker is never touched.
BLOCK_NAME = "body"

NCBI_TAXON = "https://www.ncbi.nlm.nih.gov/Taxonomy/Browser/wwwtax.cgi?id="

#: Document order, settled in Phase 0. Getting-the-data is last: it is the
#: longest block and the least interesting to someone deciding whether
#: this is the dataset they want.
FULL_BLOCKS = (
    validation_block,
    description_block,
    stats_block,
    references_block,
    links_block,
)

#: A truncated document is the heading, the verdict, and the pointers.
TRUNCATED_BLOCKS = (validation_block,)


def build_context(
    species: SpeciesMetadata,
    dataset: DatasetRecord,
    verdict: Verdict,
    record: ValidationRecord | None,
    vgp: VgpReferenceList | None = None,
    citations=None,
) -> Context:
    """Assign a reference tag to every linkable object, once.

    The tag is the object's path rather than a serial number, so the
    definition list at the foot of the document is self-documenting and a
    diff on it says which file changed.
    """
    linkable = [path for path, _ in links_block.PRIMARY] + list(links_block.SECONDARY)
    refs = {path: path for path in linkable if path in dataset.objects}
    return Context(
        species=species,
        dataset=dataset,
        verdict=verdict,
        record=record,
        vgp=vgp,
        citations=citations,
        refs=refs,
    )


def _heading(context: Context) -> list[str]:
    binomial = italic_binomial(context.species.slug)
    assembly = context.dataset.assembly or {}
    common = assembly.get("common_name")
    taxon = assembly.get("tax_id")
    n_samples = len(context.species.sheet.unique_sample_ids)

    lead = f"Population-genomic variant calls for {n_samples} *{binomial}*"
    if common:
        lead += f" ({common})"
    lead += " samples"
    if taxon:
        lead += f" ([NCBI taxon {taxon}]({NCBI_TAXON}{taxon}))"
    if context.dataset.accession:
        lead += f", produced by snpArcher against `{context.dataset.accession}`"
    lead += " and published on GenomeArk."
    if not context.verdict.is_full:
        lead = _truncated_lead(context, binomial, n_samples)
    return [f"# *{binomial}*", "", lead]


def _truncated_lead(context: Context, binomial: str, n_samples: int) -> str:
    """Two situations, not one.

    "A planned or in-progress dataset" is right for a species awaiting
    its pipeline run and wrong for `anser-albifrons`, which has a
    complete publication and a real defect. Saying it of the second
    understates the problem and misdescribes the data.
    """
    from .blocks.validation import leading_blocker
    from .gate import Blocker

    leading = leading_blocker(context.verdict)
    code = leading.code if leading else None
    # No leading article: "A *Anser albifrons*" needs "An", and deciding
    # that from a binomial is not worth a vowel table.
    if code in (Blocker.NO_DATA, Blocker.INCOMPLETE_UPLOAD, Blocker.NO_RECORD):
        return (
            f"*{binomial}* — {n_samples} samples in the sheet, awaiting the "
            "pipeline run. See below."
        )
    return (
        f"*{binomial}* — {n_samples} samples. **This dataset cannot be used as "
        "it stands.** See below."
    )


def render_body(context: Context) -> str:
    blocks = FULL_BLOCKS if context.verdict.is_full else TRUNCATED_BLOCKS
    lines = list(_heading(context))
    refs: dict[str, str] = {}
    for block in blocks:
        rendered: Rendered = block.render(context)
        if not rendered:
            continue
        lines += ["", *rendered.lines]
        refs.update(rendered.refs)

    # Link definitions go at the foot, collected across blocks, because
    # the 100-character shared S3 prefix makes inline links unreviewable
    # in a diff.
    used = sorted(
        tag for path, tag in context.refs.items() if f"][{tag}]" in "\n".join(lines)
    )
    if used:
        lines.append("")
        prefix = context.dataset.prefix
        lines += [f"[{tag}]: {BASE_URL}/{prefix}/{tag}" for tag in used]

    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def render_document(context: Context, existing: str = "") -> str:
    """Merge the generated body into `existing`, preserving hand prose."""
    body = render_body(context)
    text = render_managed_blocks(existing, [ManagedBlock(BLOCK_NAME, body)])
    return text if text.endswith("\n") else text + "\n"


def read_existing(path: Path) -> str:
    try:
        return path.read_text("utf-8")
    except (FileNotFoundError, OSError):
        return ""


def document_for(context: Context, path: Path) -> str:
    return render_document(context, read_existing(path))


def write_document(context: Context, path: Path) -> bool:
    from congen.core.metadata.writers import write_if_changed

    return write_if_changed(path, document_for(context, path))


def would_change_document(context: Context, path: Path) -> bool:
    return would_change(path, document_for(context, path))
