"""Tests for the VGP reference-genome list."""

from __future__ import annotations

from congen.core.metadata.vgp import binomial_slug, load_vgp_list, slugify

REL = "references/vgp_reference_genomes.csv"


class TestSlugs:
    def test_plain_binomial(self):
        assert slugify("Podarcis raffonei") == "podarcis-raffonei"

    def test_trinomial_keeps_all_parts(self):
        assert slugify("Sus scrofa domesticus") == "sus-scrofa-domesticus"

    def test_punctuation_is_dropped(self):
        assert slugify("Rhamphochromis sp. 'chilingali'") == "rhamphochromis-sp-chilingali"
        assert binomial_slug("Rhamphochromis sp. 'chilingali'") == "rhamphochromis-sp"

    def test_subspecies_is_dropped_by_binomial(self):
        assert binomial_slug("Fringilla coelebs palmae") == "fringilla-coelebs"
        assert binomial_slug("Aquila chrysaetos chrysaetos") == "aquila-chrysaetos"

    def test_curly_apostrophe(self):
        assert slugify("Rhamphochromis sp. ’x’") == "rhamphochromis-sp-x"


class TestLoad:
    def test_loads_and_normalizes_r_style_header(self, metadata_root):
        vgp = load_vgp_list(metadata_root / REL)
        assert vgp.issues == []
        assert len(vgp) == 7
        entry = vgp.by_accession("GCA_027172205.1")
        assert entry is not None
        assert entry.scientific_name == "Podarcis raffonei"

    def test_qid_column_is_an_ncbi_taxid(self, metadata_root):
        """The column is named QID but holds NCBI taxonomy IDs."""
        vgp = load_vgp_list(metadata_root / REL)
        assert vgp.by_accession("GCA_027172205.1").ncbi_taxid == 65483
        assert vgp.by_taxid(9117).scientific_name == "Grus americana"

    def test_trailing_blank_row_is_tolerated_silently(self, metadata_root):
        vgp = load_vgp_list(metadata_root / REL)
        assert vgp.issues == []
        assert all(e.scientific_name for e in vgp.entries)

    def test_missing_file(self, tmp_path):
        vgp = load_vgp_list(tmp_path / "nope.csv")
        assert {i.code for i in vgp.issues} == {"missing_file"}
        assert len(vgp) == 0

    def test_missing_required_column(self, tmp_path):
        path = tmp_path / "v.csv"
        path.write_text("ScientificName,QID\nX y,1\n")
        vgp = load_vgp_list(path)
        assert "missing_column" in {i.code for i in vgp.issues}

    def test_non_integer_taxid_is_reported_but_row_kept(self, tmp_path):
        path = tmp_path / "v.csv"
        path.write_text("ScientificName,QID,Accession.for.main.haplotype\nX y,abc,GCA_1.1\n")
        vgp = load_vgp_list(path)
        assert "bad_taxid" in {i.code for i in vgp.issues}
        assert vgp.by_accession("GCA_1.1").ncbi_taxid is None

    def test_duplicate_accession_is_reported(self, tmp_path):
        path = tmp_path / "v.csv"
        path.write_text(
            "ScientificName,QID,Accession.for.main.haplotype\n"
            "A b,1,GCA_1.1\nC d,2,GCA_1.1\n"
        )
        vgp = load_vgp_list(path)
        assert "duplicate_accession" in {i.code for i in vgp.issues}


class TestLookupBySlug:
    def test_exact_slug(self, metadata_root):
        vgp = load_vgp_list(metadata_root / REL)
        assert vgp.accession_for_slug("podarcis-raffonei") == "GCA_027172205.1"

    def test_unknown_slug(self, metadata_root):
        vgp = load_vgp_list(metadata_root / REL)
        assert vgp.accession_for_slug("nonexistent-species") is None
        assert vgp.candidates_for_slug("nonexistent-species") == []

    def test_binomial_fallback(self, tmp_path):
        path = tmp_path / "v.csv"
        path.write_text(
            "ScientificName,QID,Accession.for.main.haplotype\n"
            "Fringilla coelebs palmae,37598,GCA_963513975.1\n"
        )
        vgp = load_vgp_list(path)
        assert vgp.accession_for_slug("fringilla-coelebs") == "GCA_963513975.1"

    def test_ambiguous_binomial_returns_none_but_lists_candidates(self, tmp_path):
        path = tmp_path / "v.csv"
        path.write_text(
            "ScientificName,QID,Accession.for.main.haplotype\n"
            "Anser anser one,1,GCA_1.1\nAnser anser two,2,GCA_2.1\n"
        )
        vgp = load_vgp_list(path)
        assert vgp.by_slug("anser-anser") is None
        assert len(vgp.candidates_for_slug("anser-anser")) == 2


class TestAgainstTheRealList:
    """The committed list must actually resolve every species in the repo."""

    def test_every_repo_species_resolves_unambiguously(self):
        from congen.core.metadata import SpeciesRepo
        from congen.core.metadata.discovery import MetadataRootNotFound

        try:
            repo = SpeciesRepo.discover()
        except MetadataRootNotFound:
            import pytest

            pytest.skip("no congen-metadata checkout alongside this repo")

        vgp = repo.vgp_list
        assert vgp.issues == []
        unresolved = [
            directory.name
            for directory in repo.species_dirs()
            if vgp.by_slug(directory.name) is None
        ]
        assert unresolved == []
