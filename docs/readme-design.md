# congen readme — design

The second tool. Generates a per-species `README.md` in `congen-metadata`
describing the dataset: whether it can be trusted, what it contains, where to
get it, what the QC says, and how to cite it.

Part 3 of [`validator-design.md`](validator-design.md) sketched this tool far
enough to check the core boundary was drawn correctly, and said it needed its
own design pass. This is that pass. Where the two documents disagree, this one
wins; the specific reversals are called out as they arise.

The shared architecture — the core/tool boundary, tool registration, the
pure-Python constraint, round-trip YAML, findings and status — is unchanged and
not restated here.

## Part 0 — What was settled, and what the evidence was

| Decision | Settled as | Because |
|---|---|---|
| Output filename | `README.md` | the rich document should be the front door; GitHub renders it |
| `dataset.json` committed | yes | makes rendering offline, deterministic and reviewable |
| Which depth is *the* depth | `qc/qc_report.tsv` `mean_depth` | it is what the QC dashboard shows |
| Per-sample stats in the document | omitted | 232 samples for one species; summarize and link |
| Outlier flagging | **none** | the only available thresholds are not about samples — see Part 3 |
| Depth distribution | pinned bucket ladder, empty edges trimmed | adaptive bucketing collapses heavy-tailed cohorts — see Part 3 |
| GenomeArk browse links | none exist; the links block **is** the index | no browse UI covers `downstream_analyses/` — see Part 3 |
| Citations source | curated file in the repo | live lookup resolves 8% and cannot be hand-corrected |
| Failure rendering | two document modes, one gated block | no per-block graceful degradation |
| Block order | Validation, Description, Sample QC, References, Getting the data | the download block is the longest and least interesting first |
| Supplementary facts | rendered only when they deviate; suppressed ones go to `<details>` | ten description rows buried the five that vary — see Part 1 |
| Download block scope | 4 primary artifacts + `bams/`; the rest collapsed | sixteen equal-weight rows is an index nobody scans |

Three of these reverse the sketch in Part 3 of the validator design:
`remote/literature.py` is no longer called by this tool (Part 4), the document
is fully generated rather than partially (Part 1), and `README.txt` is no longer
the file GitHub renders (Part 1).

## Part 1 — What the document is

### Five blocks, in this order

| Block | Answers |
|---|---|
| Validation | can I trust this? |
| Description | what is it — species, reference, caller, samples? |
| Sample QC | what does the QC say? |
| References | how do I cite it, and whom do I credit? |
| Getting the data | how do I fetch it? |

Validation comes first because it is the gate, but the species name and a
one-line description sit above it: the file reads as a document with a gate, not
a gate with a document attached.

**Getting the data comes last**, settled from the Phase 0 render. It is the
longest block and the least interesting to someone deciding whether this dataset
is the one they want — describe it, show its quality, say how to credit it, and
only then explain how to pull 193 GiB of BAMs.

That ordering also reassigns responsibility: because Sample QC now precedes it,
**the QC block links the tables it summarizes inline** rather than deferring to
the download block. Those files are kilobytes, and a reader of that section wants
them right there. Getting the data is then purely about bulk retrieval.

### The file is `README.md` — settled

`OUTPUT_NAME` is a single constant in `congen.tools.readme` with a CLI override,
so this stays a one-line change.

The consequence accepted deliberately: GitHub prefers `README.md` over
`README.txt` when both are present, so the baseline file stops being the
rendered preview in the web UI. It remains in every directory listing, remains
authoritative for the bioproject list, remains what is mirrored to GenomeArk, and
remains what `R020`, `G017`, `G018` and `E002`/`E003` read. The generated
document links to it explicitly.

`core.metadata` still knows only about `README.txt`. Core has no business
knowing a tool's output name.

### Two modes, not graceful degradation

An earlier draft gave every block a `needs=(...)` declaration and an
"unavailable because X" rendering, mirroring the validator's `SKIPPED`. That was
the wrong shape. A species with no published data has nothing to say in three of
five blocks, and a document three-fifths full of apologies is worse than a
document that says one clear thing.

So there are exactly two modes:

- **Full** — all five blocks.
- **Truncated** — species name, block 1, a pointer to `VALIDATION.md` and
  `README.txt`, and nothing else.

Blocks become plain functions and the gather phase becomes unconditional. No
`needs`, no per-block skip states.

A truncated document must not be *poorer* than the `README.txt` beside it. Since
`README.md` is now what GitHub renders, a truncated file that omitted the
pointers would show a reader less than the plain-text file next to it.

### A supplementary fact earns a row only when it deviates

The Phase 0 render started with a ten-row description table, and the extra five
rows were all facts that are the same for every species in the corpus. Stating
them buries the five that vary.

So: **a supplementary fact is rendered only when it departs from what is
expected, and then it is flagged.** Applied in three places so far:

| Fact | Silent when | Rendered when |
|---|---|---|
| VGP main haplotype | the reference is canonical | it is not — naming what VGP lists instead |
| Ploidy | 2 | anything else |
| Sheet / BAM / VCF sample counts | all three agree | they disagree — the `anser-albifrons` case |

The quiet cases say nothing; the anomalies are the loudest thing on the page.
This is the same instinct as the validator's split between findings and status,
one level down: a fact that never varies is inventory, and inventory does not
belong in the reader's way.

Two consequences. Facts that are suppressed but were still fetched go into a
collapsed `<details>` block rather than being discarded — they cost a network
call and none of them is wrong. And **the document's shape now varies by
species**, so the golden-file suite needs a quiet fixture and an anomalous one,
not just one of each mode.

### The two gates

Two independent preconditions, because they are different questions with
different owners and different fixes.

```
Gate A — is there a dataset to describe?
    status.state == complete
    no error findings
    state != STALE
    validation.json present, and its recorded input digests match the files now

Gate B — is the provenance chain sound?
    every gating check ran and passed
```

**Gate A failure truncates the document.** It means *do not use this data*.

**Gate B failure blocks the References block only.** It means *do not publish on this yet*.

The split is the findings/status split from the validator doing useful work:
severity measures metadata hygiene, `status.state` measures whether a dataset
exists, and neither alone answers whether a document should be written.

#### Gate B's gating checks

| ID | Severity in `validate` | What its failure means for citations |
|---|---|---|
| `G017` | warn | no repo `README.txt` — no reviewed bioproject list exists |
| `G018` | warn | no `README.txt` anywhere — no bioproject list at all |
| `R020` | warn | `README.txt` names a different accession — its list may describe another dataset |
| `E001` | error | a run does not belong to the biosample the sheet claims |
| `E002` | warn | runs come from a bioproject the README does not cite — the list is incomplete |
| `E003` | warn | the README cites a bioproject contributing no runs — the list has a wrong entry |

**This tool asserts a policy the validator deliberately does not.** `G017` is a
warning in `validate` because the *data* is sound — and that is the right call
there. It is disqualifying *for publication* because the bioproject list is the
sole source of citations. Severity and publishability are different axes, and
this is where the second one gets decided. Writing that down matters: it must not
live implicitly inside a filter.

#### "Ran and passed", not "did not fire"

Gate B requires that each gating check **ran and produced no finding**. A check
that was skipped or never selected is *unknown*, and unknown is not sound.

This is not hypothetical, and it fails in two distinct ways today:

- **Tier 5 is opt-in.** `E001`–`E003` run only under `--check-sra`. All 79
  current records happen to include them, but that is a property of how the
  reports were last written, not a guarantee. Under a naive "no provenance
  findings fired" test, a plain `congen validate --all --write-reports` would
  silently promote all 13 citation-blocked species to fully cited documents.

  **Observed in Phase 1, and the staleness machinery actively invites it.**
  `catalog_digest` fingerprints the checks *selected for a run*, so
  `congen validate --stale` without `--check-sra` compares a 41-check catalog
  against the 44-check one the reports were written with, and reports **every
  species in the corpus** as needing revalidation with the reason "the check
  catalog changed". The obvious way to clear that is
  `--stale --write-reports` — which, still without `--check-sra`, rewrites all
  79 records with tier 5 skipped. One forgotten flag, and gate B's "ran and
  passed" rule is the only thing between that and 13 species quietly gaining
  unverified citations.
- **`R020` is skipped 22 times.** It `needs` the README, and 22 species have no
  repo `README.txt`. Every one of those findings is `skipped`, not `warn` — the
  check fires as a warning on nothing in the whole corpus. Reading that as a pass
  would be reading "we could not check" as "we checked."

The second case is currently redundant, since all 22 are already blocked by
`G017` or `G018`. The principle is not.

#### The gating IDs are public API

Same status as the check IDs themselves: one documented constant, marked in
`congen validate --list-checks`, and named in the document that a failure blocks.
Retire, don't renumber — a renumbered ID would silently stop gating.

### What a blocked citations block renders

Never the list. Two renderings, because `G017` and `E002`/`E003` are different
epistemic states:

| Trip | n today | State | Alert |
|---|---|---|---|
| `G017`, `G018`, `R020` | 14 | the list is **missing** from the repo | `[!WARNING]` — no reviewed bioproject list recorded; import it from GenomeArk |
| `E001`, `E002`, `E003` **fired** | 3 | the list **exists and is wrong** | `[!CAUTION]` — the recorded list is known incorrect |

**The stronger rendering requires a wrongness check to have actually fired, not
merely to be unsound.** This looked like a distinction without a difference until
the gate ran over the corpus: for all fourteen `G017` species, `R020`, `E002` and
`E003` come back **skipped**, because every one of them needs the `README.txt`
that `G017` is complaining about. Selecting the rendering by "is any of
`E001`–`E003` unsound" would therefore label all fourteen *demonstrably wrong*,
which asserts something nobody established — the list is absent, and an absent
list cannot be incorrect. Selecting on `FIRED` gives 14 missing and 3 wrong,
which is the truth.

The same distinction drives what the document *names*. For a `G017` species the
finding to cite is `G017`; `R020`, `E002` and `E003` are downstream consequences
of it, and listing them would misdescribe one problem as four.

**No bioproject list is rendered in either case, and no caveated list either.** A
wrong list with a warning above it still gets copy-pasted. And for `E003`
specifically, rendering "the list minus the spurious entry" would require
guessing which entry is spurious — the same inference the validator refuses to
make when it reports `E002` and `E003` as two facts rather than one substitution.

**There is no fallback to the published `README.txt`.** An earlier draft proposed
rendering the GenomeArk copy with a provenance note for the 10 `G017` species.
Rejected: an unreviewed file that is not in version control is not a citation
source, and rendering around a finding removes the pressure that gets it fixed.
The fix for `G017` is to copy the file into the repo, which is what `G017` is
already asking for.

The block states the obligation explicitly rather than vaguely — *the data
generators for this dataset are not recorded; do not publish analyses until they
are* — which is more actionable than withholding the document.

### The banner carries the summary

Whole-document truncation on gate B was considered and rejected. What it bought
was a binary trust signal: a full file means everything is in order. That signal
is preserved for free, because block 1 is always rendered and always first:

```markdown
> [!WARNING]
> **PASS** · 2026-09-09 · GCA_030936135.1 — data verified.
> **Provenance incomplete (`G017`)** — no reviewed bioproject list in this repo,
> so this dataset cannot yet be cited. See [References](#references).
```

Same at-a-glance signal as truncation, with a useful document underneath.

What is accepted in exchange: someone can download data whose upstream credits
are unrecorded without hitting a wall. Mitigated by the banner, by the block's
explicit statement of the obligation, and by `congen citations --report` tracking
it corpus-wide. Not eliminated. If the trade ever looks wrong, gate B moves from
block-level to document-level by changing one predicate's call site.

### Corpus baseline — 79 species, as of 2026-09-10

```
53   full, fully cited
17   full, citations blocked      14 × G017 · 3 × E002/E003
 9   truncated                     7 × no data published · 3 × validation errors
```

**These counts move, and that is not a defect.** Between 2026-09-09 and
2026-09-10 five species gained data on GenomeArk — both partial uploads
completed, and three previously-absent species were published — and the split
went from 53/13/13 to 53/17/9 with nothing wrong anywhere. Four species left the
truncated set and entered the blocked set in the same step: complete and
error-free, but with no repo `README.txt`. `cited` landing on 53 twice is
coincidence.

That is why the test suite asserts the gate's **invariants** rather than these
numbers — see Part 5 — and why `congen readme --gate-report` exists to print
them offline on demand.

Truncated by gate A:

```
validation errors   anser-albifrons (S001, S002) · grus-americana (F021)
                    sturnus-vulgaris (F020, and no data)
no data published   caprimulgus-europaeus, taeniopygia-guttata,
                    arvicola-amphibius, macrotis-lagotis, myotis-nattereri,
                    notamacropus-eugenii
```

Citations blocked, dataset otherwise complete and error-free:

```
G017 fired (14)   acridotheres-tristis, catharus-ustulatus, falco-peregrinus,
                  hirundo-rustica, haliaeetus-albicilla, esox-lucius,
                  astatotilapia-calliptera, coregonus-lavaretus,
                  eubalaena-glacialis, lemur-catta, neofelis-nebulosa,
                  panthera-onca, phocoena-sinus, eublepharis-macularius
E002/E003 fired   dryobates-pubescens, cyclopterus-lumpus, sus-scrofa-domesticus
```

**The blocked set is a work queue, and it mostly clears mechanically.** All
fourteen `G017` species have a `README.txt` on GenomeArk that was never copied
back — one `aws s3 cp` each. All three `E002`/`E003` species cite the *assembly*
BioProject where the reads sit under a separate *raw reads* BioProject — one edit
each. So a strict gate is not a standing 22% citation-blocked rate; it is
seventeen actions.

`sturnus-vulgaris` trips two gate-A reasons at once. The truncated blurb lists
every blocking reason, not the first.

**One further edit is worth more than any of them.** `GCA_052056855.1` —
*Sturnus vulgaris*, the VGP main-haplotype assembly that `F020` has been naming
all along — is now published complete: 39 samples, raw and filtered VCF, README,
all four subdirectories. `sturnus-vulgaris/config.yaml` still declares
`GCF_001447265.1`, an older scaffold-level assembly. One line there turns the
corpus's only "wrong reference, no data" species into a complete one.

## Part 2 — Inputs and determinism

### The four input classes

| Class | Files | Authored by |
|---|---|---|
| repo metadata | `config.yaml`, `sample_sheet.csv`, `README.txt` | humans, per species |
| curated references | `references/bioproject_citations.csv`, `references/tool_citations.yaml`, `references/vgp_reference_genomes.csv` | humans, corpus-wide |
| harvested facts | `dataset.json` | `congen readme --refresh` |
| the validation record | `validation.json` | `congen validate --write-reports` |

The repo metadata does the most work: the sheet is the only source of sample IDs,
counts and input types; `config.yaml` is the only source of the declared
reference; and `README.txt` is the only source of the bioproject list, which is
why gate B exists.

Note the reference sheet is plural. `vgp_reference_genomes.csv` is already
human-maintained and already supplies the taxid and the canonical-accession
comparison.

### What belongs in `dataset.json`

**Exactly what cannot be recomputed offline, and nothing else.** Sample counts
and sample IDs stay out — the sheet has them, the tolerant loaders already read
them, and a second copy is only a way for the two to disagree.

Under that rule it spans five remote sources:

| Source | Contributes |
|---|---|
| S3 listing | object names, sizes, ETags, the resolved accession, and **`opaque_subdirs`** — the prefixes under `callable_sites/` that are never enumerated, so no total can silently understate them |
| QC tables | `qc_report.tsv`, `individuals.het`, `individuals.imiss`, `coverage_thresholds.tsv` |
| VCF/BAM headers (ranged GET) | recorded caller version, ploidy, het prior, contig list |
| NCBI | organism name, common name, assembly name and level, paired accession |
| SRA | `sra`: sheet input accession → the runs it holds, each with its biosample and bioproject. The join back to samples is local (the sheet already maps sample to input), so that join is the consumer's job |

It is per-species for discoverability, matching `validation.json` — but its
contents are keyed by **accession**, so the file names the accession it was
harvested for and render fails loudly if the config's resolved accession has
since changed. Not hypothetical: `grus-americana` and `sturnus-vulgaris` both
have declared/resolved divergence today.

It also carries `tool_version` and `harvested_at`. Neither is rendered.

### `dataset.json` and `validation.json` will overlap

Both record the resolved accession, S3 ETags and BAM counts. Refactoring
`validation.json` to reference the harvest was considered and rejected — its
digests are load-bearing for staleness and it is already shipped. Instead the
readme cross-checks accession and counts across the two and renders a visible
complaint on mismatch. Two artifacts harvested against different data is
otherwise an invisible failure.

### The fifth input has no file

The renderer's own static text: headings, the download recipes, the zarr
warning, the "how to cite" boilerplate. It lives in code, and changing it changes
all 79 outputs. It is named here because it is the input people forget when
reasoning about determinism, and because it is why `--check` must be able to fail
on a release with no data change at all.

### Determinism invariants

Render touches **no clock, no network, no git state, no environment.** Testable:
a render with networking disabled must succeed.

- **Every date shown derives from an input** — `validation.json`'s `validated_at`,
  S3 `LastModified` — never from `now()`.
- **Never render S3 `LastModified` as when the data was produced.** It records
  upload order, not generation order; `G011` already learned this the hard way.
- **No tool version and no timestamp in the document body.** A rendered version
  string would diff all 79 files on every release and bury the real changes — the
  same failure mode as the ruamel boolean-casing problem.
- **Pin decimal places.** The QC TSVs carry ragged precision: `15.64` beside
  `6.97769` in the same column.
- **One size convention** (GiB) and one rounding rule, chosen once.
- Sorted ordering everywhere.

### Render is a merge, not a function

With managed blocks plus an untouched hand-prose region, the signature is
`render(inputs, existing_file) -> file`. That is two properties to test, not one:

- **determinism** — same inputs and same existing file produce the same bytes
- **idempotence** — rendering twice changes nothing

The second is what catches marker-handling bugs.

### Staleness: two questions, and one of them is free

Because `dataset.json` is committed, **every input is local**, so rendering is a
pure offline function and "is `README.md` out of date?" is answered by
re-rendering and comparing. `writers.would_change()` already does it.

That is the structural difference from the validator. **A validation report is a
claim, so it must carry digests of what it claimed about. A readme is a
rendering, and a rendering's staleness is testable by re-rendering.** This tool
needs no digest machinery of its own.

| | Detects | Cost |
|---|---|---|
| **Render staleness** — `README.md` ≠ `render(inputs)` | edited sheet, new verdict, curated citation, refreshed harvest, *and a changed renderer* | offline, exact, free |
| **Harvest staleness** — `dataset.json` no longer describes GenomeArk | new upload, pipeline rerun | one LIST per subdir plus conditional GETs, using the ETags already recorded |

## Part 3 — The blocks

### Validation

Reads `validation.json`. **Does not re-run validation.**

Renders as a GitHub alert, so it reads as a gate:

| Record state | Alert |
|---|---|
| PASS | `[!TIP]` |
| PASS WITH WARNINGS / NOTES | `[!NOTE]` |
| PENDING | `[!NOTE]` — an expected state, not a problem |
| STALE | `[!WARNING]` |
| FAIL | `[!CAUTION]` |
| absent, or digests do not match | `[!WARNING]` — unverified |

Contents: verdict, date, accession, one-line blurb, finding counts, the gate-B
provenance line when it applies, and links to `VALIDATION.md` and `CHECKS.md`.
**Not the findings themselves** — duplicating them creates two places to go
stale.

**The digest cross-check is mandatory, not advisory.** Before quoting a verdict
the block recomputes SHA-256 over `config.yaml`, `sample_sheet.csv` and
`README.txt` and compares against `validation.json`'s `inputs`. Three hashes of
small files, offline.

It was advisory in an earlier draft, when its only job was preventing a stale
banner. It is mandatory now because **the mode gate is derived from
`validation.json`**: a stale record does not merely misreport a verdict, it can
render a full document with download links and stats for a species that would now
fail gate A.

`FAIL` and `PENDING` want different truncated text in the same shape —
"validation found errors; do not use" versus "metadata committed ahead of the
run; no data published yet." `STATE_BLURB` in `report/markdown.py` is the
existing pattern.

Not doing: shields.io badges. An external image, needing a hosted endpoint, and
the only thing in the file that would not be self-contained.

### Description

| Field | Source |
|---|---|
| binomial, clade | directory path |
| NCBI taxid | `references/vgp_reference_genomes.csv` (`QID` column, already aliased in `vgp.py`) |
| organism name, common name | `remote/ncbi.py` — `AssemblyInfo`, one cached call |
| reference accession, assembly name and level, paired accession | `remote/ncbi.py` |
| is it the VGP main-haplotype assembly | `vgp.py` |
| caller version, ploidy, het prior | **the VCF header**, not the config |
| sample count, unique count, input-type breakdown, run count, `library_id` | the sheet |
| BAM count, VCF sample count | `dataset.json` |
| variant sites | `individuals.imiss` `N_DATA` |
| contig count | VCF `@contig` lines via `remote/headers.py` |

**Prefer recorded provenance over declared.** Tier 4 already reads the caller
version and arguments from the VCF header — that is what ran. Fall back to the
config only when the header is unavailable, and label it *declared* rather than
*recorded*.

**State the resolved accession, not the declared one.** `grus-americana` declares
a GCF where the data sits under the GCA; `sturnus-vulgaris` declares a different,
older, scaffold-level assembly entirely.

The variant-site count is free: `individuals.imiss` carries `N_DATA`, which is
31,253,369 for *Apteryx mantelli*.

### Sample QC

The QC directory is richer than the validator design's table suggested.
Verified against `GCA_036417845.1`:

| File | Provides |
|---|---|
| `qc/qc_report.tsv` | per-sample reads before/after filtering, `percent_mapped`, `percent_duplicates`, `percent_properly_paired`, `mean_depth`, `covered_bases` |
| `qc/individuals.idepth` | `MEAN_DEPTH`, `N_SITES` |
| `qc/individuals.het` | `F` |
| `qc/individuals.imiss` | `F_MISS`, `N_DATA` |
| `callable_sites/coverage_thresholds.tsv` | `cohort_mean_coverage`, `min_coverage`, `max_coverage` |
| `qc/qc_dashboard.html` | the dashboard — 8.6 MiB, served as `text/html`, so a plain link opens in a browser |

**Link the dashboard, never fetch it.**

**This block links its own sources.** The dashboard, `qc_report.tsv`,
`individuals.het`, `individuals.imiss` and `coverage_thresholds.tsv` are cited
inline where each is discussed, not deferred to the download block — they are
kilobytes, and someone reading a summary of them wants the originals to hand.

#### There are two coverages and they disagree — settled

For `SAMEA2554516`, `qc_report.tsv` reports `mean_depth` 18.36 while
`individuals.idepth` reports `MEAN_DEPTH` 15.64: mapped-read depth versus depth
at called sites. **`qc_report.tsv` is the source**, because it is what the
dashboard shows, and the document names the source in the text so nobody has to
rediscover this.

#### No outlier flagging — settled, against an earlier proposal

An earlier draft proposed flagging samples outside `min_coverage`/`max_coverage`
from `coverage_thresholds.tsv`, on the reasoning that restating the pipeline's own
thresholds establishes no new policy. Measured across 14 species and 557 samples,
that would flag **99 samples, 17.8% of the corpus**:

```
species                        n    thresh  depth med    min     max  below above
fishes/betta-splendens       150   10-44       18.05    6.84  161.00     31    16
birds/haliaeetus-albicilla    96    9-37       19.35    0.07   37.67     11     1
birds/catharus-ustulatus      50    4-20        8.39    1.12   35.51      5     9
fishes/esox-lucius            65   17-69       22.19   15.52  179.54      3    12
mammals/lemur-catta           12   13-55       25.01   21.30   48.12      0     0
```

**Those fields are not per-sample QC thresholds.** They are cohort-level
site-depth bounds for building the callable-sites mask, derived from
`cohort_mean_coverage` — roughly 0.5× and 2–4× of it. Comparing a per-sample mean
depth against them is a category error: it does not restate the pipeline's
judgement, because the pipeline never made that judgement about samples.

So the block reports **distributions and no flags**: cohort size,
`cohort_mean_coverage`, the callable-sites thresholds labelled as what they
actually are, the depth distribution below, and the ranges for `percent_mapped`,
`percent_duplicates`, `F_MISS` and `F`.

One descriptive addition: **name the min and max samples by ID.** That is the
first thing anyone looking at a wide range wants, and naming an extreme is not a
judgement about it. `haliaeetus-albicilla`'s minimum of 0.07 needs no flag beside
it to be noticed.

#### The depth distribution — fixed buckets, trimmed edges

A quantile line plus a histogram on a **pinned ladder** —
`0, 5, 10, 15, 20, 30, 40, 60, 100, +` — with **empty leading and trailing
buckets trimmed and interior gaps kept**:

```markdown
Mean depth: median **22.2×**, IQR 18.6–36.8, range 15.5 (SAMN10685118)
to 179.5 (SAMEA111484070).

| mean depth | samples | |
|---|---:|---|
| 15–20× | 26 | ████████████ |
| 20–30× | 20 | █████████ |
| 30–40× | 6 | ███ |
| 40–60× | 1 | █ |
| 60–100× | 8 | ████ |
| 100×+ | 4 | ██ |
```

**Both halves are needed, and `esox-lucius` above is why.** The quantile line
reads as one cohort with an outlier; the table shows two populations with a dip
between them, which is what a cohort assembled from two sequencing efforts looks
like. The line alone hides that. The table alone loses the exact extremes and the
sample IDs.

**Adaptive bucketing was tested and rejected.** Eight equal-width buckets over
the observed range put 47 of `esox-lucius`'s 65 samples in the first bucket,
because two samples above 130× stretch the range and destroy all resolution where
the samples actually are — the standard failure of range-based bucketing on a
heavy tail. A pinned ladder also makes species comparable, which adaptive
bucketing cannot be by construction.

Trimming is what keeps a fixed ladder from becoming nine rows of zeros: it yields
six rows for `esox-lucius` (n=65) and three each for `lemur-catta` (n=12) and
`apteryx-mantelli` (n=19), so no sample-count threshold is needed. Interior zeros
stay, because a gap is the signal.

The bar is `U+2588 FULL BLOCK` repeated, scaled to 30 columns at the maximum
bucket, minimum one block for any non-empty bucket. Runs of one glyph stay
comparable even in a proportional font.

#### The QC tier this does not build

The probe surfaced samples broken by any standard rather than by a chosen
threshold — `haliaeetus-albicilla` has one at `mean_depth` 0.07,
`percent_mapped` 2.33%, `F_MISS` 0.96. Near-tautological tests (`F_MISS > 0.9`,
`percent_mapped < 50`) would be honest findings, and they belong in the
validator, not here. A documentation generator must not establish QC policy as a
side effect.

**Deferred, and not an open question for this tool.** Sample-level QC thresholds
belong to a larger conversation about filtering and QC that is out of scope here.
This design's only obligation to it is not to prejudge it: `remote/qc.py` will
have parsed every table that conversation needs, and no threshold chosen by this
tool will be sitting in the corpus when it happens.

### References

Rendered only when gate B passes. Four parts:

1. **How to cite the dataset** — GenomeArk path and accession.
2. **The reference assembly** — see below; currently always `pending`.
3. **Contributing bioprojects** — accession, title, citation, and samples
   contributed where the SRA mapping is available.
4. **The pipeline** — from `references/tool_citations.yaml`.

The "please cite them when using this dataset" sentence already in every
`README.txt` becomes actual citations here.

**The assembly citation is deliberately unresolved.** It will most likely be the
VGP flagship paper, but that is not settled, so `tool_citations.yaml` carries the
assembly entry at `status: pending` and the block renders the accession with its
NCBI link and nothing more. This reuses the bioproject three-state machinery
rather than inventing a second mechanism — `pending` behaves exactly as
`unreviewed` does.

**A pending assembly citation must never trip gate B.** Gate B is about the
bioproject list; not knowing how to cite the assembly is an honest gap of the
same kind as an unreviewed bioproject, and blocking 66 documents on it would be
absurd.

**A per-species coverage line**: *citations available for 38 of 45 samples (4 of
6 bioprojects)*. Measured in **samples, not bioprojects** — one unreviewed
project contributing 40 samples matters far more than one contributing 1. This is
the number that gets gaps filled.

### Getting the data

Base is
`s3://genomeark/downstream_analyses/conservation_genomics/variant_calling/{RESOLVED_ACCESSION}/`,
built from the **resolved** accession.

#### There is no browse UI, so this block is the index — settled

GenomeArk's web browser does not cover `downstream_analyses/`, and the
alternatives are all worse than nothing: the S3 REST listing renders as raw XML
in a browser, and the AWS console's bucket browser requires a login, which
defeats the point of an anonymous bucket.

So this block is not a supplement to a browse UI — **it replaces one**. An
earlier draft concluded that therefore every file a human might open should get
its own row, roughly sixteen of them. **The Phase 0 render disproved that.**
Sixteen rows of near-equal weight is an index nobody scans, and it buries the
three or four artifacts people actually come for.

#### Primary, secondary, absent

**Primary** — one row each, with size and a one-line description:

```
vcfs/raw.vcf.gz · .tbi          the calls
vcfs/filtered.vcf.gz · .tbi     where the run produced them
callable_sites/callable_sites.bed   the mask
qc/qc_dashboard.html            the QC report, opens in a browser
bams/                           one directory row: total size and object count
```

**Secondary** — the QC tables, the mask components, `contig_map.tsv`,
`mappability.bedgraph` — in a collapsed `<details>`, and linked *from the QC
block* where they are actually discussed.

**Excluded entirely**: the GenomeArk copies of `README.txt` and
`sample_sheet.csv`. Both duplicate a file already sitting in the species
directory, and the S3 sheet is explicitly not ground truth — `S006` exists
because it can disagree with the repo copy. Linking it as though it were
authoritative would be worse than not linking it.

**Absent** is stated, not silently omitted: *"This run did not produce
`filtered.vcf.gz`."* Indexes are folded into their parent rather than named
separately.

#### Sizes are a lower bound, and the document must say so

The Phase 0 render first claimed *"the whole dataset is 199.4 GiB across 78
objects"*, which is false. The `callable_sites/` listing is delimiter-based, so
the zarr stores come back as prefixes and their contents are never enumerated —
by design, since one species' `callable_loci.zarr/` runs to thousands of objects.

Any total is therefore a lower bound. `dataset.json` records the opaque
prefixes by name (`opaque_subdirs`), and the block states the exclusion
explicitly rather than implying a total it does not have.

#### Recipes

```bash
bcftools view -r <chr>:<start>-<end> https://genomeark.s3.amazonaws.com/.../vcfs/raw.vcf.gz
aws s3 sync --no-sign-request s3://genomeark/.../bams/ ./bams/
aws s3 sync --no-sign-request --exclude '*.zarr/*' s3://genomeark/.../callable_sites/ ./
```

The ranged read leads, because most people asking how to get a 4 GiB VCF should
not be downloading it. Two non-obvious things the block must say:

- **`--no-sign-request`.** The bucket is anonymous, and an `aws s3` call without
  it fails confusingly for anyone with credentials configured.
- **Never recursively sync `callable_sites/`** without the `--exclude`. The
  listing code already refuses to descend there; humans deserve the same warning.

#### Link definitions live at the foot, tagged by path

The shared S3 prefix is 100 characters. Inline links made every table row
unreadable in source and unreviewable in a diff, so the block emits
reference-style links and the renderer collects the definitions at the end of the
document. **The tag is the object's path** — `[qc/qc_report.tsv]: …` — not a
serial number, so the definition list is self-documenting and a diff on it says
which file changed.

This is why a block returns `(lines, refs)` rather than lines: any block can cite
any file, and only the renderer knows where the definitions go. See Part 5.

## Part 4 — Citations

### Live lookup does not work — measured

Part 3 of the validator design proposed `remote/literature.py` over NCBI
`elink`. Probed on 25 sampled bioprojects:

| Method | Resolved to ≥1 publication |
|---|---|
| NCBI `elink` bioproject → pubmed | **2 / 25 (8%)** |
| Europe PMC full-text accession search | **17 / 25 (68%)** |
| NCBI `esummary` (project title only) | **25 / 25 (100%)** |

Europe PMC searches for the accession string in article full text, which is how
data citation actually happens. The hits are plausible — `PRJEB27649` returns the
house-sparrow commensalism paper, `PRJNA562003` a lumpfish local-adaptation
paper, the same bioproject `E003` flags on `cyclopterus-lumpus`. **`elink` is
close to useless for this and should not be built.**

But several accessions return more than one hit, and nothing in the response
distinguishes the paper that *generated* the data from one that *reused* it. So
the lookup produces **candidates, not answers.**

### Why a curated file, and two corrections

An earlier draft argued the file's advantage was that bioprojects recur across
species. **They do not**: only 6 are cited by more than one species (maximum 4).
The file is essentially one row per bioproject.

**And there are 298 of them, not 208.** The `README.txt` files cite 208 distinct
bioprojects between them; the SRA mapping in `dataset.json` attributes runs to
296; the union is 298. Measured on the corpus, of the 88-project difference
**87 are reachable only through the 22 species that have no `README.txt` at
all**, and the remaining 3 are exactly the `E002` cases.

That reframes `G017`. Those species are not merely missing a file — they conceal
87 bioprojects' worth of uncredited data generators, which is the real weight
behind treating a missing README as disqualifying for publication. It is also why
the review queue is built from the **union** of what a README declares and what
SRA says contributed: either source alone builds the wrong queue.

It is still right, for the reasons that survive: a human can correct it, it makes
render deterministic and offline, and it makes the gaps visible and countable.
Corpus-wide files live in `congen-metadata/references/` alongside
`vgp_reference_genomes.csv`, which is exactly where core already owns access.

### The review surface — rebuilt, twice

The first attempt was a 298-row CSV with a status column and one `doi`
column. **It failed on contact with a reviewer**, and the ways it failed
are worth recording because each was a design error rather than a bug:

- It sorted alphabetically by accession, so the 150-sample project was
  buried at row 200 and the order carried no information.
- Two of nine columns (`reviewed_by`, `reviewed_on`) were empty in all
  298 rows. Git already records who and when.
- One `doi` column could not express a BioProject with two papers.
- **79 rows had multiple search candidates and only the top one was
  stored**, so a quarter of the evidence was unreachable.
- Nothing showed samples affected or which species, which is the only
  thing that decides whether a row is worth a reviewer's time.

The diagnosis: three things with different lifecycles had been merged into
one file. *Evidence* is machine-generated, regenerable and bulky.
*Decisions* are human-authored, tiny and precious. The *reading surface*
is neither.

**Reviewing is deletion.** The fastest thing a human can do is click a
DOI, skim, and delete the lines that are wrong — and editing a file in
GitHub's web editor makes each such edit a commit, so the decision and
its provenance land together. Checkboxes were considered and ruled out on
a fact: task-list checkboxes are read-only in repository markdown files,
interactive only in issues and pull requests.

So: **one rule, and every line is a claim.** Delete the ones that are not
true. `NOT YET REVIEWED` is itself a claim, which is what makes it the
single bit distinguishing a decision from an untouched block — no status
field anywhere.

**Two files, because a queue you must scan past settled work is not a
queue.** `citations-review.md` holds only what needs a human and empties
as the work is done; `bioproject_citations.tsv` is the settled record.
The loader reads both and the queue wins, so a decision counts the moment
it is committed — running `--collect` to file it is housekeeping, not a
correctness step.

### One not-found state, not two

A draft split "no publication" into `YET` (provisional) and `EXPECTED`
(permanent). **Both halves were wrong.**

Whether a paper is *coming* is a claim about the submitter's intentions,
and we are almost never the submitter — so `EXPECTED` asked a reviewer
for something they cannot know. And it was unnecessary: the evidence
trigger already stops the queue nagging, because a BioProject with no
candidates has nothing unseen to show. Worse, `EXPECTED` would have
suppressed the one notification worth having — a paper appearing for a
project someone had written off.

`NO PUBLICATION FOUND` says only what was searched for and not seen,
which is ours to say.

**`confirmed` is final; `not_found` reopens on new evidence.** A later
search hit on the same BioProject is usually data *reuse*, and reuse
needs no credit, so reopening a confirmed entry would offer a citation
nobody should add. The genuine two-paper case is rare and is handled at
review time, when both candidates are already on screen.

The trigger is **evidence, not elapsed time** — the same reason the
validator makes staleness a property of the inputs rather than of the
calendar. A calendar rule would either nag about hopeless cases or sit on
a paper that appeared yesterday. Making that decidable needs one
addition: an invisible `<!-- considered: ... -->` line recording every DOI
a reviewer was shown, so `rejected = considered − kept`. Deleting list
items never disturbs it, and the record stores both sides, so a reopened
block never re-offers something already turned down.

### Affiliation matching earns the ★

A candidate whose author affiliations include the BioProject's submitting
organisation gets a `★` and is listed first. It is a proxy for "generated
by the same people", and it is the strongest signal available for free.

It must read **every author's** affiliation, not the top-level
`affiliation` field: for `PRJEB39599` the submitter is Helsinki and the
first author is in St Petersburg, with Helsinki further down the list.

Labelled as *the affiliation matches the submitter* and never as *this is
the right paper*, because the match is a hint. `genetics` survives the
generic-token stoplist and would match any genetics department, so a
false positive must cost a reviewer one glance rather than produce a
wrong citation.

Measured over the corpus: 166 matches across the 182 BioProjects that had
any candidate, and **95 blocks have exactly one starred candidate** —
skim it and keep the line. 30 have several, 57 have candidates with none
starred, and 116 have no candidates at all.

### Three states, and why the not-found state is load-bearing

`references/bioproject_citations.csv` carries per bioproject: accession, title,
submitter, status, DOI, citation, `reviewed_by`, `reviewed_on`.

| Status | Means | Reopens? |
|---|---|---|
| `confirmed` | a reviewer kept at least one DOI | **no** — a later hit is usually reuse |
| `not_found` | searched; nothing found | on new evidence only |
| `unreviewed` | `NOT YET REVIEWED` still present | it is already in the queue |

Without the `none` / `unreviewed` split, every refresh re-proposes the same
cases forever and the review queue never empties — and on the probe's hit rate
that is roughly a third of the 208 rows. It is the same
distinction `G017` and `G018` drew, for the same reason.

### The workflow

```bash
congen citations --propose    # network: look up newly-seen BioProjects
                              # review by deleting lines; commit
congen citations --collect    # offline: file decided blocks into the record
congen citations --report     # offline: progress, and what remains
```

Each command has one job. `--propose` never decides anything and never
touches the record; `--collect` never looks anything up; `--report` writes
nothing. `--propose` skips a `confirmed` entry entirely and skips a queued
block that already has candidates, so a review in progress is never
disturbed.

**The queue is heavily front-loaded, and `--report` says so.** 24
decisions cover 50% of the 3,473 attributed samples, 59 cover 75%, 105
cover 90% — and 118 of the 298 BioProjects carry a single sample each.
Since an unreviewed BioProject blocks nothing (gate B is about whether
the *list* is correct, not whether a paper was found), the tail is
genuinely optional work, and a reviewer deserves to know that before
facing a queue of 298.

### Gate B is about the list, not the papers

Kept sharp, because conflating them would make curating 208 rows a prerequisite
for any README at all:

- A wrong or missing bioproject **list** is a falsehood → gate B blocks the block.
- A bioproject with `status: unreviewed` or `none` is an **honest gap** → it
  renders as a row with an empty citation column and counts against the coverage
  line.

## Part 5 — Implementation

### Layout

```
src/congen/tools/readme/
  __init__.py     Tool(name="readme", writes_metadata=True, needs_network=True)
  cli.py
  gate.py         the two gates, the gating-ID constant, mode selection
  harvest.py      --refresh: writes dataset.json
  blocks/         validation.py description.py links.py stats.py references.py
  render.py       assembles blocks, manages markers
src/congen/tools/citations/
  __init__.py     Tool(name="citations", writes_metadata=True, needs_network=True)
```

`citations` is a separate tool rather than a flag on `readme`: it writes a
different file, on a different cadence, and its network dependency is Europe PMC
rather than GenomeArk.

### Core additions

| Module | Change |
|---|---|
| `remote/qc.py` | parsers for `qc_report.tsv`, `individuals.idepth`, `.het`, `.imiss`, `coverage_thresholds.tsv`. `parse_tsv` already exists and is already tolerant |
| `remote/ncbi.py` | `AssemblyInfo` gains common name from the same cached call |
| `metadata/loaders.py` | readers for the two new `references/` files |
| `remote/literature.py` | Europe PMC search, used **only** by `citations --propose` |
| `metadata/writers.py` | no change needed — `ManagedBlock`, `render_managed_blocks`, `managed_block_names`, `atomic_write`, `would_change`, `write_if_changed` all already exist |

### Markers: one managed region, not five

#### The block contract

A block is a plain function returning `(lines, refs)`: the markdown, and the
reference-style link definitions it used. Only the renderer knows where
definitions go — the foot of the document — and any block may cite any published
file, so collecting them centrally is the only arrangement that works. The
reference tag is the object's path, which keeps the definition list legible.

Blocks have no `needs` declaration and no skip states; the gates decide what gets
rendered, so a block that runs at all has its inputs.

**One managed block, not five.** The generated body — every block, in both modes
— sits inside a single `<!-- congen:begin body -->` …
`<!-- congen:end body -->` pair, with a trailing region below the end marker that
the renderer never touches.

An earlier draft used one marker pair per block, justified by keeping diffs local
and by enabling `--only stats`. Neither survives: a deterministic render already
confines the diff to whatever actually changed, and `--only` was removed (see
CLI). What five pairs *did* buy was the design's single largest implementation
risk. `render_managed_blocks` replaces content between the markers it is given and
leaves other blocks alone, so a species going full → truncated would have kept its
download links and coverage stats sitting under a red CAUTION banner unless the
renderer actively reconciled which markers should exist.

With one pair, a mode transition is a content replacement and orphaned blocks are
unrepresentable. The PASS → FAIL → PASS round-trip test stays, but it is now
testing something that cannot fail structurally.

**The cost, stated plainly:** per-species hand prose can only land in the trailing
region, at the bottom, rather than beside the section it comments on. Accepted
because no species has hand prose today, and because splitting one block into
several later is a mechanical migration the tool can perform itself — it owns the
whole region. That is the opposite of the direction that motivated markers-from-day-one,
which was retrofitting markers into files humans had already edited. The trailing
region covers that risk on its own.

**Hand-written prose survives truncation.** Human content is never deleted; it
stays below the end marker in both modes.

### Harvest is independent of the verdict

`--refresh` runs for failing species too. The numbers should be ready the moment
a species is fixed, and nothing about harvesting should depend on validation
having run first. For the 10 species with no published data it is one cheap LIST
that returns nothing.

### CLI

```bash
congen readme birds/apteryx-mantelli        # render one, offline
congen readme --all                         # render the corpus, offline
congen readme --all --check                 # exit 1 if anything would change
congen readme --all --refresh               # network: update dataset.json, then render
congen readme --all --json report.json
congen readme birds/foo mammals/bar         # N targets
congen readme --force-full birds/foo        # debugging: ignore the gates
```

**There is no `--only <block>` flag.** An earlier draft had one. It has no use
case — rendering is offline and instant, so iterating on one block means
re-rendering the file; network cost lives in `--refresh`; and hand edits belong
outside the markers, so there is nothing inside a block to protect. It is also
**incompatible with `--check`**: it can write a document that is not the output of
any single render, which `--check` then reports as "would change" in perpetuity.

Unlike `validate`, writing is this tool's purpose, so it writes by default.
`writes_metadata=True` finally means what the registration field was for.

Exit codes match `validate`: `0` clean, `1` would-change, `2` misuse.

`--all` prints the mode per species, so `53 full · 13 citations blocked ·
13 truncated` is visible at a glance.

### Testing

The suite stays offline against recorded fixtures.

- **Golden files** for rendered output, byte-exact. This is how determinism is
  tested.
- **Idempotence**: render twice, assert no change.
- **Mode round-trip**: PASS → FAIL → PASS leaves no stale content and no
  duplicated markers, and preserves the trailing hand-prose region.
- **Every gate branch**: gate A pass/fail, gate B `G017` branch, gate B
  `E002`/`E003` branch, tier-5-not-run, digest mismatch.
- **Networkless render** must succeed with sockets disabled.
- QC fixtures from three species: one clean and complete, one partial, one
  absent.
- The gate-decision table asserted against all 79 current records, so the
  baseline in Part 1 is a regression target.

### Built CI-ready

CI is deferred, so everything below is hand-run for now. These nine choices are
free today and expensive to retrofit, and together they mean CI is a YAML file
and nothing else.

1. **Network and pure are split at the CLI, not just internally** — `--refresh`,
   bare, and `--check` become three jobs on three triggers with different
   permissions, with no code change.
2. **`--check` exists in v1 and is not a separate code path** — it is `render()`
   plus `would_change()`. Added later, it would drift from the writer.
3. **Variadic targets**, because a PR job knows which paths changed.
   `discovery.resolve()` already takes a path or a slug.
4. **`--json`** describing what changed or would change. CI needs structured
   output, not parsed stdout.
5. **Reuse the GitHub-annotation renderer** in `report/render.py`, so a `--check`
   failure annotates the offending file instead of only exiting 1.
6. **Exit codes pinned now.**
7. **Never read git.** Operate on the working tree, so hand-run and CI behave
   identically.
8. **No prompts, ever** — including the citations flow, which writes a staging
   file rather than asking.
9. **One command catches the ordering problem.** `README.md` quotes
   `validation.json`, so the sequence is `validate --write-reports` →
   `readme --refresh` → `readme`. Rather than encode that order in CI YAML,
   `readme --check` fails when `validation.json`'s recorded digests do not match
   the current repo files. That makes the ordering self-enforcing in both
   hand-run and CI use, and it is the same free three-hash comparison the
   validation block already performs.

`CONGEN_METADATA_ROOT` and `NCBI_API_KEY` already cover the environment side.

## Implementation phases

Ordered by which risk each one retires, which is not the order the parts are
described in above. The two genuinely unresolved things at the end of the design
pass were **whether the document is any good** — readability of the links table,
usefulness of the stats block, how complete the manifest should be — and
**whether the harvest schema is right**, since `dataset.json` is committed across
79 species and changing it later means recommitting all of them. Those interact
badly if the schema is frozen before the document that consumes it exists, so a
throwaway-tolerant vertical slice comes first.

**Phase 0 — vertical slice, one species. Done.** End to end for
`birds/apteryx-mantelli`, output to a scratch directory. It cost **12 HTTP
requests on a cold cache** — 5 S3 listings, 5 QC tables, 1 ranged VCF-header
read, 1 NCBI call — so a corpus harvest is ~950 requests, in the same range as
`congen validate`. No core change was needed to build it, which is the boundary
holding.

Four things it settled that reasoning had not:

- The `callable_sites/` size is structurally a lower bound, and the first render
  stated a false total. `opaque_subdirs` exists in the schema because of this.
- Sixteen equal-weight link rows is worse than four plus a `<details>`.
- Supplementary facts need the deviation rule, or the description table is ten
  rows of which five never vary.
- Blocks must return `(lines, refs)`: the 100-character shared S3 prefix makes
  inline links unreviewable in a diff.

Two API notes for later phases: `SpeciesRepo.vgp_list` is a property, and
`VcfHeader.callers()` / `tool_versions()` are methods. `callers()` is the one to
use for the caller row — `bcftools 1.23.1` appears beside `gatk 4.6.2.0` in the
header, and reading `tool_versions()` naively labels bcftools a variant caller.

**Phase 1 — freeze the harvest schema. Done.** `remote/qc.py` parsers for all
five tables with fixtures; `harvest.py`; the `dataset.json` schema. 79 records
committed, 2.6 MB, harvested in 25 seconds. One `SampleTable` serves all four
per-sample tables — they differ only in key column and value columns.

Two things it found the hard way:

- **`harvested_at` must not count as a change.** Testing idempotence, as this
  document requires, failed immediately: a refresh that finds nothing new rewrote
  all 79 records, burying the one species that moved. `substance()` excludes it
  and `tool_version`, which incidentally makes both mean *when this content was
  first observed* — the more useful reading.
- **An empty species selection must be a misuse exit, not a silent success.** A
  wrong `--metadata-root` otherwise makes a CI job pass vacuously. `validate`
  still has this gap and should get the same treatment.

**Phase 2 — the gate, against the whole corpus. Done.** `gate.py`: both gates,
`GATING_CHECKS` as a pinned public constant, "ran and passed" rather than "did
not fire", the digest cross-check, tier-5 detection, and the accession-moved
check. It reproduces the hand-computed baseline exactly — 53 cited, 17 blocked,
9 truncated — before any rendering exists, which is what retires the
correctness-of-claim risk at zero rendering cost.

**The plan said to assert the counts as a regression target. That was wrong, and
the corpus proved it within a day.** Those numbers move whenever a snpArcher run
finishes; pinning 53/13/13 would have failed for entirely correct reasons the
first time GenomeArk moved. So the suite asserts the gate's **invariants** —
every truncated species states a reason, no full species carries a blocker,
`provenance` is set exactly when citations are blocked, wrongness is claimed only
on a *fired* check, tier 5 ran everywhere — in an opt-in `corpus` suite that
skips when no checkout is reachable. The counts live in this document's baseline
and in `congen readme --gate-report`, which prints them offline on demand.

That is the same lesson the validator learned about `G011` and S3
`LastModified`: assert the property, not the incidental value.

**Phase 3 — the renderer. Done.** Every block but References, the single managed
region, the mode transition, `--check`, `--json`, variadic targets, exit codes.
79 documents written and committed rather than held back until citations land —
the repository is private, and collaborators finding errors is worth more than
avoiding a second corpus-wide diff.

**The manifest question is settled, and the answer is that it does not scale
with the cohort.** Rendered at three real sizes:

| species | samples | lines | secondary files |
|---|---:|---:|---:|
| `apteryx-mantelli` | 19 | 131 | 10 |
| `esox-lucius` | 65 | 145 | 10 |
| `pungitius-pungitius` | 150 | 134 | 10 |

Document length is flat, for a structural reason: the non-`bams/` object count is
constant at about forty across the corpus, and `bams/` — the only part that grows
— is already a single rollup row. So four primary rows plus a collapsed ten holds
at every scale and needs no further thought. (`taeniopygia-guttata` could not
serve as the large case after all: 215 samples in the sheet, but no published
data, so it truncates.)

**Four defects in the prose, every one of them found by reading rendered output
rather than by a test.** Worth recording because the pattern will recur:

- The sheet/BAM/VCF disagreement line was reported for *unpublished* species —
  "215 in the sheet · 0 BAMs · None in the VCF" — which reintroduced the exact
  bug this design records as fixed for `S002`/`S003`. During an absent or partial
  upload the comparison measures how far the upload got, not whether the metadata
  is right. It is now guarded on a complete publication, the same guard for the
  same reason.
- The gate-B provenance line appeared in truncated documents, pointing at a
  `#references` anchor that only exists in a full one. Gate B is now surfaced
  only where there is a References section to reach.
- `sturnus-vulgaris` read "This is an expected state" directly above "validation
  found errors": two blurbs written for different situations colliding. Blockers
  now have a precedence order, so one leads and the rest are named compactly.
- The truncated lead called `anser-albifrons` "planned or in-progress" when it
  has a complete publication and a real defect. There are two leads now, chosen
  by the leading blocker.

References is **omitted at this phase, not stubbed with a gate-B warning** — the
bioproject list is not broken for these species, the tool simply does not do
citations yet, and rendering the blocked wording would state a false reason.
Because these documents are being committed for review, the omission carries one
honest line naming itself as not yet generated: a collaborator seeing no
References section must not conclude that citations were dropped by design.

**Phase 4 — `congen citations`. Done.** The curated file schema and loaders,
`--propose` over Europe PMC through `core.http`, `--report`. Seeded: 298 rows,
all `unreviewed`, 182 of them (61%) carrying a candidate DOI — close to the 68%
the 25-accession probe predicted. Titles and submitters resolved for all 298.

`dataset.json` gained the `sra` field in this phase, which Phase 1 should have
included: the design already said the run → bioproject mapping is committed so
`--report` can rank offline, and the Phase 1 schema omitted it. Re-harvesting
cost 27 seconds rather than the expected ~85, because `Sra.lookup` caches per
accession and the validator's `--check-sra` run had already paid for it.

From here, curating 298 rows is human work that proceeds in parallel with
Phase 5. The queue is ranked by samples affected, worst first.

**Phase 5 — the References block. Done.** Reads the curated files, so the render
stays offline. A **list** rather than a table: a BioProject can carry two
citations, and long titles beside long references make a four-column table
unreadable.

An unreviewed BioProject still gets a row saying so. Hiding it would hide the
gap, and the gap is what gets it filled — which is what the coverage line counts
against, in samples as well as projects.

`tool_citations.yaml` is still entirely `pending`, so the pipeline line names
snpArcher and GATK and says the citations are not recorded yet. Silence would
let them stay missing.

53 of 79 documents changed; the 17 with citations blocked and the 9 truncated
have no block to change. `tautogolabrus-adspersus` is the first fully cited
species, at 150 of 150 samples. `hirundo-rustica` keeps the blocked rendering
despite three confirmed citations, because `G017` means its BioProject list may
be incomplete — the gate behaving correctly, and one `aws s3 cp` from resolving.

**Phase 6 — CI workflows. Done, and running.** `tests.yml` in the tools repo;
`rebuild.yml` in congen-metadata, triggered by pushes that touch source data and
by a manual button for use after a GenomeArk upload. Generated files are absent
from the path filter, so the bot's own commit cannot retrigger it — the loop
guard is structural. The job ends by asserting the rebuild converged.

Two things CI found on its first two runs, both real:

- Bare `pytest` did not work at all. The suite imports `tests.conftest`, which
  needs the repository root importable; `python -m pytest` puts the working
  directory on `sys.path` and bare `pytest` does not. The documented command was
  the broken one.
- `validate` returned a different exit code depending on where it ran, because
  root discovery preceded argument validation and a checkout is discoverable
  from inside the source tree. Argument checks now come first.

And one design error in the workflow itself: it treated `validate`'s exit 1 as a
failure. Exit 1 means *error findings exist*, which is a fact about the metadata
— the corpus has four, awaiting human fixes — and the documents must still be
regenerated, since the generated README is where those errors get announced.
Exit 2 and above still fail the job.

### Constraints held throughout

- **No new runtime dependencies.** Europe PMC goes through `core.http` on the
  standard library. Still no pysam, no boto3.
- **No change to `validate`'s behavior**, and no new or renumbered check IDs.
  Reading its records is the only coupling. `congen validate --all` must produce
  identical findings after this work — the design's Part 1 baseline is also a
  regression target for the validator.
- **Nothing written to `congen-metadata` except `README.md` and `dataset.json`.**
  `README.txt`, `config.yaml` and the sample sheets are read-only to this tool.
- **Tests offline against fixtures**, with live checks marked `network` and
  deselected by default, matching the existing suite.

## Deferred elsewhere

Neither of these blocks any phase.

- **The citable reference for a VGP reference assembly.** Most likely the VGP
  flagship paper; not decided. Held at `status: pending` in
  `tool_citations.yaml`, which is one row and one render branch when the answer
  arrives. See Part 3, References.
- **Sample-level QC thresholds.** Part of the larger filtering and QC
  conversation, deliberately not prejudged here. See Part 3, Sample QC.

## Open questions

- **Do the two `<details>` blocks earn their place?** Confirmed at every scale
  that the split holds mechanically — see Phase 3 — but whether collapsing the
  supplementary facts is the right call, rather than cutting them, is a judgement
  that wants readers. Deliberately left open until collaborators have read a few
  of the 79 committed documents.
- **Is a common name worth adding?** Every `dataset.json` carries
  `common_name: null`, because `AssemblyInfo` does not read it. The heading is
  written to use it when present, so this is one field in `remote/ncbi.py` — but
  it is worth checking first whether the NCBI Datasets response actually supplies
  one for these assemblies, rather than assuming.
