"""Tests for the two document gates.

Synthetic records rather than the real corpus: what needs proving is that
every branch is reachable and correct, and the corpus counts legitimately
move whenever a pipeline run finishes. The corpus is checked separately,
for invariants, in `test_gate_corpus.py`.
"""

from __future__ import annotations

import pytest

from congen.core.validation_record import ReportState, ValidationRecord
from congen.tools.readme.gate import (
    GATING_CHECKS,
    WRONGNESS_CHECKS,
    Blocker,
    CheckOutcome,
    Mode,
    Provenance,
    evaluate,
    gating_states,
)

ALL_CHECKS = list(GATING_CHECKS) + ["S001", "F020"]


def record(
    *,
    state=ReportState.PASS,
    publication="complete",
    findings=(),
    checks_run=None,
    inputs=None,
    accession="GCA_000000001.1",
    missing=(),
    stale_reason=None,
) -> ValidationRecord:
    return ValidationRecord(
        subject="birds/test-species",
        state=state,
        validated_at="2026-09-10T00:00:00Z",
        tool_version="test",
        catalog="sha256:deadbeef",
        checks_run=list(ALL_CHECKS if checks_run is None else checks_run),
        checks_available=len(ALL_CHECKS),
        inputs=dict(inputs or {}),
        data={"accession": accession, "published": True},
        findings=list(findings),
        status={
            "state": publication,
            "accession": accession,
            "missing": list(missing),
            "counts": {},
        },
        stale_reason=stale_reason,
    )


def finding(check, severity, message="something"):
    return {"id": check, "severity": severity, "message": message}


@pytest.fixture
def species_dir(tmp_path):
    for name in ("config.yaml", "sample_sheet.csv", "README.txt"):
        (tmp_path / name).write_text(f"contents of {name}\n")
    return tmp_path


class TestGateA:
    def test_a_complete_error_free_species_renders_fully(self, species_dir):
        verdict = evaluate(species_dir, record())
        assert verdict.mode is Mode.FULL
        assert verdict.is_full
        assert verdict.blockers == ()

    def test_no_record_at_all_truncates(self, species_dir):
        verdict = evaluate(species_dir, None)
        assert verdict.mode is Mode.TRUNCATED
        assert verdict.has(Blocker.NO_RECORD)
        # and with no record there is nothing to say about provenance
        assert verdict.gating == ()

    def test_absent_publication_truncates(self, species_dir):
        verdict = evaluate(species_dir, record(publication="absent"))
        assert verdict.mode is Mode.TRUNCATED
        assert verdict.has(Blocker.NO_DATA)

    def test_partial_publication_truncates_and_names_what_is_missing(self, species_dir):
        verdict = evaluate(
            species_dir, record(publication="partial", missing=("raw_vcf",))
        )
        assert verdict.has(Blocker.INCOMPLETE_UPLOAD)
        assert "raw_vcf" in str(verdict.blockers[0])

    def test_an_error_finding_truncates_and_names_the_ids(self, species_dir):
        verdict = evaluate(
            species_dir,
            record(findings=[finding("S001", "error"), finding("S002", "error")]),
        )
        assert verdict.has(Blocker.VALIDATION_ERRORS)
        assert "S001, S002" in str(verdict.blockers[0])

    def test_warnings_alone_do_not_truncate(self, species_dir):
        """A warning is metadata hygiene, not a reason to withhold the data."""
        verdict = evaluate(species_dir, record(findings=[finding("R003", "warn")]))
        assert verdict.is_full

    def test_a_stale_record_truncates(self, species_dir):
        verdict = evaluate(
            species_dir, record(state=ReportState.STALE, stale_reason="sheet changed")
        )
        assert verdict.has(Blocker.STALE_RECORD)
        assert "sheet changed" in str(verdict.blockers[0])

    def test_changed_inputs_truncate_even_when_the_verdict_says_pass(self, species_dir):
        """The mode is derived from this record, so a stale one is dangerous.

        Without this check a full document — download links, statistics —
        would render for a species that would now fail.
        """
        stale = record(inputs={"sample_sheet.csv": "sha256:not-the-current-content"})
        verdict = evaluate(species_dir, stale)
        assert verdict.mode is Mode.TRUNCATED
        assert verdict.has(Blocker.INPUTS_CHANGED)
        assert "sample_sheet.csv" in str(verdict.blockers[0])

    def test_matching_input_digests_do_not_truncate(self, species_dir):
        from congen.core.validation_record import input_digests

        verdict = evaluate(species_dir, record(inputs=input_digests(species_dir)))
        assert verdict.is_full

    def test_a_moved_accession_truncates(self, species_dir):
        """Never describe one accession while linking another."""
        verdict = evaluate(
            species_dir,
            record(accession="GCA_111111111.1"),
            harvested_accession="GCA_222222222.2",
        )
        assert verdict.has(Blocker.ACCESSION_MOVED)

    def test_an_agreeing_accession_does_not_truncate(self, species_dir):
        verdict = evaluate(
            species_dir,
            record(accession="GCA_111111111.1"),
            harvested_accession="GCA_111111111.1",
        )
        assert verdict.is_full

    def test_every_blocking_reason_is_listed_not_just_the_first(self, species_dir):
        """sturnus-vulgaris trips two at once."""
        verdict = evaluate(
            species_dir,
            record(publication="absent", findings=[finding("F020", "error")]),
        )
        codes = {reason.code for reason in verdict.blockers}
        assert codes == {Blocker.NO_DATA, Blocker.VALIDATION_ERRORS}


class TestGateB:
    def test_all_gating_checks_passing_permits_citations(self, species_dir):
        verdict = evaluate(species_dir, record())
        assert verdict.cited
        assert verdict.provenance is None
        assert verdict.unsound == ()

    def test_g017_blocks_and_reads_as_missing_not_wrong(self, species_dir):
        verdict = evaluate(species_dir, record(findings=[finding("G017", "warn")]))
        assert verdict.is_full
        assert not verdict.cited
        assert verdict.provenance is Provenance.MISSING
        assert verdict.fired == ("G017",)

    def test_e002_firing_reads_as_wrong(self, species_dir):
        verdict = evaluate(species_dir, record(findings=[finding("E002", "warn")]))
        assert verdict.provenance is Provenance.WRONG

    def test_a_skipped_wrongness_check_is_not_wrongness(self, species_dir):
        """The 14-species case, and the reason `provenance` inspects outcomes.

        A species with no README.txt fires G017 and *skips* R020, E002 and
        E003 as a consequence. Calling that "the recorded list is
        incorrect" would assert something nobody established.
        """
        verdict = evaluate(
            species_dir,
            record(
                findings=[
                    finding("G017", "warn"),
                    finding("R020", "skipped"),
                    finding("E002", "skipped"),
                    finding("E003", "skipped"),
                ]
            ),
        )
        assert not verdict.cited
        assert verdict.provenance is Provenance.MISSING
        assert verdict.fired == ("G017",)

    def test_a_skipped_check_alone_still_blocks(self, species_dir):
        """"We could not check" is not "we checked"."""
        verdict = evaluate(species_dir, record(findings=[finding("R020", "skipped")]))
        assert not verdict.cited
        assert verdict.provenance is Provenance.MISSING

    def test_tier_five_not_run_blocks_citations(self, species_dir):
        """The trap: validate without --check-sra leaves E001-E003 unrun.

        Under a naive "no provenance finding fired" test this species
        would gain unverified citations.
        """
        without_tier5 = [c for c in ALL_CHECKS if not c.startswith("E")]
        verdict = evaluate(species_dir, record(checks_run=without_tier5))
        assert verdict.is_full
        assert not verdict.cited
        assert verdict.provenance is Provenance.MISSING
        outcomes = {s.check: s.outcome for s in verdict.gating}
        assert outcomes["E001"] is CheckOutcome.NOT_RUN
        assert verdict.fired == ()

    def test_gate_b_does_not_affect_the_mode(self, species_dir):
        """Provenance blocks one block, never the document."""
        verdict = evaluate(species_dir, record(findings=[finding("G018", "warn")]))
        assert verdict.mode is Mode.FULL

    def test_a_fired_check_carries_its_message(self, species_dir):
        verdict = evaluate(
            species_dir,
            record(findings=[finding("E002", "warn", "runs also from PRJNA1462765")]),
        )
        state = next(s for s in verdict.gating if s.check == "E002")
        assert "PRJNA1462765" in state.message


class TestGatingStates:
    def test_every_gating_check_gets_an_outcome(self):
        states = gating_states(record())
        assert tuple(s.check for s in states) == GATING_CHECKS

    def test_outcomes_distinguish_all_four_cases(self):
        states = {
            s.check: s.outcome
            for s in gating_states(
                record(
                    checks_run=["G017", "G018", "R020"],
                    findings=[finding("G017", "warn"), finding("G018", "skipped")],
                )
            )
        }
        assert states["G017"] is CheckOutcome.FIRED
        assert states["G018"] is CheckOutcome.SKIPPED
        assert states["R020"] is CheckOutcome.PASSED
        assert states["E001"] is CheckOutcome.NOT_RUN

    def test_only_passed_counts_as_sound(self):
        assert CheckOutcome.PASSED.is_sound
        for outcome in (CheckOutcome.FIRED, CheckOutcome.SKIPPED, CheckOutcome.NOT_RUN):
            assert not outcome.is_sound


def test_the_gating_list_is_pinned():
    """Public API: retire, don't renumber.

    A renumbered ID would silently stop gating, so a change here must be
    deliberate enough to update this test.
    """
    assert GATING_CHECKS == ("G017", "G018", "R020", "E001", "E002", "E003")
    assert WRONGNESS_CHECKS == {"E001", "E002", "E003"}
    assert WRONGNESS_CHECKS <= set(GATING_CHECKS)
