"""CLI tests. Offline: the gatherer is stubbed so no network is touched."""

from __future__ import annotations

import json
import shutil

import pytest
from click.testing import CliRunner

from congen.core.cli import main
from congen.core.tools import Tool, discover_tools
from congen.tools.validate import tool as validate_tool
from congen.tools.validate.cli import validate
from tests.conftest import METADATA_ROOT


@pytest.fixture
def offline(monkeypatch):
    """Make context gathering local-only: no S3, NCBI or header reads."""
    from congen.tools.validate import runner

    original = runner.ContextGatherer.gather

    def gather(self, species, required, *, vgp_list=None):
        # Only the slices that need no network.
        return original(self, species, {"config", "sheet", "readme", "vgp"}, vgp_list=vgp_list)

    monkeypatch.setattr(runner.ContextGatherer, "gather", gather)
    # --all also runs the corpus-level orphan checks, which list the
    # bucket. Stub them too, or this fixture is not actually offline.
    monkeypatch.setattr("congen.tools.validate.cli.orphan_findings", lambda repo, gatherer: [])


def run_cli(args, **kwargs):
    return CliRunner().invoke(main, args, **kwargs)


class TestDispatcher:
    def test_validate_is_registered_as_a_subcommand(self):
        result = run_cli(["--help"])
        assert result.exit_code == 0
        assert "validate" in result.output

    def test_list_tools_shows_metadata_write_scope(self):
        """Write scope is per tool, so assert per line, not over the output.

        `writes_metadata` exists to distinguish tools, so once a second
        tool writes, a whole-output assertion measures the wrong thing.
        """
        result = run_cli(["list-tools"])
        assert result.exit_code == 0
        lines = {line.split()[0]: line for line in result.output.splitlines() if line.strip()}
        assert "network" in lines["validate"]
        # The validator is read-only, so its line must not advertise writing.
        assert "writes metadata" not in lines["validate"]
        # The readme generator legitimately does.
        assert "writes metadata" in lines["readme"]

    def test_the_validate_tool_declares_itself_read_only(self):
        assert validate_tool.writes_metadata is False
        assert validate_tool.needs_network is True

    def test_discovery_finds_the_tool_via_entry_points(self):
        names = [t.name for t in discover_tools()]
        assert "validate" in names

    def test_a_write_capable_tool_is_labelled(self, monkeypatch):
        writer = Tool(
            name="writer", summary="writes things", writes_metadata=True, needs_network=False
        )
        monkeypatch.setattr("congen.core.cli.discover_tools", lambda: [writer])
        result = run_cli(["list-tools"])
        assert "writes metadata" in result.output


class TestValidateCli:
    def test_list_checks_needs_no_target(self):
        result = CliRunner().invoke(validate, ["--list-checks"])
        assert result.exit_code == 0
        assert "S001" in result.output
        assert "needs:" in result.output

    def test_a_target_is_required(self):
        result = CliRunner().invoke(validate, [])
        assert result.exit_code == 2
        assert "--all" in result.output

    def test_unknown_species_is_a_clean_error(self):
        result = CliRunner().invoke(
            validate, ["--metadata-root", str(METADATA_ROOT), "mammals/nope"]
        )
        assert result.exit_code == 1
        assert "no species directory matches" in result.output

    def test_a_selection_matching_nothing_is_refused(self, offline):
        """Otherwise a typo'd --only would make CI pass vacuously."""
        result = CliRunner().invoke(
            validate,
            ["--metadata-root", str(METADATA_ROOT), "--all", "--only", "NONSENSE"],
        )
        assert result.exit_code == 2
        assert "selected no checks" in result.output

    def test_a_root_with_no_species_is_misuse_not_a_vacuous_pass(self, offline, tmp_path):
        """Otherwise a CI job with a wrong --metadata-root passes silently.

        The stray tree is part of the defect, not a detail: --write-reports
        pointed at a root that holds no species used to create it, which is
        what made the mistake look like a successful run.
        """
        missing = tmp_path / "congen-metadata"
        result = CliRunner().invoke(
            validate,
            ["--metadata-root", str(missing), "--all", "--write-reports"],
        )
        assert result.exit_code == 2
        assert "no species found" in result.output
        assert str((missing / "species").resolve()) in result.output
        assert not missing.exists()

    def test_a_clade_matching_nothing_is_refused(self, offline):
        result = CliRunner().invoke(
            validate,
            ["--metadata-root", str(METADATA_ROOT), "--all", "--clade", "sponges"],
        )
        assert result.exit_code == 2
        assert "no species found" in result.output
        assert "'sponges'" in result.output

    def test_stale_selecting_nothing_is_success_over_a_populated_repo(
        self, offline, monkeypatch
    ):
        """An empty --stale selection is good news, but only here.

        Nothing pending means every report is current — a claim that needs
        reports to exist, hence the pairing with the test below.
        """
        monkeypatch.setattr(
            "congen.tools.validate.cli.stale_species", lambda *a, **k: []
        )
        result = CliRunner().invoke(
            validate, ["--metadata-root", str(METADATA_ROOT), "--stale"]
        )
        assert result.exit_code == 0
        assert "nothing to revalidate" in result.output

    def test_stale_selecting_nothing_over_an_empty_root_is_misuse(
        self, offline, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(
            "congen.tools.validate.cli.stale_species", lambda *a, **k: []
        )
        result = CliRunner().invoke(validate, ["--metadata-root", str(tmp_path), "--stale"])
        assert result.exit_code == 2
        assert "no species found" in result.output

    @pytest.mark.parametrize("mode", ["--check-stale", "--mark-stale"])
    def test_the_offline_modes_refuse_an_empty_root_too(self, mode, tmp_path):
        """They report on reports, but the CI danger is identical.

        `--check-stale` used to say every report described its inputs, and
        `--mark-stale` to stamp nothing, both exiting 0 over a root holding
        no reports at all.
        """
        result = CliRunner().invoke(validate, ["--metadata-root", str(tmp_path), mode])
        assert result.exit_code == 2
        assert "no species found" in result.output

    @pytest.mark.parametrize("mode", ["--check-stale", "--mark-stale"])
    def test_the_offline_modes_still_run_over_a_populated_root(self, mode, tmp_path):
        """The guard must not have made the offline modes unusable.

        Over a copy, because --mark-stale writes.
        """
        root = tmp_path / "metadata"
        shutil.copytree(METADATA_ROOT, root)
        result = CliRunner().invoke(validate, ["--metadata-root", str(root), mode])
        assert result.exit_code in (0, 1)
        assert "no species found" not in result.output

    def test_clean_species_exits_zero(self, offline):
        result = CliRunner().invoke(
            validate,
            [
                "--metadata-root",
                str(METADATA_ROOT),
                "reptiles/podarcis-raffonei",
                "--only",
                "R",
            ],
        )
        assert result.exit_code == 0
        assert "ok" in result.output

    def test_errors_exit_one(self, offline):
        result = CliRunner().invoke(
            validate,
            ["--metadata-root", str(METADATA_ROOT), "birds/sturnus-vulgaris", "--only", "F020"],
        )
        assert result.exit_code == 1
        assert "F020" in result.output

    def test_strict_promotes_warnings(self, offline):
        args = [
            "--metadata-root",
            str(METADATA_ROOT),
            "birds/sturnus-vulgaris",
            "--only",
            "R003",
        ]
        assert CliRunner().invoke(validate, args).exit_code == 0
        assert CliRunner().invoke(validate, [*args, "--strict"]).exit_code == 1

    def test_json_output_is_written(self, offline, tmp_path):
        target = tmp_path / "report.json"
        result = CliRunner().invoke(
            validate,
            [
                "--metadata-root",
                str(METADATA_ROOT),
                "--all",
                "--only",
                "R",
                "--json",
                str(target),
            ],
        )
        assert result.exit_code in (0, 1)
        payload = json.loads(target.read_text())
        assert len(payload["subjects"]) == 4
        assert payload["checks_run"]
        assert "counts" in payload

    def test_github_mode_emits_annotations(self, offline):
        result = CliRunner().invoke(
            validate,
            [
                "--metadata-root",
                str(METADATA_ROOT),
                "birds/sturnus-vulgaris",
                "--only",
                "F020",
                "--github",
            ],
        )
        assert result.output.startswith("::error ")

    def test_clade_filter(self, offline, tmp_path):
        target = tmp_path / "r.json"
        CliRunner().invoke(
            validate,
            [
                "--metadata-root",
                str(METADATA_ROOT),
                "--all",
                "--clade",
                "reptiles",
                "--only",
                "R",
                "--json",
                str(target),
            ],
        )
        assert json.loads(target.read_text())["subjects"] == ["reptiles/podarcis-raffonei"]

    def test_quiet_reaches_the_renderer(self, offline, monkeypatch):
        """The renderer's own behaviour is covered in test_report.py.

        No offline check emits INFO any more — the ones that can (R017,
        F009) need the network — so this asserts the wiring instead.
        """
        seen: list[bool] = []

        def spy(report, **kwargs):
            seen.append(kwargs.get("show_info"))
            return ""

        monkeypatch.setattr("congen.tools.validate.cli.render_human", spy)
        args = ["--metadata-root", str(METADATA_ROOT), "reptiles/podarcis-raffonei"]
        CliRunner().invoke(validate, args)
        CliRunner().invoke(validate, [*args, "--quiet"])
        assert seen == [True, False]


class TestOrphanFindings:
    """Corpus-level checks: published data with no species directory."""

    def _gatherer(self, accessions):
        from congen.core.cache import Cache
        from congen.tools.validate.context import ContextGatherer

        gatherer = ContextGatherer(cache=Cache(enabled=False))
        gatherer.genomeark.accessions = lambda: accessions  # type: ignore[method-assign]
        return gatherer

    def test_a_listed_species_with_no_directory_is_g020(self):
        from congen.core.metadata.discovery import SpeciesRepo
        from congen.tools.validate.runner import orphan_findings

        repo = SpeciesRepo(METADATA_ROOT)
        findings = orphan_findings(repo, self._gatherer(["GCA_028023285.1"]))
        assert [(f.id, f.subject) for f in findings] == [("G020", "<corpus>")]
        assert "Balaenoptera ricei" in findings[0].message

    def test_an_unlisted_accession_is_g021(self):
        from congen.core.metadata.discovery import SpeciesRepo
        from congen.tools.validate.runner import orphan_findings

        repo = SpeciesRepo(METADATA_ROOT)
        findings = orphan_findings(repo, self._gatherer(["GCA_999999999.1"]))
        assert [f.id for f in findings] == ["G021"]

    def test_a_claimed_accession_is_not_an_orphan(self):
        from congen.core.metadata.discovery import SpeciesRepo
        from congen.tools.validate.runner import orphan_findings

        repo = SpeciesRepo(METADATA_ROOT)
        assert orphan_findings(repo, self._gatherer(["GCA_027172205.1"])) == []

    def test_the_counterpart_of_a_claimed_accession_is_not_an_orphan(self):
        """grus-americana declares GCF; the data sits under GCA."""
        from congen.core.metadata.discovery import SpeciesRepo
        from congen.tools.validate.runner import orphan_findings

        repo = SpeciesRepo(METADATA_ROOT)
        assert orphan_findings(repo, self._gatherer(["GCA_028858705.1"])) == []


class TestReadmeCli:
    """`congen readme` — phase 1 ships --refresh only."""

    def test_a_bare_invocation_says_rendering_is_not_built(self):
        result = run_cli(["readme", "birds/apteryx-mantelli"])
        assert result.exit_code == 2
        assert "--refresh" in result.output

    def test_selecting_nothing_is_misuse_not_success(self):
        result = run_cli(["readme", "--refresh"])
        assert result.exit_code == 2

    def test_a_root_with_no_species_is_misuse_not_a_vacuous_pass(self, tmp_path):
        """Otherwise a CI job with a wrong --metadata-root passes silently."""
        result = run_cli(
            ["readme", "--refresh", "--all", "--metadata-root", str(tmp_path)]
        )
        assert result.exit_code == 2
        assert "no species found" in result.output

    def test_the_readme_tool_declares_that_it_writes(self):
        from congen.tools.readme import tool as readme_tool

        assert readme_tool.writes_metadata is True
        assert readme_tool.needs_network is True
