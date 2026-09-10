"""Tests for the snpArcher QC and callable-sites readers.

Fixtures are the real tables from `GCA_027172205.1` (podarcis-raffonei),
the same accession the header and listing fixtures come from.

The tolerance cases matter more than the happy paths here: these files
are pipeline output, so a reader that raises on one bad row takes the
whole document down with it.
"""

from __future__ import annotations

import pytest

from congen.core.remote.qc import (
    COVERAGE_THRESHOLDS_FILE,
    HET_FILE,
    IDEPTH_FILE,
    IMISS_FILE,
    QC_REPORT_FILE,
    SAMPLE_KEY,
    CoverageThresholds,
    parse_coverage_thresholds,
    parse_indv_table,
    parse_qc_report,
    parse_sample_table,
)

from tests.conftest import REMOTE


def fixture(name: str) -> str:
    return (REMOTE / f"podarcis-raffonei.{name}").read_text()


@pytest.fixture
def qc_report():
    return parse_qc_report(fixture(QC_REPORT_FILE))


@pytest.fixture
def het():
    return parse_indv_table(fixture(HET_FILE))


@pytest.fixture
def imiss():
    return parse_indv_table(fixture(IMISS_FILE))


@pytest.fixture
def idepth():
    return parse_indv_table(fixture(IDEPTH_FILE))


class TestQcReport:
    def test_it_keys_on_the_sample_column(self, qc_report):
        assert qc_report.key == "sample"
        assert len(qc_report) == 21
        assert "SAMN18355762" in qc_report.rows

    def test_mean_depth_parses_for_every_sample(self, qc_report):
        depths = qc_report.floats("mean_depth")
        assert len(depths) == len(qc_report)
        assert depths["SAMN18355762"] == pytest.approx(15.99)

    def test_integer_columns_come_back_as_ints(self, qc_report):
        assert qc_report.ints("total_reads")["SAMN18355762"] == 217418871

    def test_a_fractional_value_is_not_offered_as_an_int(self, qc_report):
        """`ints` must not silently truncate. 15.99 is not 15."""
        assert "SAMN18355762" not in qc_report.ints("mean_depth")

    def test_an_absent_column_is_empty_not_an_error(self, qc_report):
        assert qc_report.floats("no_such_column") == {}
        assert not qc_report.has("no_such_column")


class TestIndvTables:
    """The three vcftools tables differ only in their value columns."""

    def test_all_three_key_on_indv(self, het, imiss, idepth):
        for table in (het, imiss, idepth):
            assert table.key == "INDV"
            assert len(table) == 21

    def test_het_carries_the_inbreeding_coefficient(self, het):
        assert len(het.floats("F")) == 21

    def test_imiss_carries_missingness_and_the_site_count(self, imiss):
        assert len(imiss.floats("F_MISS")) == 21
        # N_DATA is the cohort variant-site count, and the readme's only
        # source for it.
        counts = set(imiss.ints("N_DATA").values())
        assert len(counts) == 1
        assert counts.pop() == 9439207

    def test_idepth_depth_differs_from_the_qc_report(self, idepth, qc_report):
        """Two 'mean depths' exist and they are not the same measurement.

        `qc_report.tsv` measures over mapped reads, `individuals.idepth`
        over called sites. The readme uses the former; this test exists so
        nobody later "fixes" the discrepancy by switching one for the other.
        """
        over_sites = idepth.floats("MEAN_DEPTH")
        over_reads = qc_report.floats("mean_depth")
        assert set(over_sites) == set(over_reads)
        assert over_sites["SAMN18355762"] != over_reads["SAMN18355762"]


class TestTolerance:
    def test_an_empty_file_yields_an_empty_table(self):
        table = parse_sample_table("", key="sample")
        assert len(table) == 0
        assert table.columns == []

    def test_a_header_with_no_rows_keeps_its_columns(self):
        table = parse_sample_table("sample\tmean_depth\n", key="sample")
        assert table.columns == ["sample", "mean_depth"]
        assert len(table) == 0

    def test_a_short_row_is_padded_rather_than_raising(self):
        table = parse_sample_table("sample\ta\tb\nS1\t1\n", key="sample")
        assert table.rows["S1"] == {"sample": "S1", "a": "1", "b": ""}
        assert table.floats("b") == {}

    def test_an_unparseable_number_is_omitted_not_nan(self):
        """A NaN would silently poison a median; an omission is visible."""
        table = parse_sample_table("sample\td\nS1\t1.5\nS2\tNA\n", key="sample")
        assert table.floats("d") == {"S1": 1.5}
        assert len(table) == 2

    def test_a_row_with_no_sample_id_is_kept_separately(self):
        table = parse_sample_table("sample\td\n\t1.5\nS1\t2.0\n", key="sample")
        assert table.samples == ["S1"]
        assert len(table.unkeyed) == 1

    def test_crlf_line_endings_parse(self):
        table = parse_sample_table("sample\td\r\nS1\t1.5\r\n", key="sample")
        assert table.floats("d") == {"S1": 1.5}

    def test_a_repeated_sample_keeps_the_last_row(self):
        table = parse_sample_table("sample\td\nS1\t1.0\nS1\t2.0\n", key="sample")
        assert table.floats("d") == {"S1": 2.0}


class TestCoverageThresholds:
    def test_it_reads_the_single_data_row(self):
        thresholds = parse_coverage_thresholds(fixture(COVERAGE_THRESHOLDS_FILE))
        assert thresholds.cohort_mean_coverage == pytest.approx(17.390952)
        assert thresholds.min_coverage == 8
        assert thresholds.max_coverage == 35

    def test_an_empty_file_yields_all_none(self):
        assert parse_coverage_thresholds("") == CoverageThresholds()

    def test_a_header_with_no_data_row_yields_all_none(self):
        text = "cohort_mean_coverage\tmin_coverage\tmax_coverage\n"
        assert parse_coverage_thresholds(text) == CoverageThresholds()

    def test_a_missing_column_is_none_not_an_error(self):
        thresholds = parse_coverage_thresholds("min_coverage\n8\n")
        assert thresholds.min_coverage == 8
        assert thresholds.cohort_mean_coverage is None


def test_sample_key_covers_every_per_sample_table():
    """The key map is the reason one reader can serve all four tables."""
    assert set(SAMPLE_KEY) == {QC_REPORT_FILE, IDEPTH_FILE, HET_FILE, IMISS_FILE}
