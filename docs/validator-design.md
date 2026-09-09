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
      status.py                  PublicationState, UploadStatus
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
        sra.py                   NCBI SRA runinfo (run → biosample, bioproject)
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
| `remote/sra.py` | `E001`–`E003`, `R017` | run counts, study titles | sheet builder |
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
renumber — `CheckRegistry.retire()` records withdrawn IDs and refuses to
re-register one, so a suppression can never silently start hiding a different
check.

### Findings and status are different questions

`core.status` carries a second, parallel output: how far along a species'
publication is.

```python
UploadStatus(subject, state, accession, declared_accession,
             present, missing, n_sheet_samples, n_bams, n_vcf_samples)
PublicationState = absent | partial | complete
```

The split exists because **findings answer "what is wrong?" and status answers
"how far along is this?"** An earlier version answered the second in the
vocabulary of the first, and no severity ever fit: "no data published yet" is not
a warning, and an absent optional artifact is not even informative as a
per-species line. Five checks were withdrawn into this model —
`G002` (data found under the counterpart accession), `G003` (nothing published),
`G015` (missing subdirectories), `G016` (`filtered.vcf.gz` present) and `R021`
(no `README.txt`) — which removed 104 of 177 findings, all of them descriptions
rather than defects.

Required artifacts are `raw_vcf`, `raw_vcf_index`, `bams`, `qc` and
`callable_sites`. `filtered_vcf`, `published_readme` and `repo_readme` are
optional on GenomeArk today, so they never affect `state`. Optional does not mean
uninteresting: each one can be supplied, so both reports name *which* species are
missing which rather than only counting them. Currently 56 species have no
`filtered_vcf`, 12 no `repo_readme`, and `panthera-onca` no `published_readme`.

Nothing in `core.status` carries a severity or a policy, which is what lets the
validator, a future status report and the readme generator share it.

**It also fixed a real false positive.** `S002` compares the sample sheet with
the published BAMs — but during a partial upload the BAM set is by definition not
final, so the comparison measures how far the upload got rather than whether the
metadata is right. `grus-americana` (57 samples in the sheet, 42 BAMs, no VCF)
was reported as a metadata mismatch until `state` existed to tell the two apart.
`S002` and `S003` now run only when the publication is complete. That guard is
not expressible through `needs`, which knows whether a slice was fetched but not
what it means.

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

**Every finding must name something that can be fixed.** A validation report
concerns itself with inconsistency, not inventory: if there is no action that
clears a finding, it is documentation and does not belong here — not even at
INFO, because an INFO nobody can ever resolve is permanent noise that teaches
readers to ignore the category.

Four were withdrawn under this rule. `P010` stamped the pipeline versions, which
have no remedy. `F010` reported that a check could not run, which is what SKIPPED
already says. `F007` became reachable only alongside an `F022` error once that
became an error. And `R010` no longer notes a trailing blank row, because the
loader skipping it is the right answer rather than something to act on.

Two INFO-level findings survive the rule because both *are* actionable: `R017`
on a single-run experiment accession (replace the `SRX` with the `SRR` it
contains, which removes any future ambiguity) and `F009` below the threshold
(silent truncation is worth knowing about, and can be resolved by rerunning
without filtering).

Inventory has a home — the publication status block, and later the readme
generator. The distinction is not that inventory is unimportant; it is that a
verdict and a stock-take answer different questions.

#### Tier 0 — repo self-consistency (no network)

| ID | Severity | Check |
|---|---|---|
| `R001` | error | `config.yaml` parses; has `reference`, `samples`, `variant_calling` |
| `R002` | error | `reference.source` is a GCA/GCF accession, URL, or path |
| `R003` | warn | `reference.name` looks like a species name, not an accession |
| `R010` | error | sheet parses; required columns `sample_id,input_type,input` present |
| `R011` | error | no empty or whitespace-only `sample_id` |
| `R012` | error | `input_type` in `{srr, fastq, bam}` |
| `R013` | error | for `input_type: srr`, `input` is an SRA run or experiment accession |
| `R014` | error | duplicate `(sample_id, input)` pairs — a genuinely repeated run |
| `R015` | warn | `input` is a local filesystem path — not reproducible |
| `R016` | warn | `sample_id` is not a BioSample accession (`SAMN`/`SAMEA`/`SAMD`) |
| `R020` | warn | `README.txt` accession matches `reference.source` |

`R014` is scoped to *pairs*: a repeated `sample_id` alone is expected.

**`R013` accepts experiment accessions.** An earlier draft required
`[SED]RR[0-9]+`, which fired on 42 rows in `birds/anser-anser` — the one species
whose sheet uses `SRX`/`ERX`. Those are SRA *experiment* accessions; the download
tooling resolves them, so they are not invalid. Whether a given experiment is
*ambiguous* is a separate question that cannot be answered offline, so it belongs
to `R017` in tier 5.

#### Tier 1 — completeness on GenomeArk

| ID | Severity | Check |
|---|---|---|
| `G001` | error | accession prefix exists on S3 but published no objects |
| `G010` | warn | `vcfs/raw.vcf.gz` present |
| `G011` | error | `vcfs/raw.vcf.gz.tbi` present |
| `G012` | error | `bams/` non-empty |
| `G013` | error | every `bams/*.bam` has a matching `.csi` |
| `G014` | error | no zero-byte objects in the data directories |
| `G020` | warn | S3 accession is in the VGP list but has no repo species directory |
| `G021` | warn | S3 accession is in neither the repo nor the VGP list — stray data |

Completeness is not answered here any more. Whether a species has data at all,
which optional artifacts exist, and which accession the data sits under are
descriptions, so they live in the status block (see Part 1). What remains are the
checks that describe something actually broken.

**`G010` is a warning, not an error.** An upload with BAMs and no VCF may simply
be mid-flight, and the tool cannot know whether it was supposed to have finished,
so an error overclaims. It stays a finding because an upload that started and
stopped is worth surfacing; `state: partial` explains the rest.

**`G011` checks presence only.** An earlier draft also required the index to be
no older than the VCF. That cannot work: S3 `LastModified` records upload order,
not generation order, and `birds/hirundo-rustica`'s index is stamped one second
before its VCF purely because that is the order they were pushed. The comparison
was removed rather than given a fudge factor — it cannot distinguish a stale
index from a normal upload.

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
| `F008` | warn | `qc/contig_map.tsv` `original_contig` values agree with VCF contigs |
| `F009` | warn / info | assembly sequences absent from the VCF — warn when missing bases exceed `--missing-contig-threshold` (default **1.0%** of assembly bases), info below |
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

Three things settled while building this tier:

**The assembly-report path is constructed, not scraped.** The prototype found
NCBI's FTP directory by parsing the directory listing HTML. That is unnecessary:
the directory name is the accession plus the assembly name — already fetched with
the dataset report — with every run of characters outside `[A-Za-z0-9._-]`
collapsed to a single `_`. Verified against all 79 accessions; 77 need no
sanitizing, and the rule was checked against the two that do
(`mEubGla1.1.hap2.+ XY` → `mEubGla1.1.hap2._XY`, `mPanOnc1 haplotype 2` →
`mPanOnc1_haplotype_2`). One request instead of two, and no HTML parsing. A
listing scrape survives as a fallback in case the rule ever shifts.

**`F009` and `F011` stay quiet when the contig set is incoherent.** Both measure
what the assembly has that the VCF lacks, which is only meaningful once the VCF's
own names are accounted for. Against the wrong assembly `F009` reads "100% of
bases missing", and under mixed naming it reads whatever fraction used the other
scheme — artifacts of what `F001` or `F003` already reported. Suppressing them
there leaves one root cause standing instead of three findings describing it.

**`F005` says how many BAMs it looked at.** Only a few BAM headers are read by
default, so "all BAMs share one `@SQ` list" is really "the BAMs examined agree".
The finding says `sampled` or `all` explicitly rather than letting a reader
assume the stronger claim.

#### Tier 3b — reference canonicality (is it the *right* reference?)

Tier 3a proves the data is self-consistent with whatever `config.yaml` declares.
It cannot tell you the config declares the wrong assembly. The VGP list can.

| ID | Severity | Check |
|---|---|---|
| `F020` | error | `reference.source` is neither the VGP main-haplotype accession nor its GCA/GCF counterpart — a different assembly entirely |
| `F021` | error | `reference.source` is the GCA/GCF counterpart of the VGP accession — same assembly, non-canonical accession form |
| `F022` | error | species has no entry in the VGP list |
| `F023` | warn | VGP `ScientificName` inconsistent with the species directory slug and `reference.name` |
| `F024` | warn | NCBI `tax_id` for the accession differs from the VGP `QID` (taxid) column |

`F021` is an error rather than a warning because the VGP list is now an
authority and the fix is mechanical and unambiguous — normalize to the listed
accession. Note this supersedes the earlier judgement, made before the list
existed, that a GCA/GCF flip could only be a warning. The observation about where
the data was actually found is now a status fact (`accession` and
`accession_differs`) rather than the `G002` warning it used to be, leaving `F021`
as the single normative claim about the config.

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

**Caller detection looks for `bcftools_call`, never bcftools in general.**
snpArcher merges its per-interval VCFs with `bcftools concat`, so every
GATK-called VCF in the corpus carries `##bcftools_concatCommand`. Reading that as
the caller would mislabel all 67 of them. `P003` also treats GATK evidence as
consistent with a `parabricks` config, since parabricks emits GATK-compatible
headers, and stays silent on a header it does not recognize — absence of evidence
is not evidence of a different caller.

`P010` used to record the pipeline versions here, and was withdrawn — see the
rule below.

#### Tier 5 — external accessions (opt-in, `--check-sra`)

| ID | Severity | Check |
|---|---|---|
| `E001` | error | each `srr` input resolves to the `sample_id` biosample |
| `E002` | warn | every run's bioproject is listed in `README.txt` |
| `E003` | warn | a `README.txt` bioproject contributes no runs to the sheet |
| `R017` | error / info | an `srr` input names an SRA experiment: **error** if it expands to more than one run, **info** if it expands to exactly one |

**NCBI, not ENA.** An earlier draft specified ENA's `filereport`, chosen during
prototyping for convenience. Tested side by side, the two agree exactly —
`SRR28065797` → `SAMN39984924`, `ERR519283` → `SAMEA2554516`, `DRR191146` →
`SAMD00156790`, with matching bioprojects — so coverage is not a differentiator;
both mirror INSDC, DDBJ `DRR` accessions included. NCBI wins on three other
grounds:

* **One provider.** The tool already depends on NCBI for the datasets API and the
  FTP assembly reports. A second service for one tier doubles what can be down
  and doubles where to look when it is.
* **Authority of record** for the identifiers the sheets actually use — `SAMN`,
  `PRJNA`.
* **`runinfo` returns `Experiment` beside `Run`**, so `R017` costs nothing extra.

Two objections to NCBI did not survive testing. `runinfo` is not 47 positional
columns: it has a header row, and `csv.DictReader` reads `Run`, `Experiment`,
`BioSample` and `BioProject` by name. And the rate limit is a pacing problem, not
a blocker — unpaced, 3 of 12 requests were refused; paced under the 3/s anonymous
cap, 12 of 12 succeeded. `core.http` owns a per-host limiter, and
`$NCBI_API_KEY` raises the cap to 10/s when set.

**Batching keeps it cheap.** Querying 3,761 runs one at a time at 3/s would take
twenty minutes. `esearch` accepts run accessions OR'd together and `efetch`
accepts the resulting UID list by POST, so a batch of ~150 accessions costs two
requests. The whole corpus is roughly 50 requests, well under a minute.

`R017` moved here from tier 0. Offline it could only ever say "this *might* be
ambiguous", which is the kind of speculation this tool avoids elsewhere. With
`runinfo` the question is answerable: all 42 experiment accessions in
`birds/anser-anser` expand to exactly one run each, so they are informational
rather than a problem. An experiment holding several runs genuinely fails to
identify which reads were used, and that is an error.

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
descriptive User-Agent on every NCBI call, and the per-host rate limiter for
eutils.

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

1. **Additional to `README.txt`, not a replacement — settled.** The baseline
   `README.txt` stays, and stays authoritative; the generated document is a
   richer sibling. So the validator's `R020` and `E002`/`E003` keep reading
   `README.txt` unchanged, and there is no migration to sequence. Its presence
   is recorded as an optional status artifact rather than a finding.

   The generated file's **name is not settled**, and may deliberately avoid
   `README.md` to prevent confusion. Two consequences for the implementation:
   the name lives in exactly one constant (`congen.tools.readme.OUTPUT_NAME`)
   with a CLI override, so changing it later is a one-line edit; and
   `core.metadata` keeps only the `README.txt` constant, since core has no
   business knowing about a tool's output.

   One concrete argument against `README.md` specifically: GitHub prefers
   `README.md` over `README.txt` when both are present, so adding it would hide
   the baseline file from every directory listing in the web UI. If `README.txt`
   is to remain the primary document, the generated one wants a different name —
   `DATASET.md` reads as a sibling with its own purpose rather than a competing
   readme. If the intent is the opposite, that the rich document should be what
   people see first, `README.md` gets that for free.
2. **Fully generated, or generated sections inside a hand-written file?** If any
   species will ever carry hand-written prose, use explicit managed-block markers
   (`<!-- congen:begin stats -->` … `<!-- congen:end stats -->`) and only ever
   rewrite between them. Retrofitting that after hand edits exist is painful.
3. **Determinism.** Output must be byte-identical across runs given the same
   inputs — stable ordering, no timestamps, rounded numbers pinned — or CI will
   see a diff on every run. Pair the tool with `--check` (exit non-zero if
   regenerating would change anything), which is what makes it CI-usable.

## Part 4 — validation reports

**The report is documentation, not a CI artifact.** Someone deciding whether to
rely on a species' metadata has to see the answer at a glance, in the species
directory. That rules out git history, diffs, pull requests and issues as the
mechanism: all of them are invisible to the person who just opened
`species/birds/foo/` and wants to know whether they can trust it.

This reverses the read-only decision in Part 2, and only for this: writing stays
behind `--write-reports`, so a bare `congen validate` remains read-only and safe
in CI. `writes_metadata` on the tool registration comes to mean *capable of
writing* rather than *always writes*.

### What a validation actually claims

A validation is not a health check that expires with age. It is a claim about a
specific pair of inputs — *this* metadata, against *that* published data. The
claim stays true until one of those inputs changes. So the default is not
"revalidate everything nightly" but "revalidate what is stale", and staleness is
a property of the inputs, not of the calendar.

### Layout

```
species/{clade}/{slug}/
    VALIDATION.md          generated — the at-a-glance verdict
    validation.json        generated — machine-readable, carries the input digests
VALIDATION.md              generated — the corpus table
validation.json            generated — machine-readable corpus summary
```

Top level of the species directory rather than a `validation/` subdirectory:
discoverability beats tidiness here, and burying the one file a consumer needs
defeats its purpose. Uppercase for the human document, lowercase for the data,
matching the existing `README.txt` / `config.yaml` split.

Every species gets a `VALIDATION.md`, including ones with no published data, so
the absence of the file is never ambiguous.

### Staleness must be content-based

**Git does not preserve mtimes.** A fresh clone stamps every file with checkout
time, so "config.yaml is newer than the report" is unreliable in exactly the
places that matter — CI, and anyone else's machine. Comparing against `git log`
would work but needs a git checkout and a subprocess per file.

So a report records digests of what it validated, and staleness is a pure
function of the current inputs:

```json
"validated": {
  "at": "2026-09-08T14:23:11Z",
  "tool_version": "0.1.0",
  "catalog": "sha256:...",
  "inputs": {
    "config.yaml":      "sha256:...",
    "sample_sheet.csv": "sha256:...",
    "README.txt":       "sha256:..."
  },
  "data": {
    "accession":  "GCA_027172205.1",
    "raw.vcf.gz": "etag:9a6aba769820...",
    "bams":       {"count": 21, "digest": "sha256:..."}
  }
}
```

The data-side digests are free: S3 listings already return ETags and
`core.remote.genomeark` already parses them.

`catalog` is a digest of the sorted `(check_id, severity)` pairs, not the release
version. A report should be invalidated when the *checks* change — one added, one
re-severitied — but not by a bugfix release that would leave every verdict
identical. A catalog change does invalidate the whole corpus, which is correct
and affordable: a full run is ~85s.

### A species needs revalidation when

1. it has no report;
2. an input digest no longer matches (metadata edited);
3. a data digest no longer matches, or data has appeared for a species that had
   none — one bucket listing covers the whole corpus;
4. the check catalog changed;
5. the last report recorded errors or warnings, so the fix can be noticed.

### Report states

| State | Meaning |
|---|---|
| `PASS` | validated, no errors or warnings |
| `PASS WITH WARNINGS` | validated, warnings only |
| `FAIL` | errors found |
| `PENDING` | no data published yet; metadata checks pass. **Expected, not a failure** — pushing a config and sheet before the run exists is normal |
| `STALE` | inputs changed since the last validation; the recorded verdict no longer describes the current files |

The first three lines of `VALIDATION.md` carry the whole verdict, because that is
all most readers will get to:

```markdown
# Validation — Podarcis raffonei

**PASS** · 2026-09-08 · GCA_027172205.1
21 samples · sheet, BAMs and VCF agree · reference confirmed against NCBI
```

A stale report keeps its old result but cannot be mistaken for current:

```markdown
# Validation — Podarcis raffonei

**STALE** · metadata changed after the last validation (2026-09-08)
The verdict below described `config.yaml` and `sample_sheet.csv` as they were;
they have changed since. Revalidation pending.

<details><summary>Superseded result — PASS, 2026-09-08</summary>
...
</details>
```

### Who writes, and when

**Pull requests never validate against GenomeArk.** A config and sample sheet
routinely land before the data does, so a validating PR check would fail on a
perfectly normal state. What a PR can do is offline and always meaningful:

* run tier 0, which needs no network;
* **stamp `STALE` immediately** on any report whose inputs the PR changed.

Stamping at PR time rather than waiting for the nightly is deliberate: it closes
the window in which a consumer could read `PASS` from a report that no longer
describes the files next to it.

`congen validate --mark-stale` does the stamping — offline, instant, no network —
and `--check-stale` exits non-zero listing any report that needs it.

> **Deferred.** How the stamp gets into a pull request is not decided. CI running
> `--check-stale` and failing with the command to run works for forks and needs
> no write token; a bot running `--mark-stale` and committing to the branch is
> less friction but fails silently on fork PRs, where `GITHUB_TOKEN` is
> read-only. The tool supports either. Nothing in `congen-metadata-tools`
> depends on the answer, so it can wait until the CI workflows are written.

Note that `--check-stale` and `--mark-stale` deliberately **ignore the check
catalog**. A catalog change means a species should be *revalidated*, which is
`--stale`'s business, but it does not make an existing report describe the wrong
files — and including it would make the offline check's result depend on whether
`--check-sra` was passed.

**The nightly** runs `--stale --write-reports`: on a quiet corpus that is zero
species and near-zero cost. `--all` stays available as a periodic audit.

### Corpus summary

Root `VALIDATION.md`: counts by state, a one-line-per-species table linking to
each report, corpus-level findings such as `G020`, the run timestamp and the
catalog digest. Kept as its own file rather than folded into the root `README.md`
for now; incorporating it later is a one-line include.

`congen readme` can likewise pull the species verdict into the generated README
when it exists, which keeps the two tools decoupled — the validator owns the
verdict, the readme tool decides whether to surface it.

The terminal summary for `--all` grows the same table; the current one-line
summary is too thin for a batch run.

### The four modes

```bash
congen validate <species> --write-reports    # validate one species, write its report
congen validate --all     --write-reports    # force: revalidate everything
congen validate --stale   --write-reports    # only what needs it (the nightly)
congen validate --check-stale                # offline: which reports are out of date
congen validate --mark-stale                 # offline: stamp them STALE
```

"Force rerun everything" needs no flag of its own: `--all` already means every
species and `--stale` is the filter.

### Implementation notes

* `core/report/markdown.py` renders both documents, and is the renderer
  `congen readme` will reuse.
* `core/validation_record.py` owns the digest computation, the record schema and
  the staleness predicate — shared by the validator and, later, any status tool.
* `writers.atomic_write` and `write_if_changed` already exist for this.
* Reports are fully generated, so no managed blocks: unlike a README, nothing in
  them is hand-written.
* A partial check selection is recorded in the report ("1 of 45 checks — **a
  partial selection**") so a filtered report cannot be read as a full one.
* The corpus table is assembled from every species' `validation.json` on disk
  rather than from the run, so it stays complete and accurate after a `--stale`
  run that touched only a handful of species.
* A `STALE` report renders its old findings **only** inside the collapsed
  superseded block. Leaving them inline would defeat the stamp: the point is
  that the old verdict must not read as current.

## Milestones

1. **Done.** `core.http`, `core.remote.headers`, `core.metadata` loaders +
   `vgp.py`, `core.metadata.writers`, fixtures. The risky primitives, proven in
   isolation. `writers.py` is here rather than later because `readme` is next and
   needs it. Record fixtures from `podarcis-raffonei` (clean), `anser-albifrons`
   (sample mismatch), `sturnus-vulgaris` (wrong reference, weird config),
   `grus-americana` (non-canonical accession, no VCF).
2. **Done.** `findings.py` + registry + `report/`, `core.remote.genomeark`,
   `core.remote.ncbi`, the `congen` dispatcher, and validate tiers 0–2 and 3b.
   Tier 3b is pulled forward because it needs only the VGP CSV and one cached API
   call, and it already catches two real errors. Reproduces the baseline below.
3. **Done.** Tier 3a, with the NCBI assembly-report cache, `core.remote.qc`
   for `contig_map.tsv`, and the `F010` fallback for a non-NCBI reference.
4. **Done.** Tier 4. Batch mode and orphan detection (`G020`/`G021`) landed in
   milestone 2.
5. **Done.** Tier 5 behind `--check-sra`, on NCBI SRA, plus the per-host rate
   limiter in `core.http`.
6. **Done.** Validation reports (Part 4): the record schema and staleness
   predicate, the markdown renderer, and the four modes.
7. **CI workflows** for `congen-metadata` — deferred, along with the question of
   how the `STALE` stamp reaches a pull request. The tool supports either
   answer.
8. **`congen readme`** (Part 3), which reuses the markdown renderer and grows
   `core.remote.qc` into the coverage tables.

Regression-test milestone 2 against the baseline: the counts below are the
expected output, and any change to them should be explained.

## Baseline — findings as of 2026-09-08

Produced by `congen validate --all` over all 79 species: **4 errors and 7
warnings across 6 subjects**, with 68 informational findings. A full-corpus run
takes ~40s at 6-way concurrency. These counts are the regression target; any
change to them should be explained.

Moving publication state out of the finding stream cut this from 7 errors, 18
warnings and 159 informational findings — 104 of those were descriptions, not
defects.

### Errors

| Species | Finding |
|---|---|
| `birds/sturnus-vulgaris` | **`F020`** — config declares `GCF_001447265.1` (`Sturnus_vulgaris-1.0`, Scaffold); the VGP main-haplotype assembly is `GCA_052056855.1`. Not a GCA/GCF variant — a different, older, scaffold-level assembly. Nothing has been run against it: `state: absent`. |
| `birds/grus-americana` | **`F021`** — config declares `GCF_028858705.1`; VGP and GenomeArk both use `GCA_028858705.1`, confirmed by NCBI `paired_assembly` as the same assembly. |
| `birds/anser-albifrons` | **`S001`** and **`S002`** — `SAMEA112262514` is in the BAMs and the VCF but absent from the sheet. The published copy of the sheet is identically wrong, so the VCF is the only witness. Neither finding asserts a direction. |

### Warnings

| Subject | Finding |
|---|---|
| `birds/grus-americana` | `G010` — 42 BAMs, no VCF. `state: partial`. |
| `mammals/panthera-onca` | `G010` — 35 BAMs, no VCF. `state: partial`. |
| `birds/sturnus-vulgaris` | `R003` — an accession in `reference.name`. |
| `birds/anser-anser` | `R017` — 42 inputs across 40 samples are SRA experiment accessions. The only species using `SRX`/`ERX`, and it is nearly the whole sheet. |
| `reptiles/shinisaurus-crocodilurus` | `R015` (one input is a local scratch fastq path) and `S006` (the published sheet differs from the repo copy — same samples, different rows). |
| `<corpus>` | `G020` — `GCA_028023285.1` (*Balaenoptera ricei*) is published and in the VGP list, but has no species directory. |

### Publication status

```
complete   67
partial     2   grus-americana, panthera-onca
absent     10   caprimulgus-europaeus, haliaeetus-albicilla, sturnus-vulgaris,
                taeniopygia-guttata, astatotilapia-calliptera, coregonus-lavaretus,
                arvicola-amphibius, macrotis-lagotis, myotis-nattereri,
                notamacropus-eugenii
optional:  filtered_vcf 13/69, published_readme 68/69, repo_readme 57/69
```

`filtered_vcf` is 13 of 69 rather than 14: fourteen accessions publish one, and
the fourteenth is the *Balaenoptera ricei* orphan with no species directory.

### Tier 5 (`--check-sra`)

Opt-in, so not part of the default counts above. Over the whole corpus it takes
~85s and adds:

| ID | Result |
|---|---|
| `E001` | **clean** — all 3,761 run accessions belong to the biosample the sheet claims |
| `E002` | 3 species draw runs from a bioproject their README does not cite: `birds/dryobates-pubescens` (`PRJNA1462765`), `fishes/cyclopterus-lumpus` (`PRJNA1462766`), `mammals/sus-scrofa-domesticus` (`PRJEB71922`) |
| `E003` | 2 species cite a bioproject contributing no runs: `dryobates-pubescens` (`PRJNA561991`), `cyclopterus-lumpus` (`PRJNA562003`) |
| `R017` | 1 info — `birds/anser-anser`'s 42 experiment accessions each hold exactly one run |

**The `E002`/`E003` pairs are one mistake, not two.** For both
`dryobates-pubescens` and `cyclopterus-lumpus` the README cites the *RefSeq
genome assembly* BioProject (`PRJNA561991`, `PRJNA562003`), which by definition
contributes no reads, while the reads sit under a separate *raw sequence reads*
BioProject (`PRJNA1462765`, `PRJNA1462766`). Both checks fire because the project
that has the runs is undocumented and the documented one has none. The tool
reports the two facts and does not try to pair them, since inferring a
substitution would be a guess.

`sus-scrofa-domesticus` is the different case: eight documented projects, all
contributing, plus one that is not documented.

### Silent checks

31 of 41 default checks fire on nothing: all of tier 3a, all of tier 4's
comparisons, and most of tier 1's integrity checks. Tier 5 adds `E001`, also
clean. That is good news about the data, but it means
those checks are load-bearing only through their negative controls in the test
suite — `tests/test_tier3a.py` and `tests/test_tier4.py` exist for exactly that
reason.

The corpus is clean on: reference identity (every VCF contig set exactly equals
its assembly's sequence set, matching lengths, consistent GenBank naming, BAM
`@SQ` agreeing throughout), provenance (all 67 published VCFs record GATK 4.6.2.0
with `--sample-ploidy 2` and `--heterozygosity 0.005`, matching every config), and
canonicality for 77 of 79 species.

Corpus coverage: the VGP list holds 124 species, the repo 79, so 47 listed
species have no repo directory yet. That is expected backlog, not a finding —
`G020` fires only where data exists on GenomeArk without repo metadata.

## Open questions

- **What to call the generated document.** Settled that it is additional to
  `README.txt`; the name is not. See decision 1 in Part 3 — it is one constant,
  so it need not block anything.
- **What is the citable reference for a dataset?** The bioproject, the assembly
  paper, the paper that generated the reads, or all three. Shapes
  `remote/literature.py` and whether it needs Europe PMC at all.
- **Should `F022`** (species absent from the VGP list) **ever be an error?**
  Currently a warning, and it fires on nothing — all 79 repo species are listed.
  If every congen species must by definition be a VGP species, error is more
  honest.
