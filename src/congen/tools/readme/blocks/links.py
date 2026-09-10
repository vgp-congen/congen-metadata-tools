"""The download block — how do I fetch this?

GenomeArk's web browser does not cover `downstream_analyses/`, and the
alternatives are worse than nothing: the S3 REST listing renders as raw
XML, and the AWS console needs a login, which defeats an anonymous
bucket. So this block replaces a browse UI.

An earlier draft concluded that therefore every file a human might open
should get a row — sixteen of them. The Phase 0 render disproved it:
sixteen rows of near-equal weight is an index nobody scans, and it
buries the three or four artifacts people come for. Primary gets rows;
secondary is collapsed; the redundant GenomeArk copies of `README.txt`
and `sample_sheet.csv` are excluded entirely, the latter because it is
explicitly not ground truth — `S006` exists because it can disagree with
the repo copy.
"""

from __future__ import annotations

from congen.core.remote.genomeark import BASE_URL

from ..format import alert, details, human_size, sentence_list, table
from . import Context, Rendered

#: What people come here for. Order is the order of the table.
PRIMARY = (
    ("vcfs/raw.vcf.gz", "unfiltered joint-genotyped calls"),
    ("vcfs/raw.vcf.gz.tbi", "index for the above"),
    ("vcfs/filtered.vcf.gz", "filtered calls"),
    ("vcfs/filtered.vcf.gz.tbi", "index for the above"),
    ("callable_sites/callable_sites.bed", "the callable-sites mask"),
    ("qc/qc_dashboard.html", "the snpArcher QC report — opens in a browser"),
)

#: Linked from the QC block, where they are discussed, and collapsed
#: here so the block stays an index rather than a listing.
SECONDARY = (
    "qc/qc_report.tsv",
    "qc/individuals.idepth",
    "qc/individuals.het",
    "qc/individuals.imiss",
    "qc/individuals.samps.txt",
    "qc/contig_map.tsv",
    "callable_sites/coverage.bed",
    "callable_sites/mappability.bed",
    "callable_sites/mappability.bedgraph",
    "callable_sites/coverage_thresholds.tsv",
)

#: Present on GenomeArk, deliberately not linked: each duplicates a file
#: already in this directory.
EXCLUDED = ("README.txt", "sample_sheet.csv")

BAMS = "bams/"


def render(context: Context) -> Rendered:
    dataset = context.dataset
    prefix = dataset.prefix
    if not prefix or not dataset.objects:
        return Rendered(
            ["## Getting the data", "", "Nothing is published for this accession yet."]
        )

    lines = [
        "## Getting the data",
        "",
        f"Everything below is under `s3://genomeark/{prefix}/`. The bucket is "
        "public and needs no credentials, but the AWS CLI needs "
        "`--no-sign-request` or it will try to sign the request and fail.",
        "",
    ]

    rows = []
    for path, blurb in PRIMARY:
        if context.has(path):
            name = path.rsplit("/", 1)[-1]
            rows.append(
                (
                    context.link(path, f"`{name}`"),
                    human_size(dataset.objects[path]["size"]),
                    blurb,
                )
            )
    bam_count = dataset.count_of(BAMS)
    if bam_count:
        rows.append(
            (
                f"`{BAMS}`",
                human_size(dataset.size_of(BAMS)),
                f"{bam_count} objects — alignments and indexes",
            )
        )
    lines += table(("", "Size", ""), rows, align="lrl")

    absent = [
        path.rsplit("/", 1)[-1]
        for path, _ in PRIMARY
        if not context.has(path) and not path.endswith(".tbi")
    ]
    if absent:
        lines += [
            "",
            "This run did not produce "
            + sentence_list([f"`{name}`" for name in absent])
            + ".",
        ]

    lines += ["", *_recipes(prefix)]

    zarr = sorted(
        name for names in (dataset.opaque_subdirs or {}).values() for name in names
    )
    if zarr:
        lines += ["", *_zarr_warning(zarr)]

    secondary = [path for path in SECONDARY if context.has(path)]
    if secondary:
        body = table(
            ("", "Size"),
            [
                (context.link(path, f"`{path}`"), human_size(dataset.objects[path]["size"]))
                for path in secondary
            ],
            align="lr",
        )
        lines += ["", *details(f"The other {len(secondary)} published files", body)]
    return Rendered(lines)


def _recipes(prefix: str) -> list[str]:
    """The ranged read leads deliberately.

    Most people asking how to get a multi-gigabyte VCF should not be
    downloading it.
    """
    return [
        "```bash",
        "# one region of the VCF, without downloading the whole thing",
        "bcftools view -r <chr>:<start>-<end> \\",
        f"  {BASE_URL}/{prefix}/vcfs/raw.vcf.gz",
        "",
        "# the alignments — check the size above first",
        "aws s3 sync --no-sign-request \\",
        f"  s3://genomeark/{prefix}/bams/ ./bams/",
        "",
        "# the masks and QC tables, without the zarr stores",
        "aws s3 sync --no-sign-request --exclude '*.zarr/*' \\",
        f"  s3://genomeark/{prefix}/callable_sites/ ./callable_sites/",
        "```",
    ]


def _zarr_warning(zarr: list[str]) -> list[str]:
    """Say that the sizes above are a lower bound, and why.

    A delimited listing returns these as prefixes and never descends, so
    nothing here knows how large they are. Stating a total without this
    caveat would be false: one species' `callable_loci.zarr/` alone runs
    to thousands of objects.
    """
    return alert(
        "WARNING",
        [
            "`callable_sites/` also holds "
            + sentence_list([f"`{name}/`" for name in zarr])
            + " — zarr stores of thousands of small objects each.",
            "",
            "Nothing here lists or sizes them, so the sizes above are a lower "
            "bound, and a recursive `sync` without the `--exclude` above will "
            "pull all of them.",
        ],
    )
