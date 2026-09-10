"""`congen readme` at the command line.

Kept out of `test_cli.py` so the dispatcher tests and this tool's tests
do not collide in the same file.
"""

from __future__ import annotations

import json

from click.testing import CliRunner

from congen.core.cli import main
from congen.tools.readme import tool as readme_tool
from congen.tools.readme.render import OUTPUT_NAME

from tests.conftest import METADATA_ROOT


def run(args):
    return CliRunner().invoke(main, args, catch_exceptions=False)


class TestSelection:
    def test_selecting_nothing_is_misuse(self):
        assert run(["readme"]).exit_code == 2

    def test_a_root_with_no_species_is_misuse_not_a_vacuous_pass(self, tmp_path):
        """Otherwise a CI job with a wrong --metadata-root passes silently."""
        result = run(["readme", "--all", "--metadata-root", str(tmp_path)])
        assert result.exit_code == 2
        assert "no species found" in result.output

    def test_it_accepts_several_targets(self, tmp_path):
        """A PR-scoped CI job knows which paths changed, not a clade."""
        result = run(
            [
                "readme",
                "--check",
                "--metadata-root",
                str(METADATA_ROOT),
                "reptiles/podarcis-raffonei",
                "birds/grus-americana",
            ]
        )
        assert "podarcis-raffonei" in result.output
        assert "grus-americana" in result.output


class TestCheck:
    """`--check` is the only entry point CI uses, so it is not a separate
    code path: it is the renderer plus `would_change`."""

    def test_it_writes_nothing(self, tmp_path):
        root = _copy_species(tmp_path)
        run(["readme", "--check", "--all", "--metadata-root", str(root)])
        assert not list(root.rglob(OUTPUT_NAME))

    def test_a_missing_document_is_out_of_date(self, tmp_path):
        root = _copy_species(tmp_path)
        result = run(["readme", "--check", "--all", "--metadata-root", str(root)])
        assert result.exit_code == 1
        assert "out of date" in result.output

    def test_a_freshly_written_document_is_current(self, tmp_path):
        root = _copy_species(tmp_path)
        assert run(["readme", "--all", "--metadata-root", str(root)]).exit_code == 0
        result = run(["readme", "--check", "--all", "--metadata-root", str(root)])
        assert result.exit_code == 0
        assert "0 out of date" in result.output


class TestJson:
    def test_it_reports_mode_and_gate_state_per_species(self, tmp_path):
        root = _copy_species(tmp_path)
        out = tmp_path / "report.json"
        run(["readme", "--all", "--metadata-root", str(root), "--json", str(out)])
        payload = json.loads(out.read_text())
        assert payload["documents"]
        row = payload["documents"][0]
        assert set(row) >= {"subject", "mode", "cited", "provenance", "blockers", "changed"}


def test_the_tool_declares_that_it_writes():
    assert readme_tool.writes_metadata is True
    assert readme_tool.needs_network is True


def _copy_species(tmp_path):
    """A scratch metadata root, so writing tests never touch the fixtures."""
    import shutil

    root = tmp_path / "metadata"
    shutil.copytree(METADATA_ROOT, root)
    return root


class TestFetchFailures:
    """Unattended, a failed fetch must never reach disk.

    Otherwise an outage commits a record asserting the data has no QC
    tables, and nothing ever tells anyone.
    """

    def test_a_harvest_failure_exits_non_zero_and_writes_nothing(
        self, tmp_path, monkeypatch
    ):
        from congen.tools.readme.record import RECORD_JSON

        root = _copy_species(tmp_path)

        def boom(self, species):
            self.notes = []
            self.failures = ["GenomeArk listing failed: the network is on fire"]
            from congen.tools.readme.record import DatasetRecord

            return DatasetRecord(subject=species.key)

        monkeypatch.setattr(
            "congen.tools.readme.harvest.Harvester.harvest", boom, raising=True
        )
        result = run(
            ["readme", "--refresh", "--all", "--metadata-root", str(root)]
        )
        assert result.exit_code == 1
        assert "could not be harvested" in result.output
        assert not list(root.rglob(RECORD_JSON))

    def test_a_previous_good_record_survives_a_failure(self, tmp_path, monkeypatch):
        from congen.tools.readme.record import RECORD_JSON, DatasetRecord

        root = _copy_species(tmp_path)
        species = next(iter(sorted(root.glob("species/*/*"))))
        good = DatasetRecord(subject="keep/me", accession="GCA_1.1", published=True)
        (species / RECORD_JSON).write_text(good.render_json())

        def boom(self, sp):
            self.notes = []
            self.failures = ["NCBI SRA lookup failed: timeout"]
            return DatasetRecord(subject=sp.key)

        monkeypatch.setattr(
            "congen.tools.readme.harvest.Harvester.harvest", boom, raising=True
        )
        run(["readme", "--refresh", "--all", "--metadata-root", str(root)])
        assert "keep/me" in (species / RECORD_JSON).read_text()
