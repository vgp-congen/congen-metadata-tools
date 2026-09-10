# congen-metadata-tools

Tools for processing VGP Conservation Genomics metadata — the species
metadata in [`congen-metadata`](https://github.com/vgp-congen/congen-metadata)
and the snpArcher outputs published on GenomeArk.

Design documents: [`docs/validator-design.md`](docs/validator-design.md) for the
shared architecture and `congen validate`;
[`docs/readme-design.md`](docs/readme-design.md) for `congen readme` and
`congen citations`.

## Status

Three tools, all built.

| Tool | What it does | Writes |
|---|---|---|
| `congen validate` | cross-checks the metadata against GenomeArk. 44 checks over tiers 0–4, plus tier 5 behind `--check-sra`, and `G020`/`G021` at corpus level | validation reports, behind `--write-reports` |
| `congen readme` | harvests GenomeArk, NCBI, the QC tables and SRA into `dataset.json`, then renders a `README.md` per species | `dataset.json`, `README.md` |
| `congen citations` | proposes publication citations for contributing BioProjects, and files the ones a human accepts | the review queue and the citation record |

Every generated file is **idempotent**: regenerating without an input change
writes nothing. That is what makes the automatic rebuild in `congen-metadata`
readable — without it every push produced a 160-file diff of nothing but
timestamps.

| Module | What it does |
|---|---|
| `congen.core.http` | ranged GET with retry/backoff, per-host rate limits |
| `congen.core.cache` | ETag-aware JSON disk cache |
| `congen.core.findings` | severities, findings, the check registry |
| `congen.core.status` | publication state: absent / partial / complete |
| `congen.core.validation_record` | report schema, content digests, staleness |
| `congen.core.report` | human / JSON / GitHub-annotation renderers |
| `congen.core.metadata` | tolerant loaders, models, species discovery, VGP list |
| `congen.core.metadata.writers` | round-trip YAML, managed blocks, atomic writes |
| `congen.core.metadata.citations` | the citation review queue and settled record |
| `congen.core.remote.headers` | VCF/BAM headers over HTTP Range, no htslib |
| `congen.core.remote.genomeark` | anonymous S3 listing, zarr-aware |
| `congen.core.remote.ncbi` | NCBI Datasets metadata and assembly reports |
| `congen.core.remote.bioproject` | NCBI BioProject titles and submitters |
| `congen.core.remote.qc` | snpArcher QC and callable-sites tables |
| `congen.core.remote.sra` | NCBI SRA runinfo, batched and rate-limited |
| `congen.core.remote.literature` | Europe PMC accession search |
| `congen.core.remote.doi` | doi.org resolution, for typo-checking hand-typed DOIs |

Not yet built: the References block of the generated README, which will render
the curated citations rather than a placeholder. See
[`docs/readme-design.md`](docs/readme-design.md), phase 5.

## Usage

### Generating documents

```bash
congen readme --all --check     # offline: is any README.md out of date?
congen readme --all             # offline: rewrite them
congen readme --refresh --all   # network: re-harvest into dataset.json
congen readme --gate-report     # offline: which species render fully, and why not
```

The network pass and the render are separate commands on purpose, so the
offline one runs in a job with no egress at all. `--refresh` never renders and
a bare `readme` never fetches.

A README is rendered **in full** only when there is a complete, error-free
dataset to describe; otherwise it is truncated to a heading and a verdict. Its
References block is withheld separately, when the BioProject list is missing or
known to be wrong. See `docs/readme-design.md`.

### Citations

```bash
congen citations --report    # offline: progress, and what is left
congen citations --propose   # network: look up newly-seen BioProjects
congen citations --verify    # network: check accepted DOIs resolve
congen citations --collect   # offline: file decided blocks into the record
```

Review by editing `references/citations-review.md` and **deleting the lines
that are not true** — every line in it is a claim. Editing that file on GitHub
makes each decision a commit.

### Validation reports

`--write-reports` writes `VALIDATION.md` and `validation.json` into each species
directory, and a `VALIDATION.md` table at the repo root. Without the flag
`validate` is read-only.

```bash
congen validate --all   --write-reports --check-sra   # force: revalidate everything
congen validate --stale --write-reports --check-sra   # only what needs it
congen validate --check-stale                         # offline: what is out of date
congen validate --mark-stale                          # offline: stamp those STALE
```

A validation is a claim about specific inputs, so a report records SHA-256
digests of the metadata it validated and the S3 ETags of the data. Staleness is
then a pure function of the current files — content, not mtimes, because git does
not preserve mtimes.

A verdict that has not changed keeps the date it was established, so
re-validating writes nothing. `validated_at` means *when this verdict was
reached*, not *when we last looked* — which is also what the design doc always
claimed a validation was.

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
  core/          shared: access to congen-metadata, GenomeArk, NCBI, Europe PMC
  tools/         validate/ readme/ citations/, registered via entry points
tests/
  fixtures/      real metadata files, 16 KiB header prefixes, golden documents
```

Core owns *access* to the data sources; tools own *interpretation*.

## What lands in congen-metadata

| File | Written by | Hand-edited? |
|---|---|---|
| `VALIDATION.md`, `validation.json` | `validate --write-reports` | no |
| `CHECKS.md` | `validate --write-reports` | no |
| `dataset.json` | `readme --refresh` | no |
| `README.md` | `readme` | only below the `congen:end` marker |
| `references/citations-review.md` | `citations --propose`/`--collect` | **yes — this is the review surface** |
| `references/bioproject_citations.json` | `citations --collect`/`--verify` | no |
| `references/tool_citations.yaml` | nobody | **yes** |

A push to `congen-metadata` that touches source data rebuilds all of the "no"
rows automatically; see that repository's `.github/workflows/rebuild.yml`. It
pins a tagged version of this package, so a renderer change arrives there as a
deliberate bump rather than overnight.
