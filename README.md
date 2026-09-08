# congen-metadata-tools

Tools for processing VGP Conservation Genomics metadata — the species
metadata in [`congen-metadata`](https://github.com/vgp-congen/congen-metadata)
and the snpArcher outputs published on GenomeArk.

See [`docs/validator-design.md`](docs/validator-design.md) for the architecture
and the design of the first two tools.

## Status

All five milestones are in place. `congen validate` runs tiers 0, 1, 2, 3a, 3b
and 4 over one species or the whole corpus, plus tier 5 behind `--check-sra`.

| Module | What it does |
|---|---|
| `congen.core.http` | ranged GET with retry/backoff |
| `congen.core.cache` | ETag-aware JSON disk cache |
| `congen.core.findings` | severities, findings, the check registry |
| `congen.core.status` | publication state: absent / partial / complete |
| `congen.core.report` | human / JSON / GitHub-annotation renderers |
| `congen.core.metadata` | tolerant loaders, models, species discovery, VGP list |
| `congen.core.metadata.writers` | round-trip YAML, managed blocks, atomic writes |
| `congen.core.remote.headers` | VCF/BAM headers over HTTP Range, no htslib |
| `congen.core.remote.genomeark` | anonymous S3 listing, zarr-aware |
| `congen.core.remote.ncbi` | NCBI Datasets metadata and assembly reports |
| `congen.core.remote.qc` | snpArcher QC tables (`contig_map.tsv` so far) |
| `congen.core.remote.sra` | NCBI SRA runinfo, batched and rate-limited |
| `congen.tools.validate` | 45 checks across tiers 0, 1, 2, 3a, 3b, 4 and 5 |

Not yet built: the CI workflow and the `readme` tool.

## Usage

```bash
congen validate reptiles/podarcis-raffonei   # one species
congen validate --all                        # the whole corpus, ~16s
congen validate --list-checks                # the catalog
congen validate --all --json report.json --strict
congen validate --all --check-sra              # + NCBI SRA cross-checks (tier 5)
```

Tier 5 is opt-in: it is the only tier whose cost scales with sample count rather
than species count. Set `NCBI_API_KEY` to raise the eutils rate cap from 3/s to
10/s.

Exit codes: `0` clean or warnings only, `1` any error, `2` misuse.

The report has two parts. **Findings** are defects. **Publication status** is a
description — whether a species has data yet, which optional artifacts exist,
and which accession the data sits under. A species awaiting its run is a status
row, not a warning.

## Install

```bash
pip install -e '.[dev]'
```

The runtime dependencies are deliberately pure-Python — `ruamel.yaml` and
`click`, nothing else. In particular there is **no pysam** (headers are read via
HTTP Range requests) and **no boto3** (S3 listing uses anonymous
`ListObjectsV2` over the standard library). Please keep it that way; see the
design doc for why.

## Tests

```bash
pytest
```

The suite runs entirely offline against recorded fixtures. A handful of canary
tests check those fixtures still match live GenomeArk, and are deselected by
default:

```bash
pytest -m network
```

## A note on virtualenvs and Dropbox

If your checkout lives inside a Dropbox folder, **do not create the virtualenv
inside the repo.** Dropbox rewrites the symlinks in `.venv/bin` into conflict
copies (`python 2`, `python3 2`, …), which breaks the environment in a way that
looks like a missing interpreter. Put it somewhere unsynced instead:

```bash
python3 -m venv ~/.virtualenvs/congen && ~/.virtualenvs/congen/bin/pip install -e '.[dev]'
```

## Layout

```
src/congen/
  core/          shared: access to congen-metadata and GenomeArk
  tools/         one subpackage per tool, registered via entry points
tests/
  fixtures/      real metadata files and 16 KiB header prefixes
```

Core owns *access* to the two data sources; tools own *interpretation*.
