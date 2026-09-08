"""Writers for the congen-metadata repository.

Read-only tools never import this module. The upcoming ``congen readme``
tool does, which is why it exists now rather than later.

Two properties matter for anything that writes into the metadata repo:

* **Comments survive.** The configs are heavily commented, so YAML is
  loaded and dumped in ruamel round-trip mode.
* **Output is deterministic.** Regenerating a file from unchanged inputs
  must produce identical bytes, or CI sees a diff on every run. Hence
  ``would_change`` and no timestamps anywhere.
"""

from __future__ import annotations

import csv
import io
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from ruamel.yaml import YAML

BLOCK_BEGIN = "<!-- congen:begin {name} -->"
BLOCK_END = "<!-- congen:end {name} -->"


def _yaml() -> YAML:
    yaml = YAML()
    yaml.preserve_quotes = True
    # snpArcher configs use two-space mapping indent and four-space
    # sequence indent with a two-space offset; matching that keeps diffs
    # limited to the values actually changed.
    yaml.indent(mapping=2, sequence=4, offset=2)
    yaml.width = 4096  # never re-wrap long comment lines
    return yaml


def load_yaml_roundtrip(text: str) -> Any:
    """Parse YAML preserving comments, quoting and key order."""
    return _yaml().load(text)


def dump_yaml_roundtrip(data: Any) -> str:
    stream = io.StringIO()
    _yaml().dump(data, stream)
    return stream.getvalue()


#: A ``key: <bool>`` line, capturing the key prefix, the boolean token and
#: any trailing comment.
_BOOL_LINE_RE = re.compile(
    r"^(?P<prefix>\s*[\w.\-]+\s*:\s*)"
    r"(?P<value>true|false|True|False|TRUE|FALSE)"
    r"(?P<suffix>\s*(?:#.*)?)$"
)


def _bool_casing_map(text: str) -> dict[tuple[str, bool], set[str]]:
    """Map ``(key prefix, value)`` to the spellings used in ``text``."""
    out: dict[tuple[str, bool], set[str]] = {}
    for line in text.splitlines():
        match = _BOOL_LINE_RE.match(line)
        if match:
            key = (match.group("prefix"), match.group("value").lower() == "true")
            out.setdefault(key, set()).add(match.group("value"))
    return out


def restore_bool_casing(original: str, dumped: str) -> str:
    """Undo ruamel's normalization of YAML boolean spelling.

    ruamel round-trips comments, key order and quoting, but always emits
    booleans lowercase — and these configs mix styles within one file
    (``generate_bed_file: True`` beside ``enabled: true``). Left alone,
    changing one value would produce a diff on every boolean line in the
    file, burying the real change.

    ``True`` and ``true`` are the same YAML scalar, so this is cosmetic by
    definition and safe. It is also conservative: a spelling is restored
    only where every occurrence of that key at that indent agreed in the
    original, so an already-inconsistent key keeps ruamel's form.
    """
    casings = _bool_casing_map(original)
    if not casings:
        return dumped

    lines = dumped.splitlines(keepends=True)
    for position, line in enumerate(lines):
        stripped = line.rstrip("\r\n")
        match = _BOOL_LINE_RE.match(stripped)
        if not match:
            continue
        spellings = casings.get((match.group("prefix"), match.group("value").lower() == "true"))
        if not spellings or len(spellings) != 1:
            continue
        original_spelling = next(iter(spellings))
        if original_spelling == match.group("value"):
            continue
        ending = line[len(stripped):]
        lines[position] = (
            f"{match.group('prefix')}{original_spelling}{match.group('suffix')}{ending}"
        )
    return "".join(lines)


def dump_yaml_preserving(original: str, data: Any) -> str:
    """Dump ``data``, keeping the boolean spelling ``original`` used.

    The pairing to use whenever an existing config is rewritten, so the
    diff shows only what actually changed.
    """
    return restore_bool_casing(original, dump_yaml_roundtrip(data))


def atomic_write(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """Write via a temporary file in the same directory, then rename.

    An interrupted run leaves the previous file intact rather than a
    half-written one.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def would_change(path: Path, text: str, *, encoding: str = "utf-8") -> bool:
    """True if writing ``text`` to ``path`` would alter it.

    This is what makes a generator usable in CI: ``--check`` regenerates
    and fails if anything differs, without touching the tree.
    """
    try:
        return path.read_text(encoding=encoding) != text
    except FileNotFoundError:
        return True


def write_if_changed(path: Path, text: str, *, encoding: str = "utf-8") -> bool:
    """Write only when the content differs. Returns whether it wrote."""
    if not would_change(path, text, encoding=encoding):
        return False
    atomic_write(path, text, encoding=encoding)
    return True


def render_csv(
    columns: Iterable[str],
    rows: Iterable[Mapping[str, Any]],
    *,
    line_terminator: str = "\n",
) -> str:
    """Render a CSV, preserving a file's original line terminator."""
    columns = list(columns)
    stream = io.StringIO()
    writer = csv.DictWriter(
        stream, fieldnames=columns, lineterminator=line_terminator, extrasaction="ignore"
    )
    writer.writeheader()
    for row in rows:
        writer.writerow({c: row.get(c, "") for c in columns})
    return stream.getvalue()


@dataclass(frozen=True)
class ManagedBlock:
    name: str
    content: str


def _block_pattern(name: str) -> re.Pattern[str]:
    return re.compile(
        re.escape(BLOCK_BEGIN.format(name=name))
        + r"\n?.*?"
        + re.escape(BLOCK_END.format(name=name)),
        re.DOTALL,
    )


def render_managed_blocks(existing: str, blocks: Iterable[ManagedBlock]) -> str:
    """Replace the content of each managed block, leaving all else alone.

    Lets a generated file carry hand-written prose: only the region
    between ``<!-- congen:begin NAME -->`` and its matching end marker is
    ever rewritten. Blocks not already present are appended.

    Retrofitting this after hand edits exist is painful, so it ships with
    the first writer rather than being added when someone complains.
    """
    text = existing
    for block in blocks:
        begin = BLOCK_BEGIN.format(name=block.name)
        end = BLOCK_END.format(name=block.name)
        body = block.content.strip("\n")
        replacement = f"{begin}\n{body}\n{end}" if body else f"{begin}\n{end}"
        pattern = _block_pattern(block.name)
        if pattern.search(text):
            text = pattern.sub(lambda _m, r=replacement: r, text, count=1)
        else:
            separator = "" if not text or text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
            text = f"{text}{separator}{replacement}\n"
    return text


def managed_block_names(text: str) -> list[str]:
    """Names of managed blocks present in ``text``, in order."""
    pattern = re.compile(re.escape(BLOCK_BEGIN.split("{")[0]) + r"\s*([\w.-]+)\s*-->")
    return pattern.findall(text)
