"""Formatting shared by the blocks.

Every function here is a pure function of its arguments, with pinned
precision and no locale dependence. That is not fussiness: the document
is regenerated in CI and compared byte for byte, so a figure that
formats differently on a different machine or a different day is a
false diff on every run.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

#: Bucket edges for the per-sample depth histogram, in ×.
#:
#: A pinned ladder rather than one derived from the observed range.
#: Eight equal-width buckets over the range put 47 of `esox-lucius`'s 65
#: samples in the first bucket, because two samples above 130× stretch it
#: and destroy all resolution where the samples actually are. A fixed
#: ladder also makes species comparable, which an adaptive one cannot be
#: by construction.
DEPTH_LADDER: tuple[float, ...] = (0, 5, 10, 15, 20, 30, 40, 60, 100, float("inf"))

#: Width of the longest histogram bar, in `U+2588 FULL BLOCK` characters.
BAR_WIDTH = 30

_UNITS = (("GiB", 1 << 30), ("MiB", 1 << 20), ("KiB", 1 << 10))


def human_size(n: int) -> str:
    for unit, divisor in _UNITS:
        if n >= divisor:
            return f"{n / divisor:.1f} {unit}"
    return f"{n} B"


def thousands(n: int) -> str:
    return f"{n:,}"


def quantiles(values: Sequence[float]) -> tuple[float, float, float, float, float]:
    """min, Q1, median, Q3, max — linear interpolation, no dependencies."""
    ordered = sorted(values)
    count = len(ordered)

    def at(fraction: float) -> float:
        position = fraction * (count - 1)
        low = int(position)
        high = min(low + 1, count - 1)
        return ordered[low] + (ordered[high] - ordered[low]) * (position - low)

    return ordered[0], at(0.25), at(0.5), at(0.75), ordered[-1]


def summarise(values: Sequence[float], unit: str = "", places: int = 1) -> str:
    """`median X (range Y–Z)`, or the bare value when there is only one."""
    if len(values) == 1:
        return f"{values[0]:.{places}f}{unit}"
    low, _, median, _, high = quantiles(values)
    return (
        f"median {median:.{places}f}{unit} "
        f"(range {low:.{places}f}–{high:.{places}f}{unit})"
    )


def histogram(values: Sequence[float], ladder: Sequence[float] = DEPTH_LADDER) -> list[tuple[str, int, str]]:
    """Bucket `values`, trimming empty leading and trailing buckets.

    Interior zeros are kept, because a gap is the signal: `esox-lucius`
    has two populations either side of one, which is what a cohort
    assembled from two sequencing efforts looks like. Trimming the edges
    is what stops a fixed ladder becoming nine rows of zeros for a
    tightly-grouped cohort.
    """
    counts = [
        sum(1 for value in values if ladder[i] <= value < ladder[i + 1])
        for i in range(len(ladder) - 1)
    ]
    occupied = [i for i, count in enumerate(counts) if count]
    if not occupied:
        return []
    peak = max(counts)
    rows = []
    for i in range(occupied[0], occupied[-1] + 1):
        top = ladder[i + 1]
        label = f"{ladder[i]:g}–{top:g}×" if top != float("inf") else f"{ladder[i]:g}×+"
        bar = "█" * max(1, round(BAR_WIDTH * counts[i] / peak)) if counts[i] else ""
        rows.append((label, counts[i], bar))
    return rows


def table(headers: Sequence[str], rows: Iterable[Sequence[str]], *, align: str = "") -> list[str]:
    """A GitHub-flavoured markdown table.

    `align` is one character per column: `r` right, `c` centre, anything
    else left. Blank headers are legal — a two-column label/value table
    reads better without them.
    """
    separators = []
    for index in range(len(headers)):
        marker = align[index] if index < len(align) else "l"
        separators.append({"r": "---:", "c": ":---:"}.get(marker, "---"))
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join(separators) + "|"]
    out += ["| " + " | ".join(str(cell) for cell in row) + " |" for row in rows]
    return out


def details(summary: str, body: Sequence[str]) -> list[str]:
    """A collapsed section, for facts that are true but not in the way."""
    return ["<details>", f"<summary>{summary}</summary>", "", *body, "", "</details>"]


def alert(kind: str, lines: Sequence[str]) -> list[str]:
    """A GitHub alert callout: `> [!NOTE]` and friends."""
    out = [f"> [!{kind}]"]
    for line in lines:
        out.append(f"> {line}".rstrip())
    return out


def italic_binomial(slug: str) -> str:
    """`apteryx-mantelli` -> `Apteryx mantelli`, unitalicised."""
    parts = slug.split("-")
    return " ".join([parts[0].capitalize(), *parts[1:]])


def sentence_list(items: Sequence[str], conjunction: str = "and") -> str:
    items = list(items)
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} {conjunction} {items[1]}"
    return ", ".join(items[:-1]) + f" {conjunction} {items[-1]}"
