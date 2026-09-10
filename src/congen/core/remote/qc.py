"""Readers for snpArcher's QC and callable-sites tables.

These are plain tab-separated files produced by a pipeline nobody here
controls, so every reader is tolerant in the same way the metadata
loaders are: a short row is padded, a blank line is skipped, and an
unparseable number is omitted rather than raising.

Values stay strings until a caller asks for them as numbers. That is
deliberate and matches `core.metadata.models`: these are dumb containers,
and deciding what a missing or malformed value *means* is the consumer's
job, not the reader's.
"""

from __future__ import annotations

from dataclasses import dataclass, field

CONTIG_MAP_FILE = "contig_map.tsv"

#: ``qc/qc_report.tsv`` — the per-sample summary the QC dashboard plots.
#: Its ``mean_depth`` is the depth this tool reports: it is measured over
#: mapped reads, and it is what the dashboard shows. Note that
#: ``individuals.idepth`` also has a mean depth, measured over called
#: sites, and the two disagree by a few × per sample.
QC_REPORT_FILE = "qc_report.tsv"

#: vcftools per-individual tables, all keyed on ``INDV``.
IDEPTH_FILE = "individuals.idepth"
HET_FILE = "individuals.het"
IMISS_FILE = "individuals.imiss"

#: ``callable_sites/coverage_thresholds.tsv``
COVERAGE_THRESHOLDS_FILE = "coverage_thresholds.tsv"

#: The column holding the sample identifier, by file. snpArcher's own
#: table says ``sample``; the vcftools ones say ``INDV``.
SAMPLE_KEY = {
    QC_REPORT_FILE: "sample",
    IDEPTH_FILE: "INDV",
    HET_FILE: "INDV",
    IMISS_FILE: "INDV",
}


def parse_tsv(text: str) -> tuple[list[str], list[dict[str, str]]]:
    """Parse a headed TSV into column names and row dicts.

    Tolerant in the same way the metadata loaders are: blank lines are
    skipped and short rows are padded rather than raising, because these
    files are produced by a pipeline nobody here controls.
    """
    lines = [line for line in text.replace("\r\n", "\n").split("\n") if line.strip()]
    if not lines:
        return [], []
    columns = [c.strip() for c in lines[0].split("\t")]
    rows: list[dict[str, str]] = []
    for line in lines[1:]:
        fields = [f.strip() for f in line.split("\t")]
        rows.append({name: fields[i] if i < len(fields) else "" for i, name in enumerate(columns)})
    return columns, rows


@dataclass
class ContigMap:
    """``qc/contig_map.tsv`` — the plink contig renaming table."""

    columns: list[str] = field(default_factory=list)
    rows: list[dict[str, str]] = field(default_factory=list)

    @property
    def original_contigs(self) -> list[str]:
        return [row["original_contig"] for row in self.rows if row.get("original_contig")]

    @property
    def plink_contigs(self) -> list[str]:
        return [row["plink_contig"] for row in self.rows if row.get("plink_contig")]

    def as_pairs(self) -> dict[str, str]:
        return {
            row["original_contig"]: row.get("plink_contig", "")
            for row in self.rows
            if row.get("original_contig")
        }


def parse_contig_map(text: str) -> ContigMap:
    columns, rows = parse_tsv(text)
    return ContigMap(columns=columns, rows=rows)


@dataclass
class SampleTable:
    """A headed TSV keyed by one sample-identifier column.

    One class serves ``qc_report.tsv``, ``individuals.idepth``,
    ``individuals.het`` and ``individuals.imiss``, because they differ
    only in their key column and their value columns. Four near-identical
    dataclasses would carry no information the column names do not.
    """

    key: str
    columns: list[str] = field(default_factory=list)
    rows: dict[str, dict[str, str]] = field(default_factory=dict)
    #: Rows whose key column was empty, in file order. Kept rather than
    #: dropped silently: a table that cannot name its samples is a
    #: problem a consumer may want to report.
    unkeyed: list[dict[str, str]] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.rows)

    @property
    def samples(self) -> list[str]:
        return list(self.rows)

    def has(self, column: str) -> bool:
        return column in self.columns

    def floats(self, column: str) -> dict[str, float]:
        """Sample to value, omitting anything that will not parse.

        Omission rather than an exception or a NaN: the caller can see
        that ``len(floats(col)) < len(table)`` and decide what to do,
        whereas a NaN silently poisons a median.
        """
        out: dict[str, float] = {}
        for sample, row in self.rows.items():
            try:
                out[sample] = float(row[column])
            except (KeyError, TypeError, ValueError):
                continue
        return out

    def ints(self, column: str) -> dict[str, int]:
        out: dict[str, int] = {}
        for sample, value in self.floats(column).items():
            if value.is_integer():
                out[sample] = int(value)
        return out


def parse_sample_table(text: str, key: str) -> SampleTable:
    columns, rows = parse_tsv(text)
    table = SampleTable(key=key, columns=columns)
    for row in rows:
        sample = row.get(key, "")
        if not sample:
            table.unkeyed.append(row)
            continue
        table.rows[sample] = row
    return table


def parse_qc_report(text: str) -> SampleTable:
    return parse_sample_table(text, SAMPLE_KEY[QC_REPORT_FILE])


def parse_indv_table(text: str) -> SampleTable:
    """``individuals.idepth`` / ``.het`` / ``.imiss`` — all keyed on INDV."""
    return parse_sample_table(text, "INDV")


@dataclass(frozen=True)
class CoverageThresholds:
    """``callable_sites/coverage_thresholds.tsv`` — a single data row.

    ``min_coverage`` and ``max_coverage`` are **site-level** bounds used
    to build the callable-sites mask, derived from the cohort mean. They
    are not per-sample QC cutoffs, and comparing a sample's mean depth
    against them is a category error — measured across 14 species it
    would "fail" 17.8% of samples. See `readme-design.md`.
    """

    cohort_mean_coverage: float | None = None
    min_coverage: float | None = None
    max_coverage: float | None = None


def parse_coverage_thresholds(text: str) -> CoverageThresholds:
    _, rows = parse_tsv(text)
    if not rows:
        return CoverageThresholds()

    def number(name: str) -> float | None:
        try:
            return float(rows[0][name])
        except (KeyError, TypeError, ValueError):
            return None

    return CoverageThresholds(
        cohort_mean_coverage=number("cohort_mean_coverage"),
        min_coverage=number("min_coverage"),
        max_coverage=number("max_coverage"),
    )
