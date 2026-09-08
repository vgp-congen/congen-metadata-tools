"""Tests for locating and enumerating species."""

from __future__ import annotations

import pytest

from congen.core.metadata import SpeciesRepo
from congen.core.metadata.discovery import (
    ENV_ROOT,
    MetadataRootNotFound,
    find_metadata_root,
)


class TestFindRoot:
    def test_env_var_wins(self, metadata_root, monkeypatch, tmp_path):
        monkeypatch.setenv(ENV_ROOT, str(metadata_root))
        assert find_metadata_root(tmp_path) == metadata_root.resolve()

    def test_env_var_pointing_somewhere_useless_is_an_error(self, monkeypatch, tmp_path):
        monkeypatch.setenv(ENV_ROOT, str(tmp_path))
        with pytest.raises(MetadataRootNotFound, match="species/"):
            find_metadata_root(tmp_path)

    def test_finds_the_directory_itself(self, metadata_root, monkeypatch):
        monkeypatch.delenv(ENV_ROOT, raising=False)
        assert find_metadata_root(metadata_root) == metadata_root.resolve()

    def test_finds_a_congen_metadata_sibling(self, monkeypatch, tmp_path):
        """The normal local layout: both repos cloned side by side."""
        monkeypatch.delenv(ENV_ROOT, raising=False)
        (tmp_path / "congen-metadata" / "species" / "birds").mkdir(parents=True)
        (tmp_path / "congen-metadata-tools").mkdir()
        found = find_metadata_root(tmp_path / "congen-metadata-tools")
        assert found == (tmp_path / "congen-metadata").resolve()

    def test_raises_when_nothing_is_found(self, monkeypatch, tmp_path):
        monkeypatch.delenv(ENV_ROOT, raising=False)
        # tmp_path is under /private/var on macOS, far from any checkout.
        with pytest.raises(MetadataRootNotFound):
            find_metadata_root(tmp_path)


class TestSpeciesRepo:
    def test_clades_and_species(self, metadata_root):
        repo = SpeciesRepo(metadata_root)
        assert repo.clades == ["birds", "reptiles"]
        assert len(repo.species_dirs()) == 4
        assert len(repo.species_dirs(clade="reptiles")) == 1

    def test_species_dirs_are_sorted_by_clade_then_slug(self, metadata_root):
        keys = [f"{p.parent.name}/{p.name}" for p in SpeciesRepo(metadata_root).species_dirs()]
        assert keys == sorted(keys)

    @pytest.mark.parametrize("target", [
        "reptiles/podarcis-raffonei",
        "species/reptiles/podarcis-raffonei",
        "podarcis-raffonei",
        "reptiles/podarcis-raffonei/",
    ])
    def test_resolve_accepts_several_target_forms(self, metadata_root, target):
        repo = SpeciesRepo(metadata_root)
        assert repo.resolve(target).name == "podarcis-raffonei"

    def test_resolve_accepts_a_filesystem_path(self, metadata_root):
        repo = SpeciesRepo(metadata_root)
        path = metadata_root / "species/reptiles/podarcis-raffonei"
        assert repo.resolve(path) == path.resolve()

    def test_resolve_rejects_the_unknown(self, metadata_root):
        with pytest.raises(ValueError, match="no species directory matches"):
            SpeciesRepo(metadata_root).resolve("mammals/nope")

    def test_load_assembles_the_whole_species(self, metadata_root):
        species = SpeciesRepo(metadata_root).load("reptiles/podarcis-raffonei")
        assert species.key == "reptiles/podarcis-raffonei"
        assert species.clade == "reptiles"
        assert species.reference.source == "GCA_027172205.1"
        assert len(species.sheet.unique_sample_ids) == 21
        assert species.readme is not None
        assert species.issues == []

    def test_species_without_a_readme_loads_fine(self, metadata_root):
        species = SpeciesRepo(metadata_root).load("birds/sturnus-vulgaris")
        assert species.readme is None

    def test_iter_species_covers_every_directory(self, metadata_root):
        repo = SpeciesRepo(metadata_root)
        keys = sorted(s.key for s in repo.iter_species())
        assert keys == [
            "birds/anser-albifrons",
            "birds/grus-americana",
            "birds/sturnus-vulgaris",
            "reptiles/podarcis-raffonei",
        ]

    def test_vgp_list_is_loaded_from_the_repo_and_cached(self, metadata_root):
        repo = SpeciesRepo(metadata_root)
        assert repo.vgp_list is repo.vgp_list
        assert len(repo.vgp_list) == 7

    def test_ambiguous_bare_slug_is_rejected(self, tmp_path):
        for clade in ("birds", "mammals"):
            directory = tmp_path / "species" / clade / "same-name"
            directory.mkdir(parents=True)
            (directory / "config.yaml").write_text("reference: {name: x, source: y}\n")
        with pytest.raises(ValueError, match="ambiguous"):
            SpeciesRepo(tmp_path).resolve("same-name")

    def test_directories_without_a_config_are_not_species(self, tmp_path):
        (tmp_path / "species" / "birds" / "notes").mkdir(parents=True)
        assert SpeciesRepo(tmp_path).species_dirs() == []
