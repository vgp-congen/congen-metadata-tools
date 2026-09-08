"""Tests for the tolerant metadata loaders.

Every tolerance case here is drawn from something that actually exists in
congen-metadata, not from imagination.
"""

from __future__ import annotations

from congen.core.metadata.loaders import load_config, load_readme, load_sample_sheet


def codes(issues) -> set[str]:
    return {i.code for i in issues}


class TestSampleSheet:
    def test_clean_sheet(self, metadata_root):
        sheet = load_sample_sheet(
            metadata_root / "species/reptiles/podarcis-raffonei/sample_sheet.csv"
        )
        assert sheet.issues == []
        assert len(sheet.rows) == 21
        assert len(sheet.unique_sample_ids) == 21
        assert sheet.columns == ["sample_id", "input_type", "input"]
        assert sheet.line_terminator == "\n"

    def test_blank_trailing_row_is_skipped_not_counted(self, metadata_root):
        sheet = load_sample_sheet(
            metadata_root / "species/birds/anser-albifrons/sample_sheet.csv"
        )
        assert "blank_row" in codes(sheet.issues)
        assert all(row.sample_id for row in sheet.rows)
        assert len(sheet.rows) == 12

    def test_repeated_sample_id_is_not_an_issue(self, metadata_root):
        """42 of 79 sheets repeat a sample_id: multiple runs per biosample."""
        sheet = load_sample_sheet(
            metadata_root / "species/birds/anser-albifrons/sample_sheet.csv"
        )
        assert len(sheet.rows) == 12
        assert len(sheet.unique_sample_ids) == 10
        assert "duplicate_run" not in codes(sheet.issues)
        assert sheet.runs_by_sample["SAMEA104378305"] == [
            "ERR2193519",
            "ERR2193520",
            "ERR2193521",
        ]

    def test_crlf_is_detected_and_stripped(self, metadata_root):
        sheet = load_sample_sheet(
            metadata_root / "species/birds/grus-americana/sample_sheet.csv"
        )
        assert sheet.line_terminator == "\r\n"
        assert all("\r" not in row.input for row in sheet.rows)
        assert all("\r" not in column for column in sheet.columns)

    def test_missing_file(self, tmp_path):
        sheet = load_sample_sheet(tmp_path / "nope.csv")
        assert codes(sheet.issues) == {"missing_file"}
        assert sheet.rows == []

    def test_missing_required_column(self, tmp_path):
        path = tmp_path / "s.csv"
        path.write_text("sample_id,input\nS1,SRR1\n")
        sheet = load_sample_sheet(path)
        assert "missing_column" in codes(sheet.issues)

    def test_empty_sample_id_is_reported_and_row_dropped(self, tmp_path):
        path = tmp_path / "s.csv"
        path.write_text("sample_id,input_type,input\n,srr,SRR1\nS2,srr,SRR2\n")
        sheet = load_sample_sheet(path)
        assert "empty_sample_id" in codes(sheet.issues)
        assert [r.sample_id for r in sheet.rows] == ["S2"]

    def test_unknown_input_type(self, tmp_path):
        path = tmp_path / "s.csv"
        path.write_text("sample_id,input_type,input\nS1,cram,x.cram\n")
        sheet = load_sample_sheet(path)
        assert "unknown_input_type" in codes(sheet.issues)
        # The row is still kept: a tool decides whether it is fatal.
        assert len(sheet.rows) == 1

    def test_duplicate_run_pair_is_reported(self, tmp_path):
        path = tmp_path / "s.csv"
        path.write_text("sample_id,input_type,input\nS1,srr,SRR1\nS1,srr,SRR1\n")
        sheet = load_sample_sheet(path)
        assert "duplicate_run" in codes(sheet.issues)

    def test_extra_columns_are_preserved(self, tmp_path):
        path = tmp_path / "s.csv"
        path.write_text("sample_id,input_type,input,library_id,note\nS1,srr,SRR1,LB1,hello\n")
        sheet = load_sample_sheet(path)
        assert sheet.rows[0].library_id == "LB1"
        assert sheet.rows[0].extra == {"note": "hello"}

    def test_utf8_bom_is_stripped(self, tmp_path):
        path = tmp_path / "s.csv"
        path.write_bytes("sample_id,input_type,input\nS1,srr,SRR1\n".encode("utf-8-sig"))
        sheet = load_sample_sheet(path)
        assert sheet.columns[0] == "sample_id"
        assert sheet.issues == []

    def test_local_path_input_is_flagged_by_the_model(self, tmp_path):
        path = tmp_path / "s.csv"
        path.write_text("sample_id,input_type,input\nS1,fastq,/n/scratch/a_1.fq.gz\n")
        row = load_sample_sheet(path).rows[0]
        assert row.is_local_path
        assert not row.is_run_accession

    def test_line_numbers_point_at_the_file(self, tmp_path):
        path = tmp_path / "s.csv"
        path.write_text("sample_id,input_type,input\nS1,srr,SRR1\nS2,srr,SRR2\n")
        sheet = load_sample_sheet(path)
        assert [r.line for r in sheet.rows] == [2, 3]


class TestConfig:
    def test_clean_config(self, metadata_root):
        config = load_config(metadata_root / "species/reptiles/podarcis-raffonei/config.yaml")
        assert config.issues == []
        assert config.reference.name == "Podarcis_raffonei"
        assert config.reference.source == "GCA_027172205.1"
        assert config.reference.is_accession
        assert config.ploidy == 2
        assert config.het_prior == 0.005
        assert config.caller == "gatk"

    def test_structural_outlier_is_loaded_with_issues(self, metadata_root):
        """sturnus-vulgaris has an accession in reference.name and no intervals."""
        config = load_config(metadata_root / "species/birds/sturnus-vulgaris/config.yaml")
        assert config.reference.source == "GCF_001447265.1"
        assert config.reference.name == "GCF_001447265.1"
        assert config.get("intervals") is None
        assert config.get("reads") is not None

    def test_nested_get_tolerates_missing_levels(self, metadata_root):
        config = load_config(metadata_root / "species/reptiles/podarcis-raffonei/config.yaml")
        assert config.get("nope", "deeper", default="fallback") == "fallback"
        assert config.get("reference", "name", "too", "deep") is None

    def test_missing_file(self, tmp_path):
        config = load_config(tmp_path / "nope.yaml")
        assert codes(config.issues) == {"missing_file"}
        assert config.reference.source is None

    def test_unparseable_yaml_is_reported_not_raised(self, tmp_path):
        path = tmp_path / "c.yaml"
        path.write_text("reference:\n  name: [unclosed\n")
        config = load_config(path)
        assert codes(config.issues) == {"parse_error"}

    def test_missing_reference_block(self, tmp_path):
        path = tmp_path / "c.yaml"
        path.write_text("samples: x\nvariant_calling: {}\n")
        config = load_config(path)
        assert "missing_key" in codes(config.issues)
        assert config.reference.source is None

    def test_non_mapping_top_level(self, tmp_path):
        path = tmp_path / "c.yaml"
        path.write_text("- just\n- a\n- list\n")
        config = load_config(path)
        assert codes(config.issues) == {"parse_error"}


class TestReadme:
    def test_parses_species_accession_and_bioprojects(self, metadata_root):
        readme = load_readme(metadata_root / "species/reptiles/podarcis-raffonei/README.txt")
        assert readme is not None
        assert readme.species == "Podarcis_raffonei"
        assert readme.accession == "GCA_027172205.1"
        assert readme.bioprojects == ["PRJNA1089471", "PRJNA715201"]

    def test_absent_readme_is_none_not_an_error(self, metadata_root):
        assert load_readme(metadata_root / "species/birds/sturnus-vulgaris/README.txt") is None

    def test_bioprojects_are_deduplicated_in_order(self, tmp_path):
        path = tmp_path / "README.txt"
        path.write_text("PRJNA2\nPRJNA1\nPRJNA2\n")
        readme = load_readme(path)
        assert readme is not None
        assert readme.bioprojects == ["PRJNA2", "PRJNA1"]
