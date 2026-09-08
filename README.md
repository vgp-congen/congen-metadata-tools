# congen-metadata-tools

Tools for processing VGP Conservation Genomics metadata — the species
metadata in [`congen-metadata`](https://github.com/vgp-congen/congen-metadata)
and the snpArcher outputs published on GenomeArk.

See [`docs/validator-design.md`](docs/validator-design.md) for the architecture
and the design of the first two tools.

## Status

Milestone 1 (shared core) is in place:

| Module | What it does |
|---|---|
| `congen.core.http` | ranged GET with retry/backoff |
| `congen.core.cache` | ETag-aware JSON disk cache |
| `congen.core.remote.headers` | VCF/BAM headers over HTTP Range, no htslib |
| `congen.core.metadata` | tolerant loaders, models, species discovery, VGP list |
| `congen.core.metadata.writers` | round-trip YAML, managed blocks, atomic writes |

Not yet built: the `congen` CLI, the findings framework, and the `validate`
and `readme` tools themselves.

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
