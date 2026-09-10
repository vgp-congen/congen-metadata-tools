"""The gates over a real congen-metadata checkout.

Opt-in (`pytest -m corpus`) and skipped when no checkout is reachable,
because the default suite must run offline against fixtures.

**Invariants, not counts.** An earlier plan asserted the exact
53/17/9 split as a regression target, and that is the wrong shape for a
test: those numbers move legitimately every time a snpArcher run
finishes. Between 2026-09-09 and 2026-09-10 five species gained data and
the split went 53/13/13 to 53/17/9 with nothing wrong. What must not
change silently is the *logic*, so that is what this asserts. The counts
belong in the design doc's baseline, and in `readme --gate-report`.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from congen.core.metadata.discovery import MetadataRootNotFound, SpeciesRepo
from congen.core.validation_record import load_record
from congen.tools.readme.gate import Mode, Provenance, evaluate
from congen.tools.readme.record import RECORD_JSON
from congen.tools.readme.record import load_record as load_dataset

pytestmark = pytest.mark.corpus


@pytest.fixture(scope="module")
def verdicts():
    root = os.environ.get("CONGEN_METADATA_ROOT")
    try:
        repo = SpeciesRepo(Path(root)) if root else SpeciesRepo.discover()
    except (MetadataRootNotFound, FileNotFoundError):
        pytest.skip("no congen-metadata checkout reachable")
    out = []
    for species in repo.iter_species():
        dataset = load_dataset(species.path / RECORD_JSON)
        out.append(
            (
                species.key,
                evaluate(
                    species.path,
                    load_record(species.path / "validation.json"),
                    harvested_accession=dataset.accession if dataset else None,
                ),
            )
        )
    if not out:
        pytest.skip("checkout has no species")
    return out


def test_every_species_gets_a_verdict(verdicts):
    assert all(v.mode in (Mode.FULL, Mode.TRUNCATED) for _, v in verdicts)


def test_a_truncated_species_always_says_why(verdicts):
    """A truncated document with no stated reason is a bug, not a verdict."""
    silent = [key for key, v in verdicts if not v.is_full and not v.blockers]
    assert silent == []


def test_a_full_species_has_no_blockers(verdicts):
    assert [key for key, v in verdicts if v.is_full and v.blockers] == []


def test_blocked_citations_always_have_something_unsound(verdicts):
    bad = [key for key, v in verdicts if not v.cited and not v.unsound]
    assert bad == []


def test_provenance_is_set_exactly_when_citations_are_blocked(verdicts):
    for key, v in verdicts:
        assert (v.provenance is None) is v.cited, key


def test_wrongness_is_claimed_only_when_a_wrongness_check_fired(verdicts):
    """Never say a bioproject list is incorrect on the strength of a skip."""
    from congen.tools.readme.gate import CheckOutcome, WRONGNESS_CHECKS

    for key, v in verdicts:
        if v.provenance is Provenance.WRONG:
            assert any(
                s.check in WRONGNESS_CHECKS and s.outcome is CheckOutcome.FIRED
                for s in v.gating
            ), key


def test_tier_five_ran_everywhere(verdicts):
    """If this fails, the last --write-reports run omitted --check-sra.

    Not a code defect — an operational one, and the reason gate B asks
    whether a check ran rather than only whether it complained.
    """
    from congen.tools.readme.gate import CheckOutcome

    unrun = [
        key
        for key, v in verdicts
        if any(s.outcome is CheckOutcome.NOT_RUN for s in v.gating)
    ]
    assert unrun == []


def test_full_splits_into_cited_and_blocked(verdicts):
    full = [v for _, v in verdicts if v.is_full]
    assert len(full) == sum(1 for v in full if v.cited) + sum(
        1 for v in full if not v.cited
    )
