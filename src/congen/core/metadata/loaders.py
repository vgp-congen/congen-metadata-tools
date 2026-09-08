"""Tolerant readers for the congen-metadata repository.

Every loader is deliberately forgiving and *never* raises on bad content:
it returns a populated model whose ``issues`` list describes what was
wrong. Tools decide what a problem means; loaders only report structure.

The tolerance here is not hypothetical. Across the 79 species currently in
the repo: 24 sheets use CRLF, one has a blank trailing row, two carry an
extra ``library_id`` column, one input is a local scratch path, and one
config has a nonstandard top-level key and is missing another.
"""

from __future__ import annotations

import csv
import io
import re
from pathlib import Path

from congen.core.metadata.models import (
    INPUT_TYPES,
    LoadIssue,
    ReadmeInfo,
    ReferenceSpec,
    SampleRow,
    SampleSheet,
    SpeciesConfig,
)

REQUIRED_SHEET_COLUMNS = ("sample_id", "input_type", "input")
BIOPROJECT_RE = re.compile(r"\bPRJ[A-Z]{2}\d+\b")


def _read_text(path: Path) -> tuple[str | None, LoadIssue | None]:
    """Read a file as text, tolerating a UTF-8 BOM."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None, LoadIssue("missing_file", f"{path.name} not found", path)
    except OSError as exc:
        return None, LoadIssue("unreadable", f"cannot read {path.name}: {exc}", path)
    return raw.decode("utf-8-sig", "replace"), None


def _detect_terminator(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def load_sample_sheet(path: Path) -> SampleSheet:
    """Load ``sample_sheet.csv`` from disk."""
    text, issue = _read_text(path)
    if text is None:
        return SampleSheet(path=path, columns=[], rows=[], issues=[issue] if issue else [])
    return parse_sample_sheet(text, path)


def parse_sample_sheet_bytes(body: bytes, path: Path) -> SampleSheet:
    """Parse sample-sheet bytes, e.g. the copy published on GenomeArk.

    ``path`` is only used to label issues, so it can name a remote object.
    """
    return parse_sample_sheet(body.decode("utf-8-sig", "replace"), path)


def parse_sample_sheet(text: str, path: Path) -> SampleSheet:
    """Parse sample-sheet text.

    Blank rows are skipped rather than treated as samples, and unknown
    extra columns are preserved in ``SampleRow.extra`` so a writer can
    round-trip them.
    """
    terminator = _detect_terminator(text)
    reader = csv.reader(io.StringIO(text.replace("\r\n", "\n")))
    issues: list[LoadIssue] = []

    try:
        header = next(reader)
    except StopIteration:
        return SampleSheet(
            path=path,
            columns=[],
            rows=[],
            issues=[LoadIssue("parse_error", "file is empty", path, 1)],
            line_terminator=terminator,
        )

    columns = [c.strip() for c in header]
    index = {name: i for i, name in enumerate(columns)}
    for name in REQUIRED_SHEET_COLUMNS:
        if name not in index:
            issues.append(
                LoadIssue("missing_column", f"required column {name!r} is missing", path, 1)
            )

    rows: list[SampleRow] = []
    seen_pairs: set[tuple[str, str]] = set()

    for offset, fields in enumerate(reader, start=2):
        if not fields or all(not f.strip() for f in fields):
            issues.append(LoadIssue("blank_row", "blank row skipped", path, offset))
            continue
        if len(fields) < len(REQUIRED_SHEET_COLUMNS):
            issues.append(
                LoadIssue(
                    "short_row",
                    f"row has {len(fields)} field(s), expected at least "
                    f"{len(REQUIRED_SHEET_COLUMNS)}",
                    path,
                    offset,
                )
            )
            continue

        def cell(name: str) -> str:
            pos = index.get(name)
            return fields[pos].strip() if pos is not None and pos < len(fields) else ""

        sample_id = cell("sample_id")
        input_type = cell("input_type")
        input_value = cell("input")

        if not sample_id:
            issues.append(LoadIssue("empty_sample_id", "sample_id is empty", path, offset))
            continue
        if input_type and input_type not in INPUT_TYPES:
            issues.append(
                LoadIssue(
                    "unknown_input_type",
                    f"input_type {input_type!r} is not one of "
                    f"{sorted(INPUT_TYPES)}",
                    path,
                    offset,
                )
            )

        pair = (sample_id, input_value)
        if pair in seen_pairs:
            issues.append(
                LoadIssue(
                    "duplicate_run",
                    f"({sample_id}, {input_value}) appears more than once",
                    path,
                    offset,
                )
            )
        seen_pairs.add(pair)

        known = set(REQUIRED_SHEET_COLUMNS) | {"library_id"}
        extra = {
            name: fields[pos].strip()
            for name, pos in index.items()
            if name not in known and pos < len(fields) and fields[pos].strip()
        }

        rows.append(
            SampleRow(
                sample_id=sample_id,
                input_type=input_type,
                input=input_value,
                library_id=cell("library_id") or None,
                extra=extra,
                line=offset,
            )
        )

    return SampleSheet(
        path=path,
        columns=columns,
        rows=rows,
        issues=issues,
        line_terminator=terminator,
    )


def load_config(path: Path) -> SpeciesConfig:
    """Load ``config.yaml`` in round-trip mode so comments survive a rewrite."""
    from congen.core.metadata.writers import load_yaml_roundtrip

    text, issue = _read_text(path)
    if text is None:
        return SpeciesConfig(
            path=path,
            data={},
            reference=ReferenceSpec(None, None),
            issues=[issue] if issue else [],
        )

    issues: list[LoadIssue] = []
    try:
        data = load_yaml_roundtrip(text)
    except Exception as exc:  # noqa: BLE001 - any YAML failure is reportable
        return SpeciesConfig(
            path=path,
            data={},
            reference=ReferenceSpec(None, None),
            issues=[LoadIssue("parse_error", f"YAML did not parse: {exc}", path)],
        )

    if not isinstance(data, dict):
        return SpeciesConfig(
            path=path,
            data={},
            reference=ReferenceSpec(None, None),
            issues=[LoadIssue("parse_error", "top level of config is not a mapping", path)],
        )

    ref = data.get("reference")
    if not isinstance(ref, dict):
        issues.append(LoadIssue("missing_key", "config has no reference: block", path))
        reference = ReferenceSpec(None, None)
    else:
        name = ref.get("name")
        source = ref.get("source")
        reference = ReferenceSpec(
            name=str(name).strip() if name is not None else None,
            source=str(source).strip() if source is not None else None,
        )
        for key in ("name", "source"):
            if ref.get(key) is None:
                issues.append(
                    LoadIssue("missing_key", f"reference.{key} is missing or empty", path)
                )

    for key in ("samples", "variant_calling"):
        if key not in data:
            issues.append(LoadIssue("missing_key", f"config has no {key}: key", path))

    return SpeciesConfig(path=path, data=data, reference=reference, issues=issues)


def load_readme(path: Path) -> ReadmeInfo | None:
    """Parse the minimal ``README.txt`` format.

    Returns ``None`` when the file is absent, which is a normal state — 22
    of 79 species have no README — so it is the caller's business whether
    that matters.
    """
    if not path.exists():
        return None

    text, issue = _read_text(path)
    if text is None:
        return ReadmeInfo(path=path, text="", issues=[issue] if issue else [])

    species = accession = None
    for line in text.splitlines():
        lowered = line.lower()
        if lowered.startswith("species:"):
            species = line.split(":", 1)[1].strip() or None
        elif lowered.startswith("accession number:"):
            accession = line.split(":", 1)[1].strip() or None

    bioprojects: list[str] = []
    for match in BIOPROJECT_RE.finditer(text):
        if match.group(0) not in bioprojects:
            bioprojects.append(match.group(0))

    return ReadmeInfo(
        path=path,
        text=text,
        species=species,
        accession=accession,
        bioprojects=bioprojects,
    )
