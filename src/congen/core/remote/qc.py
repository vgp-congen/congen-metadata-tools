"""Readers for snpArcher's QC and callable-sites tables.

These are plain tab-separated files. Only ``contig_map.tsv`` is needed so
far, by `F008`; the per-sample coverage, heterozygosity and missingness
tables land here when `congen readme` needs them.
"""

from __future__ import annotations

from dataclasses import dataclass, field

CONTIG_MAP_FILE = "contig_map.tsv"


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
