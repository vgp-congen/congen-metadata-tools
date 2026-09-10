"""The description block — what is this dataset?

Five rows, because a ten-row table buried the five that vary. Everything
else is either suppressed (when it agrees with expectation) or collapsed
into a `<details>`.

Recorded provenance beats declared provenance throughout: the config
records an intent, the VCF header records an execution.
"""

from __future__ import annotations

from ..format import details, table, thousands
from . import Context, Rendered

NCBI_GENOME = "https://www.ncbi.nlm.nih.gov/datasets/genome"

INPUT_TYPE_WORDS = {
    "srr": "SRA runs",
    "srx": "SRA experiments",
    "fastq": "local FASTQ",
    "bam": "local BAM",
}


def render(context: Context) -> Rendered:
    dataset = context.dataset
    assembly = dataset.assembly or {}
    vcf = dataset.vcf or {}
    sheet = context.species.sheet

    rows = [
        ("Clade", context.species.clade),
        ("Reference assembly", _assembly(dataset, assembly)),
        ("Variant caller", _caller(vcf)),
        ("Samples", describe_samples(sheet)),
        ("Variant sites", _sites(dataset)),
    ]
    rows += _deviations(context, dataset, vcf)

    lines = ["## Dataset", "", *table(("", ""), rows)]
    supplementary = _supplementary(assembly, vcf)
    if supplementary:
        lines += ["", *details("Assembly and pipeline detail", table(("", ""), supplementary))]
    return Rendered(lines)


def _assembly(dataset, assembly: dict) -> str:
    accession = dataset.accession or "—"
    out = f"[`{accession}`]({NCBI_GENOME}/{accession}/)" if dataset.accession else "—"
    trailer = [part for part in (assembly.get("assembly_name"), assembly.get("assembly_level")) if part]
    if trailer:
        out += " — " + ", ".join(trailer)
    return out


def _caller(vcf: dict) -> str:
    """The callers, not every tool in the header.

    `bcftools` sits beside `gatk` in every corpus VCF and is not a
    variant caller; `VcfHeader.callers()` is the field that knows the
    difference.
    """
    versions = vcf.get("tool_versions") or {}
    callers = vcf.get("callers") or []
    if not callers:
        return "—"
    return ", ".join(f"{name} {versions.get(name, '?')}" for name in callers)


def _sites(dataset) -> str:
    sites = (dataset.cohort or {}).get("n_sites")
    return thousands(int(sites)) if sites else "—"


def describe_samples(sheet) -> str:
    counts: dict[str, int] = {}
    for row in sheet.rows:
        counts[row.input_type] = counts.get(row.input_type, 0) + 1
    unique = len(sheet.unique_sample_ids)
    if len(counts) == 1:
        word = INPUT_TYPE_WORDS.get(next(iter(counts)), next(iter(counts)))
        source = f", all from {word}"
    elif counts:
        source = ", from " + ", ".join(
            f"{count} {INPUT_TYPE_WORDS.get(kind, kind)}"
            for kind, count in sorted(counts.items())
        )
    else:
        source = ""
    spread = "" if unique == len(sheet.rows) else f" across {len(sheet.rows)} sheet rows"
    return f"{unique}{spread}{source}"


def _deviations(context: Context, dataset, vcf: dict) -> list[tuple[str, str]]:
    """Rows that exist only when a fact departs from expectation.

    When these agree they are noise; when they disagree they are the most
    important thing on the page. Suppressing the agreeing case is what
    keeps the table at five rows for a typical species while making an
    odd one loud.
    """
    out = []
    canonical = _canonical_accession(context)
    if canonical and dataset.accession and canonical != dataset.accession:
        out.append(
            ("⚠︎ Not the VGP main haplotype", f"VGP lists `{canonical}` for this species")
        )
    ploidy = vcf.get("ploidy")
    if ploidy and str(ploidy) != "2":
        out.append(("⚠︎ Ploidy", str(ploidy)))
    if not dataset.accession_matches:
        out.append(
            (
                "⚠︎ Accession",
                f"the config declares `{dataset.declared_accession}`; "
                f"the data is published under `{dataset.accession}`",
            )
        )
    return out


def _canonical_accession(context: Context) -> str | None:
    if context.vgp is None:
        return None
    entry = None
    if context.dataset.accession:
        entry = context.vgp.by_accession(context.dataset.accession)
    entry = entry or context.vgp.by_slug(context.species.slug)
    return entry.accession if entry else None


def _supplementary(assembly: dict, vcf: dict) -> list[tuple[str, str]]:
    versions = vcf.get("tool_versions") or {}
    others = {
        name: version
        for name, version in versions.items()
        if name not in set(vcf.get("callers") or ())
    }
    rows = [
        ("Assembly organism", assembly.get("organism_name") or "—"),
        (
            "Paired RefSeq/GenBank accession",
            f"`{assembly['paired_accession']}`" if assembly.get("paired_accession") else "—",
        ),
        ("NCBI taxon", str(assembly.get("tax_id") or "—")),
        ("Contigs in the VCF", str(vcf.get("n_contigs") or "—")),
        (
            "Ploidy / heterozygosity prior",
            f"{vcf['ploidy']} / {vcf.get('het_prior') or '—'}" if vcf.get("ploidy") else "—",
        ),
        (
            "Other tools recorded in the VCF header",
            ", ".join(f"{name} {version}" for name, version in sorted(others.items())) or "—",
        ),
    ]
    return [row for row in rows if row[1] != "—"] or []
