# congen-metadata-tools — architecture, and the `validate` tool

This repo will host several tools over the metadata in
[`congen-metadata`](https://github.com/vgp-congen/congen-metadata) and the
snpArcher outputs on GenomeArk. This document defines the shared core those tools
build on, specifies the first tool (`congen validate`), and sketches the second
(`congen readme`) far enough to check that the core boundary is drawn correctly.

## Part 1 — Shared architecture

### Layout

One distribution, one import package, a shared core, tools as registered
subcommands.

```
congen-metadata-tools/
  pyproject.toml                 distribution: congen-metadata-tools
  src/congen/
    core/
      http.py                    ranged GET, retry/backoff, User-Agent
      cache.py                   disk cache, ETag-aware
      findings.py                Severity, Finding, CheckRegistry
      metadata/
        discovery.py             SpeciesRepo — find and select species dirs
        models.py                SpeciesMetadata, SampleRow, ReferenceSpec
        loaders.py               tolerant readers (read-only)
        writers.py               round-trip YAML + CSV + managed-block writers
        vgp.py                   VGP reference-genome list
      remote/
        genomeark.py             ListObjectsV2, delimiter-based, zarr-aware
        headers.py               range-read VCF/BAM headers
        qc.py                    snpArcher QC/callable-sites table readers
        ncbi.py                  datasets API + assembly_report.txt
        ena.py                   filereport (SRR → biosample, study membership)
        literature.py            study → publication lookup (readme tool)
      report/render.py           human / json / github renderers
      cli.py                     `congen` dispatcher
    tools/
      validate/                  Part 2
      readme/                    Part 3
  tests/fixtures/                recorded responses; the suite runs offline
```

### The core/tool boundary

The rule that keeps this from rotting: **core owns access to the two data
sources; tools own interpretation.** Anything that reads or writes
`congen-metadata` or GenomeArk belongs in `core`. Anything that decides what a
finding or a sentence *means* belongs in a tool.

With two concrete tools the shared surface is no longer guesswork:

| Core module | `validate` | `readme` | Later |
|---|---|---|---|
| `metadata/loaders.py`, `models.py` | reads config + sheet | reads config + sheet | all |
| `metadata/writers.py` | — | **writes `README.md`** | sheet builder, config scaffolder |
| `metadata/vgp.py` | `F020`–`F024` | species name, taxid | status |
| `remote/headers.py` | sample + contig identity | sample count, contig count | filtering pipeline |
| `remote/genomeark.py` | presence checks | file inventory, sizes | upload/sync |
| `remote/qc.py` | `S005` sample list | **coverage range, het, missingness** | status |
| `remote/ncbi.py` | reference identity | assembly name, organism | sheet builder |
| `remote/ena.py` | `E001`–`E003` | run counts, study titles | sheet builder |
| `remote/literature.py` | — | citations | — |
| `findings.py`, `report/` | primary consumer | diagnostics while generating | all |
| `http.py`, `cache.py` | all remote | all remote | all |

Two modules exist *because* of the second tool rather than the first —
`remote/qc.py` and `metadata/writers.py` — and both have a validator consumer
too, which is the signal that they belong in core rather than inside `readme/`.

### Tool registration

Tools register through a setuptools entry point group:

```toml
[project.entry-points."congen.tools"]
validate = "congen.tools.validate:tool"
readme   = "congen.tools.readme:tool"
```

`congen.cli` discovers that group and builds the subcommand list, so a tool can
later move to its own distribution without touching the dispatcher. Each
registration declares metadata the dispatcher surfaces:

```python
tool = Tool(name="validate", summary="Cross-check metadata against GenomeArk",
            writes_metadata=False, needs_network=True)
```

`writes_metadata` matters because **read-only is a property of the validator, not
of the framework** — `readme` will legitimately write to `congen-metadata`.
Declaring it lets `congen --list-tools` show it and lets CI refuse write-capable
tools by policy rather than convention.

### Keep the core pure Python

Two dependencies that look inevitable are not:

- **pysam is unnecessary** — headers come from HTTP Range reads (see the trap
  below), and no planned tool reads VCF *records*.
- **boto3 and the AWS CLI are unnecessary** — anonymous `ListObjectsV2` over
  plain HTTPS returns keys, sizes, and ETags from the stdlib. Verified.

That leaves `ruamel.yaml` and `click`, both pure Python. CI installs in seconds
instead of compiling htslib, with no binary-wheel platform risk. Worth protecting
deliberately: adding pysam later for one convenience forfeits it.

**pydantic came out too.** An earlier draft of this document listed it, but the
models are plain dataclasses instead. The loaders must be *tolerant* — a
malformed sample sheet has to come back with its problems described so a tool can
report them, not raise on the first bad row — and validation-on-construction is
precisely the wrong behaviour for that. Schema checking lives in the loaders,
which already have to collect issues rather than throw; the models stay dumb
containers.

### Round-trip YAML, and the boolean wrinkle

The configs are heavily commented, and `congen readme` writes into
congen-metadata, so `core.metadata` standardizes on `ruamel.yaml` round-trip mode:
comments, key order and quoting all survive a rewrite.

One thing it cannot do, found while building it: **ruamel always emits YAML
booleans lowercase**, and these configs mix styles within a single file
(`generate_bed_file: True` beside `enabled: true`). Left alone, changing one value
would produce a diff on every boolean line in the file, burying the real change.
`ScalarBoolean` does not help — ruamel does not retain the original spelling.

So `writers.dump_yaml_preserving(original, data)` pairs the dump with a pass that
restores the original spelling, and does it conservatively: only where every
occurrence of that key at that indent agreed in the original, so an
already-inconsistent key keeps ruamel's form. `True` and `true` are the same YAML
scalar, so the rewrite is cosmetic by definition. With it, all 79 configs
round-trip byte-identically; without it, 78 of 79 do not.

### Findings framework

```python
Finding(id, severity, subject, message, detail=None, location=None)
```

`subject` rather than `species`, so corpus-level findings (an orphan accession
belonging to no species) have somewhere to live. `location` is an optional
`(file, line)` for CI annotations.

Severities: `ERROR`, `WARN`, `INFO`, `SKIPPED`.

Checks self-register with metadata, declaring which slices of context they need:

```python
@check(id="S001", tier=2, severity=Severity.ERROR,
       summary="sheet samples == VCF samples",
       needs=("sheet", "vcf_header"))
def sheet_vs_vcf(ctx) -> list[Finding]: ...
```

Two things fall out of `needs`. The gather phase fetches only what the selected
checks require. And **a check whose inputs are unavailable reports `SKIPPED`, not
`ERROR`** — so a species with BAMs but no VCF yet produces one honest finding
about the missing VCF instead of a cascade of false sample mismatches.

Check IDs are public API: CI configs will suppress them by ID. Retire, don't
renumber.

## Part 2 — `congen validate`

Cross-checks the repo metadata against the published snpArcher outputs, and now
against the VGP reference-genome list. It answers three questions per species:

1. **Do the samples agree?** Are the biosamples in `sample_sheet.csv` exactly the
   biosamples in the VCF and the BAMs?
2. **Is it the right genome?** Does the reference declared in `config.yaml` match
   the reference actually used to produce the VCF and BAMs?
3. **Is it the *canonical* genome?** Is that reference the VGP main-haplotype
   assembly for the species?

**Strictly read-only.** It reports; it never edits `congen-metadata`. That keeps
`exit != 0` meaningful and makes it safe to run anywhere. Several findings below
are ambiguous by nature and *require* human judgement, which is the other reason
not to give this tool write access.

### The three sides

#### Repo side

```
references/vgp_reference_genomes.csv     ← the VGP main-haplotype list (124 species)
species/{clade}/{species-slug}/
    config.yaml        snpArcher v2 config; reference.name + reference.source are the keys
    sample_sheet.csv   sample_id,input_type,input  (+ optional library_id)
    README.txt         species, accession, contributing NCBI bioprojects  (57/79)
```

79 species across `birds`, `mammals`, `fishes`, `reptiles`. Loaders must tolerate
the real state of the corpus:

- 24 sheets use CRLF; read as `utf-8-sig` and normalize newlines.
- At least one sheet has a blank trailing row — skip blank rows, don't error.
- Two sheets carry an extra `library_id` column — allow unknown extra columns.
- One `input` value is a local scratch fastq path rather than an SRA run.
- `birds/sturnus-vulgaris` is the structural outlier: an accession in
  `reference.name`, a nonstandard top-level `reads:` key, and no `intervals:`.
  Use it as the "weird config" fixture.

**Duplicate `sample_id` rows are normal, not errors.** 42 of 79 sheets repeat a
`sample_id`, because snpArcher accepts multiple sequencing runs per biosample and
merges them. Every sample comparison must be over the **set of unique
`sample_id` values**, never over row counts. Getting this wrong would
false-positive on more than half the corpus.

#### The VGP reference-genome list

Committed verbatim as `references/vgp_reference_genomes.csv` — 124 species,
columns `ScientificName,QID,Accession.for.main.haplotype`.

**The `QID` column holds NCBI taxonomy IDs, not Wikidata QIDs.** Verified against
the NCBI datasets API: `65483` → *Podarcis raffonei*, `91951` → *Catharus
ustulatus*, `9117` → *Grus americana*. Name the field `ncbi_taxid` in the model
and leave a comment, because the column name invites exactly the wrong
assumption.

The file is kept byte-identical to the source export rather than tidied, so
provenance is obvious and re-exports diff cleanly. `metadata/vgp.py` normalizes
the R-style header (`Accession.for.main.haplotype` → `accession`) and skips the
trailing blank row, consistent with the tolerance the sheet loader already needs.

This list is what makes reference validation *normative* rather than merely
self-consistent: it is the authority on which assembly a species should be called
against. It resolved both of the previous draft's open questions — see the
baseline.

#### GenomeArk side

Keyed by **assembly accession, not species**:

```
s3://genomeark/downstream_analyses/conservation_genomics/variant_calling/{ACCESSION}/
    README.txt
    sample_sheet.csv          a copy — a third source of truth, not an authority
    bams/{SAMPLE}.bam(.csi)
    vcfs/raw.vcf.gz(.tbi)     14 accessions also have filtered.vcf.gz(.tbi)
    qc/                       individuals.samps.txt, contig_map.tsv, plink outputs, …
    callable_sites/           beds + zarr stores
```

68 of 70 accessions have that full layout. Three properties to design around:

- **Never list `callable_sites/` recursively** — it holds zarr stores, 3,264
  objects for one species. All listing is delimiter-based, descending only into
  `bams/`, `vcfs/`, and `qc/`.
- **The S3 `sample_sheet.csv` is a copy, not ground truth.** For
  `birds/anser-albifrons` the S3 copy is byte-identical to the repo sheet and
  both disagree with the VCF. Comparing them catches upload drift, but only the
  VCF and BAM headers witness what actually ran.
- **Match accessions allowing GCA/GCF equivalence**, resolved via the NCBI
  `paired_assembly` field. With the VGP list in place, the *canonical* form is no
  longer ambiguous — see `F021`.

### The htslib index-cache trap

**Do not use pysam/htslib to read remote headers.**

htslib downloads a remote index into the **current working directory, named by
basename**. Every species' VCF is `raw.vcf.gz`, so the cached `raw.vcf.gz.tbi`
left by species A is silently picked up for species B — and htslib **merges the
stale index's sequence names into the header it reports**.

Observed live: a *Catharus ustulatus* VCF reported 183 contigs, 22 of them
*Podarcis raffonei*, appended after the file's own 161. The raw header bytes
contain only 161. It fails silently, looks plausible, and would manufacture
"contig not in assembly" errors. The same applies to the uniformly-named
`.bam.csi` files.

The fix is also the faster path — HTTP Range plus `zlib`:

| | Range + zlib | pysam |
|---|---|---|
| VCF header | 0.6 s | 1.7 s + 1 MB index download |
| BAM header | 0.3 s | 1.2 s + 2 MB index download |

Neither range read touches an index or writes to the working directory, and this
is what lets the core stay pure Python.

For VCF: range-read ~2 MB, inflate consecutive BGZF members until `#CHROM`, parse
the header text. For BAM: range-read ~512 KB, inflate, verify the `BAM\1` magic,
then read `l_text` / header text / `n_ref` / the per-reference name+length pairs.
Both must detect a header that overruns the window and retry with a larger range
rather than silently truncating.

**Retry with backoff is mandatory, not defensive.** GenomeArk reset roughly 1.5%
of connections during a full-corpus sweep (2 of ~134 header reads). Without
retry a clean corpus reports spurious failures; with 5 attempts and exponential
backoff, 67/67 succeeded.

### Check catalog

Stable IDs so findings can be referenced and suppressed in CI.

#### Tier 0 — repo self-consistency (no network)

| ID | Severity | Check |
|---|---|---|
| `R001` | error | `config.yaml` parses; has `reference`, `samples`, `variant_calling` |
| `R002` | error | `reference.source` is a GCA/GCF accession, URL, or path |
| `R003` | warn | `reference.name` looks like a species name, not an accession |
| `R010` | error | sheet parses; required columns `sample_id,input_type,input` present |
| `R011` | error | no empty or whitespace-only `sample_id` |
| `R012` | error | `input_type` in `{srr, fastq, bam}` |
| `R013` | error | for `input_type: srr`, `input` matches `[SED]RR[0-9]+` |
| `R014` | error | duplicate `(sample_id, input)` pairs — a genuinely repeated run |
| `R015` | warn | `input` is a local filesystem path — not reproducible |
| `R016` | warn | `sample_id` is not a BioSample accession (`SAMN`/`SAMEA`/`SAMD`) |
| `R020` | warn | `README.txt` accession matches `reference.source` |
| `R021` | info | `README.txt` missing |

`R014` is scoped to *pairs*: a repeated `sample_id` alone is expected.

#### Tier 1 — completeness on GenomeArk

| ID | Severity | Check |
|---|---|---|
| `G001` | error | accession prefix exists on S3 (after GCA/GCF resolution) |
| `G002` | warn | S3 data found only under the GCA/GCF counterpart of the config accession |
| `G003` | warn | no data on S3 for this species |
| `G010` | error | `vcfs/raw.vcf.gz` present |
| `G011` | error | `vcfs/raw.vcf.gz.tbi` present and not older than the VCF |
| `G012` | error | `bams/` non-empty |
| `G013` | error | every `bams/*.bam` has a matching `.csi` |
| `G014` | error | no zero-byte objects |
| `G015` | warn | expected subdirectory missing (`qc/`, `callable_sites/`) |
| `G016` | info | `filtered.vcf.gz` present / absent — recorded, never judged |
| `G020` | warn | S3 accession is in the VGP list but has no repo species directory |
| `G021` | warn | S3 accession is in neither the repo nor the VGP list — stray data |

**`G003` is a warning carrying legitimate information, not a defect.** A species
with no GenomeArk data is a normal, expected state — the run is pending — but it
should be visible rather than silent. Two consequences: it renders with its own
wording ("no data published yet") rather than as a failure, and because it
removes the inputs for tiers 1–4, those checks report `SKIPPED` via `needs`
rather than erroring. Currently 10 species.

**`G016` records `filtered.vcf.gz` presence at INFO and draws no conclusion.** It
is a default GATK output that some snpArcher versions omitted, so its presence
says nothing about the config or the run's validity. It is deliberately *not*
correlated with `modules.postprocess.enabled`. A future filtering pipeline will
own this file; until then the validator only inventories it.

**`G020` and `G021` split the old single orphan check**, because the VGP list now
distinguishes the two cases. An S3 accession in the VGP list with no repo
directory is a metadata gap to fill (currently *Balaenoptera ricei*); one in
neither is unexplained data worth a different conversation. Batch mode only.

#### Tier 2 — sample identity

The first primary ask. All comparisons over unique sets.

| ID | Severity | Check |
|---|---|---|
| `S001` | error | unique sheet `sample_id`s == VCF header sample columns |
| `S002` | error | unique sheet `sample_id`s == `bams/*.bam` basenames |
| `S003` | error | VCF samples == BAM basenames |
| `S004` | error | each BAM's `@RG SM` equals its own filename stem |
| `S005` | warn | `qc/individuals.samps.txt` agrees with VCF samples |
| `S006` | warn | S3 `sample_sheet.csv` differs from repo copy (normalized) |
| `S007` | warn | `@RG LB` values inconsistent with `library_id`, where present |

Stated as three checks rather than one so the report can distinguish "the sheet
is stale" from "a BAM failed to upload".

**`S001`–`S003` never assert a direction.** Either the sheet is stale or the VCF
is stale, and which is not derivable from the artifacts — it always requires human
investigation. The report states the disagreement and the evidence on both sides,
and stops there. This is a triage flag, not a diagnosis, and it is the main
reason the tool has no `--fix` mode.

#### Tier 3a — reference identity (does the data match the declared reference?)

Where `reference.source` resolves to an NCBI accession — all 79 currently do —
fetch `*_assembly_report.txt`, which gives the sequence table with
GenBank↔RefSeq accessions, lengths, and sequence roles. Where it does not
resolve, fall back to internal consistency and emit `F010`.

The name comparison is **asymmetric**, because the two directions mean different
things:

| ID | Severity | Check |
|---|---|---|
| `F001` | error | every VCF contig appears in the assembly report — an unknown contig means the reference is not what the config claims |
| `F002` | error | every VCF contig length equals the assembly length |
| `F003` | error | contig names use one consistent naming scheme, not a GenBank/RefSeq mix |
| `F004` | error | BAM `@SQ` names and lengths equal the VCF contigs |
| `F005` | error | all BAMs share an identical `@SQ` list |
| `F006` | warn | reference basename in the BAM's `@PG bwa` command line matches `reference.name` |
| `F007` | warn | NCBI organism name is consistent with the species directory slug |
| `F008` | warn | `qc/contig_map.tsv` `original_contig` values agree with VCF contigs |
| `F009` | warn / info | assembly sequences absent from the VCF — warn when missing bases exceed `--missing-contig-threshold` (default **1.0%** of assembly bases), info below |
| `F010` | warn | `reference.source` is not an NCBI accession — reference identity only partially verified |
| `F011` | warn | a missing sequence is an `assembled-molecule` (a whole chromosome), at any fraction |

`F001` and `F002` are the wrong-genome detectors: a different assembly of the
same species reuses naming conventions but not lengths.

`F009` and `F011` cover the reverse direction, where absence is expected in
principle — snpArcher's interval logic could legitimately drop small contigs — so
it is a threshold warning rather than an error. **Measured across all 67 species
with a published VCF, the VCF contig set exactly equals the assembly sequence
set: zero unknown contigs, zero length mismatches, zero missing bases, all
GenBank naming.** No contig filtering happens in practice today, so the 1%
default is headroom for future runs rather than a noise filter, and `F011` — any
whole chromosome missing — is a strictly more sensitive detector that is also
clean today. Both are worth having.

There are no `M5` checksum tags in the `@SQ` lines, so identity rests on
name+length agreement rather than sequence checksums. The report should say so
explicitly, so the guarantee isn't overread.

#### Tier 3b — reference canonicality (is it the *right* reference?)

Tier 3a proves the data is self-consistent with whatever `config.yaml` declares.
It cannot tell you the config declares the wrong assembly. The VGP list can.

| ID | Severity | Check |
|---|---|---|
| `F020` | error | `reference.source` is neither the VGP main-haplotype accession nor its GCA/GCF counterpart — a different assembly entirely |
| `F021` | error | `reference.source` is the GCA/GCF counterpart of the VGP accession — same assembly, non-canonical accession form |
| `F022` | warn | species has no entry in the VGP list |
| `F023` | warn | VGP `ScientificName` inconsistent with the species directory slug and `reference.name` |
| `F024` | warn | NCBI `tax_id` for the accession differs from the VGP `QID` (taxid) column |

`F021` is an error rather than a warning because the VGP list is now an
authority and the fix is mechanical and unambiguous — normalize to the listed
accession. Note this supersedes the earlier judgement, made before the list
existed, that a GCA/GCF flip could only be a warning. `G002` remains a *warning*
and is not redundant with `F021`: it is the observation about where the data was
actually found, which the report needs in order to explain itself, whereas `F021`
is the normative claim about the config.

These five checks are cheap — one CSV read, one cached datasets-API call — and
they caught two real errors on the first pass.

#### Tier 4 — config vs recorded provenance (warnings)

The VCF header preserves the actual GATK invocations, so the config is checkable
against what really ran. All warnings: `config.yaml` is a record of intent that
may legitimately have been edited after a run.

| ID | Severity | Check |
|---|---|---|
| `P001` | warn | `##GATKCommandLine … --sample-ploidy` == `variant_calling.ploidy` |
| `P002` | warn | `--heterozygosity` == `variant_calling.gatk.het_prior` |
| `P003` | warn | caller in the header is consistent with `variant_calling.tool` |

#### Tier 5 — external accessions (opt-in, `--check-sra`)

| ID | Severity | Check |
|---|---|---|
| `E001` | error | each `srr` run resolves via ENA to the `sample_id` biosample |
| `E002` | warn | every run's study is listed among the README bioprojects |
| `E003` | warn | a README bioproject contributes no runs to the sheet |

ENA's `filereport` endpoint is bulk-queryable by bioproject, so this costs one
request per bioproject rather than one per run.

### Report format

Rendered three ways from the same `Finding` list:

- **human** — grouped by species then severity, one-line summary per species in
  batch mode, `SKIPPED` counts collapsed
- **`--json`** — stable field names, for dashboards and diffing between runs
- **`--github`** — `::error file=…,line=…::` annotations so CI comments land on
  the offending line of `sample_sheet.csv` or `config.yaml`

Exit codes: `0` clean or warnings only, `1` any error, `2` tool failure (network,
unparseable input). `--strict` promotes warnings to exit `1`.

### CLI

```
congen validate species/reptiles/podarcis-raffonei     # one species
congen validate --all                                  # batch, all 79
congen validate --all --clade birds
congen validate --changed origin/main                  # only dirs changed vs a ref (CI)
congen validate --list-checks
congen validate ... --json report.json --strict --check-sra --only S,F --skip E001
```

`--metadata-root` defaults to a sibling `congen-metadata` checkout, overridable
by env var so CI can point at its own path.

### Caching and performance

Disk cache under `~/.cache/congen/` holding NCBI assembly reports keyed by
accession, S3 listings keyed by prefix with a short TTL, and parsed headers keyed
by object **ETag** — so a re-upload invalidates naturally.

Budget per species: one delimited listing per subdirectory, one VCF header read,
*n* BAM header reads. Full-corpus BAM header reads dominate, so default to
sampling — first BAM plus a random few for `F005` — with `--all-bams` for an
exhaustive pass. At 6–8 way concurrency a full-corpus run lands in a couple of
minutes.

Be a good citizen: bounded concurrency, backoff on 5xx/503 and connection resets,
descriptive User-Agent on NCBI and ENA calls.

## Part 3 — `congen readme` (sketch)

The second tool. Generates a richer `README.md` per species, extending today's
minimal `README.txt` with sample counts, coverage range, citations, and more.
Sketched here only far enough to confirm what it demands from core — the tool
itself needs its own design pass.

**What it needs that core doesn't have yet:**

- **`remote/qc.py`** — the snpArcher QC tables are plain TSVs and carry most of
  what the readme wants:

  | File | Provides |
  |---|---|
  | `qc/individuals.idepth` | `MEAN_DEPTH` per sample → **coverage range** |
  | `callable_sites/coverage_thresholds.tsv` | `cohort_mean_coverage`, `min_coverage`, `max_coverage` |
  | `qc/individuals.het` | `F` (inbreeding coefficient) per sample |
  | `qc/individuals.imiss` | `F_MISS` per sample |
  | `qc/individuals.samps.txt` | sample list (already used by `S005`) |

- **`metadata/writers.py`** with round-trip YAML — this is why it moves into
  milestone 1 rather than staying a placeholder.
- **`remote/literature.py`** — bioproject/study → publication, for citations.
  Europe PMC or NCBI elink; needs a design decision about what counts as the
  citable reference for a dataset.

**Three decisions this tool forces, worth settling before it is built:**

1. **`README.md` alongside or instead of `README.txt`?** 57 species have a
   `README.txt` today, and the validator's `R020`/`R021` and `E002`/`E003` read
   it. If `README.md` supersedes it, those checks must follow, and the `.txt`
   files should be removed in the same change rather than left to rot as a second
   stale source of truth.
2. **Fully generated, or generated sections inside a hand-written file?** If any
   species will ever carry hand-written prose, use explicit managed-block markers
   (`<!-- congen:begin stats -->` … `<!-- congen:end stats -->`) and only ever
   rewrite between them. Retrofitting that after hand edits exist is painful.
3. **Determinism.** Output must be byte-identical across runs given the same
   inputs — stable ordering, no timestamps, rounded numbers pinned — or CI will
   see a diff on every run. Pair the tool with `--check` (exit non-zero if
   regenerating would change anything), which is what makes it CI-usable.

## Milestones

1. **`core.http`, `core.remote.headers`, `core.metadata` loaders + `vgp.py`,
   `core.metadata.writers`, fixtures.** The risky primitives, proven in
   isolation. `writers.py` is here rather than later because `readme` is next and
   needs it. Record fixtures from `podarcis-raffonei` (clean), `anser-albifrons`
   (sample mismatch), `sturnus-vulgaris` (wrong reference, weird config),
   `grus-americana` (non-canonical accession, no VCF).
2. **`findings.py` + registry + `report/`, then validate tiers 0–2 and 3b.**
   Tier 3b is pulled forward because it needs only the VGP CSV and one cached API
   call, and it already catches two real errors. Reproduces the baseline below.
3. **Tier 3a** with the NCBI assembly-report cache and the internal-consistency
   fallback.
4. **Tier 4, batch mode, orphan detection (`G020`/`G021`), CI workflow.**
5. **Tier 5** behind `--check-sra`, and `core.remote.qc` in support of `readme`.

Regression-test milestone 2 against the baseline: the counts below are the
expected output, and any change to them should be explained.

## Baseline — findings as of 2026-09-08

Measured by prototype across all 79 species.

| Species | Finding |
|---|---|
| `birds/sturnus-vulgaris` | **`F020`** — config declares `GCF_001447265.1`; the VGP main-haplotype assembly is `GCA_052056855.1`. Not a GCA/GCF variant — a different assembly entirely. Also `G003` (no data published yet), so nothing has been run against the wrong reference. This was an open question in the previous draft; the VGP list answers it. |
| `birds/grus-americana` | **`F021`** — config declares `GCF_028858705.1`; VGP and S3 both use `GCA_028858705.1`, so the config is the non-canonical form. Also `G002` (data found under the counterpart accession) and `G010` (BAMs uploaded, no VCF). |
| `birds/anser-albifrons` | `S001`/`S002` — `SAMEA112262514` is in the BAMs and VCF but absent from the sheet. The row was evidently dropped after the run; the S3 copy of the sheet is identically wrong, so the VCF is the only witness. Direction of the fix needs human review. |
| `mammals/panthera-onca` | `G010` — `GCA_046562875.2` has BAMs but no VCF. |
| — | `G020` — `GCA_028023285.1` (*Balaenoptera ricei*) is on S3 and in the VGP list, but has no repo species directory. |
| 10 species | `G003` — no data published yet: `notamacropus-eugenii`, `macrotis-lagotis`, `taeniopygia-guttata`, `arvicola-amphibius`, `caprimulgus-europaeus`, `haliaeetus-albicilla`, `myotis-nattereri`, `coregonus-lavaretus`, `astatotilapia-calliptera`, `sturnus-vulgaris`. |
| 13 species | `G016` — `filtered.vcf.gz` present. Informational only. |
| 77 species | Clean on canonicality — `reference.source` is exactly the VGP main-haplotype accession. |
| 66 species | Clean on sample identity — sheet, BAMs, and VCF agree exactly. |
| 67 species | Clean on reference identity — every VCF contig set exactly equals its assembly's sequence set, with matching lengths and consistent GenBank naming. |

Corpus coverage: the VGP list holds 124 species, the repo 79, so 47 listed
species have no repo directory yet. That is expected backlog, not a finding —
`G020` fires only where data exists on S3 without repo metadata.

## Open questions

- **`README.md` vs `README.txt`** — decision 1 in Part 3. It changes four
  validator checks, so worth settling before milestone 2 rather than after.
- **What is the citable reference for a dataset?** The bioproject, the assembly
  paper, the paper that generated the reads, or all three. Shapes
  `remote/literature.py` and whether it needs Europe PMC at all.
- **Should `F022`** (species absent from the VGP list) **ever be an error?**
  Currently a warning, and it fires on nothing — all 79 repo species are listed.
  If every congen species must by definition be a VGP species, error is more
  honest.
