"""The sample-QC block — what does the QC say?

Distributions, never flags. The only thresholds published alongside the
data (`coverage_thresholds.tsv`) bound *site* depth when building the
callable-sites mask; comparing a sample's mean depth against them would
"fail" 17.8% of the corpus, because the pipeline never made that
judgement about samples at all. Inventing our own cutoffs would be
establishing QC policy inside a documentation generator, which is the
wrong place for that conversation.

This block links the tables it summarises. They are kilobytes, and a
reader of a summary wants the originals to hand — which is also why the
download block does not carry them.
"""

from __future__ import annotations

from ..format import histogram, summarise, table
from . import Context, Rendered

DASHBOARD = "qc/qc_dashboard.html"
QC_REPORT = "qc/qc_report.tsv"
HET = "qc/individuals.het"
IMISS = "qc/individuals.imiss"
THRESHOLDS = "callable_sites/coverage_thresholds.tsv"

#: The depth this block reports, and why. `qc_report.tsv` measures over
#: mapped reads and is what the QC dashboard plots;
#: `individuals.idepth` measures over called sites. They disagree by a
#: few × per sample, so the document names its source rather than
#: leaving a reader to rediscover the discrepancy.
DEPTH_METRIC = "mean_depth"


def render(context: Context) -> Rendered:
    dataset = context.dataset
    depths = dataset.metric(DEPTH_METRIC)
    if not depths:
        return Rendered(
            [
                "## Sample QC",
                "",
                "No QC tables are published for this accession.",
            ]
        )

    lines = ["## Sample QC", "", _sources(context), "", _headline(depths), ""]
    rows = histogram(sorted(depths.values()))
    lines += table(("mean depth", "samples", ""), rows, align="lr")

    cohort_rows = _cohort(dataset)
    if cohort_rows:
        lines += ["", "Across the cohort:", "", *table(("", ""), cohort_rows)]

    mask = _mask_note(context)
    if mask:
        lines += ["", mask]
    return Rendered(lines)


def _sources(context: Context) -> str:
    parts = []
    if context.has(DASHBOARD):
        parts.append(f"Full plots: {context.link(DASHBOARD, 'the snpArcher QC dashboard')}.")
    tables = [
        context.link(path)
        for path in (QC_REPORT, HET, IMISS)
        if context.has(path)
    ]
    if tables:
        joined = ", ".join(tables[:-1]) + (f" and {tables[-1]}" if len(tables) > 1 else tables[-1] if not tables[:-1] else "")
        parts.append(
            f"Per-sample values are not reproduced here; they are in {joined}."
        )
    return " ".join(parts)


def _headline(depths: dict[str, float]) -> str:
    values = sorted(depths.values())
    lowest = min(depths, key=lambda sample: depths[sample])
    highest = max(depths, key=lambda sample: depths[sample])
    if len(values) == 1:
        return f"**Mean depth {values[0]:.1f}×** for the single sample `{lowest}`, over mapped reads."
    from ..format import quantiles

    low, q1, median, q3, high = quantiles(values)
    return (
        f"**Mean depth {median:.1f}×** (IQR {q1:.1f}–{q3:.1f}, "
        f"range {low:.1f}× `{lowest}` to {high:.1f}× `{highest}`), over mapped reads."
    )


def _cohort(dataset) -> list[tuple[str, str]]:
    cohort = dataset.cohort or {}
    rows: list[tuple[str, str]] = []
    if cohort.get("mean_coverage") is not None:
        rows.append(("Cohort mean coverage", f"{cohort['mean_coverage']:.1f}×"))
    for label, metric, unit, places in (
        ("Reads mapped", "percent_mapped", "%", 1),
        ("Duplicates", "percent_duplicates", "%", 1),
        ("Properly paired", "percent_properly_paired", "%", 1),
        ("Missingness F_MISS", "f_missing", "", 3),
        ("Inbreeding coefficient F", "f_inbreeding", "", 3),
    ):
        values = sorted(dataset.metric(metric).values())
        if values:
            rows.append((label, summarise(values, unit, places)))
    return rows


def _mask_note(context: Context) -> str | None:
    """State what the mask thresholds are, and what they are not.

    Without the second half a reader will compare them against the
    per-sample depths above and conclude that samples were excluded.
    """
    cohort = context.dataset.cohort or {}
    low, high = cohort.get("min_coverage"), cohort.get("max_coverage")
    if low is None or high is None:
        return None
    source = f" ({context.link(THRESHOLDS)})" if context.has(THRESHOLDS) else ""
    return (
        f"The callable-sites mask was built over depths {low:g}–{high:g}×{source}. "
        "That is a site-level threshold for the mask, not a per-sample cutoff: "
        "samples outside it are neither excluded nor flagged here."
    )
