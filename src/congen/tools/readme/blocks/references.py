"""The references block — how do I cite this, and whom do I credit?

**Phase 3 renders only the placeholder.** The bioproject table, the
curated citations file and the two blocked renderings arrive in phase 5,
once `congen citations` exists.

The placeholder is deliberately not the gate-B wording. For a species
whose provenance is sound, the list is not broken — the tool simply does
not do citations yet, and borrowing the blocked wording would state a
false reason. And it is not silence either: these documents are
committed for review, so a reader finding no References section must not
conclude that citations were dropped by design.
"""

from __future__ import annotations

from ..gate import Provenance
from ..format import alert, sentence_list
from . import Context, Rendered

HEADING = "## References"

NOT_BUILT = (
    "*Not generated yet.* This section will list the BioProjects that "
    "contributed reads to this dataset, with a citation for each. Until then, "
    "the contributing BioProjects are recorded in "
    "[`README.txt`](README.txt) — please cite them when you use this dataset."
)

BLOCKED = {
    Provenance.MISSING: [
        "**This dataset cannot yet be cited.**",
        "",
        "No reviewed list of contributing BioProjects exists in this "
        "repository, so there is nothing here to credit the people who "
        "generated the reads. Do not publish analyses of this dataset until "
        "that list exists.",
    ],
    Provenance.WRONG: [
        "**This dataset cannot yet be cited.**",
        "",
        "The recorded list of contributing BioProjects is known to be "
        "incorrect, so it is not reproduced here — a wrong citation list is "
        "worse than none, because it propagates into other people's papers. "
        "Do not publish analyses of this dataset until it is corrected.",
    ],
}


def render(context: Context) -> Rendered:
    verdict = context.verdict
    if verdict.cited:
        return Rendered([HEADING, "", NOT_BUILT])

    body = list(BLOCKED[verdict.provenance])
    fired = sentence_list([f"`{check}`" for check in verdict.fired])
    if fired:
        means = "these mean" if len(verdict.fired) > 1 else "that means"
        body += [
            "",
            f"Reported by {fired} — see [`VALIDATION.md`](VALIDATION.md) for "
            f"what {means} and how to fix it.",
        ]
    kind = "WARNING" if verdict.provenance is Provenance.MISSING else "CAUTION"
    return Rendered([HEADING, "", *alert(kind, body)])
