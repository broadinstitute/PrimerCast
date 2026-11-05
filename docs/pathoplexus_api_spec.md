# Pathoplexus as a sequence source — API spec

Reference for adding Pathoplexus alongside NCBI (`gget virus`) as a fetch source for
`adapt monitor` and the GUI. Everything below was verified against the live API on
2026-11-03 (`lapisVersion 0.8.0`, `siloVersion 0.11.0`).

## 1. The API

Pathoplexus serves its data through [LAPIS](https://github.com/GenSpectrum/LAPIS),
backed by SILO. One base URL per organism:

```
https://lapis.pathoplexus.org/{organism}/sample/{endpoint}
```

`databaseConfig` reports `opennessLevel: OPEN` — **no authentication, no API key, no
account**. Unlike GISAID there is no data-access agreement gating server-side fetching,
so this can run unattended under the Cloud Run service account.

### Endpoints we need

| Endpoint | Use |
| --- | --- |
| `aggregated` | Counts. `?fields=a,b` groups by those fields. Cheap way to size a query before pulling it. |
| `details` | Metadata rows. `?fields=…` selects columns, `?dataFormat=tsv\|csv\|json`. |
| `unalignedNucleotideSequences[/{segment}]` | FASTA. Submitter-provided sequence, not an alignment. |
| `referenceGenome` | Segment names for the organism. |
| `databaseConfig` | Field list + `primaryKey` + `opennessLevel`. |

Also live: `/api-docs`, `/swagger-ui/index.html`, `/sample/info`.
(`/v3/api-docs` and the LAPIS host root are 404.)

All endpoints accept **GET with query params or POST with a JSON body**. POST matters:
it is how we request sequences for a specific list of accessions without hitting URL
length limits.

```bash
curl -X POST https://lapis.pathoplexus.org/ebola-zaire/sample/details \
  -H 'Content-Type: application/json' \
  -d '{"accessionVersion":["PP_000LAWQ.4","PP_000LAXN.4"],"fields":["accessionVersion","length"]}'
```

### Filter grammar

A field named `x` yields filter keys `x` (exact match) plus, by type:

- date / numeric → `xFrom`, `xTo` (inclusive range), `x.isNull`
- string → `x.regex`, `x.isNull`

427 valid keys for ebola-zaire. An unrecognised key returns **HTTP 400 with the full
valid-key list in `error.detail`** — the fastest way to check a field name.

Multiple values for one key are OR'd; different keys are AND'd.

## 2. Organisms

**Decision: enable all 16.** Each is its own URL path segment; there is no per-organism
work beyond the name, so a subset would only be an arbitrary restriction. Counts below
are `versionStatus=LATEST_VERSION&dataUseTerms=OPEN` as of 2026-11-03 — see §3 for the
restricted counts to add on top (notably `ebola-bdbv`, where they dominate):

| organism | sequences | segments |
| --- | ---: | --- |
| `andv` | 645 | L, M, S |
| `cchf` | 3 244 | L, M, S |
| `chikungunya` | 11 415 | main |
| `dengue` | 59 715 | DENV-1…4 |
| `ebola-bdbv` | 80 | main |
| `ebola-sudan` | 166 | main |
| `ebola-zaire` | 3 800 | main |
| `hmpv` | 15 450 | main |
| `marburg` | 418 | main |
| `measles` | 30 290 | main |
| `mpox` | 15 223 | main |
| `rsv-a` | 41 599 | main |
| `rsv-b` | 31 369 | main |
| `west-nile` | 9 062 | main |
| `yellow-fever` | 2 303 | main |
| `zika` | 3 084 | main |

**The mastersheet has no column for this.** Rows are keyed on `TaxID`, and organism is
not a function of TaxID: ebola-zaire alone spans 186538 (11 378), 1570291 (617),
128952 (22), 128947 (4), 3052462 (4) and 81 rows with no TaxID at all. A new
`pathoplexus_organism` column is required; it cannot be derived.

Dengue's "segments" are serotypes, not genome segments — one sequence per record, in
whichever serotype slot it matched.

## 3. Field mapping

Mastersheet columns that have a Pathoplexus equivalent:

| mastersheet column | LAPIS filter |
| --- | --- |
| `min_release_date` | `earliestReleaseDateFrom` |
| `max_release_date` | `earliestReleaseDateTo` |
| `min_collection_date` | `sampleCollectionDateRangeUpperFrom` (see below) |
| `max_collection_date` | `sampleCollectionDateRangeLowerTo` (see below) |
| `min_seq_length` | `lengthFrom` |
| `max_seq_length` | `lengthTo` |
| `max_ambiguous_chars` | `totalAmbiguousNucsTo` |
| `geographic_location` | `geoLocCountry` (also `geoLocAdmin1/2`, `geoLocCity`) |
| `host` | `hostNameScientific` (or `hostTaxonId`) |
| `nuc_completeness` | — approximate with `lengthFrom` |

No equivalent: `refseq_only`, `annotated`, `lab_passaged`, `is_sars_cov2`,
`is_alphainfluenza`, `genbank_metadata`, `proteins_complete`. A Pathoplexus fetch must
reject or ignore these rather than silently dropping them.

### Collection dates are partial

`sampleCollectionDate` is a **string**, not a date — it holds whatever precision the
submitter gave. For ebola-zaire's 966 distinct values: 927 full dates, 25 year-month
(`2002-12`), 12 year-only (`1976`), 2 malformed. It therefore has no `…From`/`…To`
filters, only `.regex` and `.isNull`.

LAPIS derives two real date fields from it, and those are range-filterable:
`sampleCollectionDateRangeLower` and `…RangeUpper` — the first and last day the record
*could* have been collected (`2002-12` → `2002-12-01` / `2002-12-31`; an exact date
gives Lower == Upper).

So a collection-date bound has to pick a reading of coarse records:

- **permissive** — the record *might* satisfy the bound: `min` → `…RangeUpperFrom`,
  `max` → `…RangeLowerTo`
- **strict** — the record *certainly* satisfies it: `min` → `…RangeLowerFrom`,
  `max` → `…RangeUpperTo`

Recommend permissive, matching the table above. Strict silently discards every
year-only record, and those are disproportionately the old reference isolates a primer
panel most wants to cover.

### The two release dates

This is the one real trap in the API:

| field | meaning | range filter? |
| --- | --- | --- |
| `earliestReleaseDate`, `ncbiReleaseDate` | first public *anywhere* — for INSDC-mirrored records this is the original GenBank date, going back to **1993** | yes (`…From`/`…To`) |
| `releasedDate`, `submittedDate` | when this version appeared **in Pathoplexus** — only 22 distinct values, starting at the 2024-10-29 backfill | **no** (string field: only `.regex`, `.isNull`) |

So:

- Use `earliestReleaseDateFrom` for the user's *scientific* window ("sequences released
  since 2024").
- Do **not** use it for incremental monitoring. A record ingested into Pathoplexus today
  but originally deposited in GenBank in 1998 carries `earliestReleaseDate=1998` and
  would be invisible to a `…From=<last run>` query.
- The field that does mean "new since last check" (`releasedDate`) is not
  range-filterable.

**Therefore: no server-side date narrowing for incremental fetches.** See §5.

### Versions

`accessionVersion` is the primary key, and *every revision is a separate record*. For
ebola-zaire: 12 106 records total, of which 8 303 are `REVISED` and only 3 803 are
`LATEST_VERSION`. **Every query must pass `versionStatus=LATEST_VERSION`** or we
download each sequence's full revision history and treat superseded versions as real
candidates.

### Data use terms

`dataUseTerms` is `OPEN` or `RESTRICTED`. RESTRICTED carries
`dataUseTermsRestrictedUntil` and `dataUseTermsUrl`, and converts to OPEN automatically
on that date — the submitter sets the period, capped at one year.

**Decision: fetch both.** RESTRICTED is not a GISAID-style access wall. The terms say it
is "available to all users, on condition of acceptance of the Data Use Terms" — no
registration, no credentials, no per-user agreement to countersign. The obligations are
attribution and onward-sharing hygiene, which fall mostly on the user publishing a
paper.

Excluding it would also be scientifically wrong, because it is not rare. Current
records, by organism:

| organism | restricted | organism | restricted |
| --- | ---: | --- | ---: |
| `dengue` | 2 951 | `rsv-a` | 377 |
| `measles` | 1 804 | `rsv-b` | 357 |
| `ebola-bdbv` | 1 222 | `west-nile` | 207 |
| `hmpv` | 467 | `andv` | 44 |
| `mpox` | 345 | `zika` | 5 |

(plus `ebola-zaire` 3, `cchf` 2, `yellow-fever` 2; 7 786 total. `chikungunya`,
`ebola-sudan`, `marburg`, `rsv`… the rest are 0.)

`ebola-bdbv` is the case that settles it: **1 222 restricted against 80 open.** Filtering
to OPEN would hide 94% of that organism, including essentially all recent material.

#### What including it obliges *us* to do

These are requirements on the software, not on the user, and they need building:

1. **Carry the terms columns through.** "Keep the Data Use Terms columns
   (`dataUseTerms`, `dataUseTermsRestrictedUntil`, `dataUseTermsUrl`) intact in the
   metadata." The fetcher must emit all three in its metadata CSV and nothing
   downstream may drop them.
2. **Link and accession on display.** "If displayed on a website or in another database,
   each sequence must have a direct link to the original sequence page on Pathoplexus
   and display the Pathoplexus accession." This hits `_get_new_seq_table` in the GUI and
   the monitor email — both list sequences. Needs a link column, not just the bare ID.
   The URL is `https://pathoplexus.org/seq/<accessionVersion>` (verified 200; no
   organism path segment — `/{organism}/seq/…` is a 404. Dropping the version still
   works but 302-redirects to the current one, so emit the full `accessionVersion`).
3. **State the terms where the data is shown.** A line on any results view or report
   containing restricted records, pointing at `dataUseTermsUrl`.
4. **No access restrictions on re-sharing.** We impose none, so this is satisfied — but
   it rules out gating restricted results behind a future login.

Worth surfacing in the UI: restricted records become OPEN on
`dataUseTermsRestrictedUntil`, so a panel built today against restricted data is
publishable without conditions after that date.

## 4. Output formats

FASTA headers are the bare `accessionVersion`, one whitespace-free token:

```
>PP_000LAAX.3
ATACGATGAAGATTAAGGCGACAACGAGCCGAAACTT…
```

That is already compatible with `_extract_accessions` (`adapt_cli.py:669`), which takes
the first whitespace token of each `>` line.

**Segmented organisms need header rewriting.** `unalignedNucleotideSequences/L` and
`/M` both emit the bare accession, so concatenating segments produces duplicate IDs. A
fetcher merging segments must suffix them (`>PP_000PPLR.4_L`). Mastersheet rows already
carry a `Gene Segment` column, so the simpler route is one row per segment, fetching a
single segment each.

`details?dataFormat=tsv` gives the metadata table. The monitor's `_get_new_seq_table`
expects an `accession` column plus `Length`, `Geographic Region`, `Release date`, so a
fetcher should emit a CSV with those names rather than the raw LAPIS ones. Note that
`geoLocCountry` and `sampleCollectionDate` are frequently empty.

## 5. Integration shape

The existing monitor already does accession-set diffing — `_extract_accessions` →
`new_accessions`. Date narrowing (`_narrow_to_new_releases`) exists only as a bandwidth
optimisation for gget, and it is exactly the thing Pathoplexus's date semantics break.

Proposed Pathoplexus fetch, per run:

1. `GET details?versionStatus=LATEST_VERSION&dataUseTerms=OPEN&<user filters>&fields=accessionVersion&dataFormat=tsv`
   — one cheap ID-only pull, 3 800 rows for ebola-zaire, ~60 k for dengue.
2. Diff against the previous run's accession file.
3. `POST unalignedNucleotideSequences` with just the new `accessionVersion` list.
4. `POST details` with the same list for the metadata CSV.

This sidesteps the late-ingestion problem entirely: anything newly present is new,
regardless of what date it carries. It also removes the 7-day overlap fudge
(`_MONITOR_OVERLAP_DAYS`) for this source.

Step 1 must request the ID-only pull with `dataUseTerms` omitted (both tiers) and
`versionStatus=LATEST_VERSION` set.

### Revisions count as new

**Decision: diff on `accessionVersion`.** A revision mints a new `accessionVersion`, so
this is the no-extra-work option, and it is the right default for primer design — a
revision can change the sequence itself, and a panel validated against v3 is not
necessarily valid against v4.

It does come with an operational cost that needs designing around. Revisions on
Pathoplexus are dominated by **bulk pipeline re-processing, not submitter corrections**.
Current ebola-zaire records by version: v1 109 (2.9%), v3 3 051 (80.2%), v4 439, v5 132,
v6 71 — i.e. 97% are revisions. And they land in clusters: 158 revisions over
2025-12-22…24, then 70 on 2026-10-11.

So a re-ingest day makes the monitor report hundreds to thousands of "new" sequences
when nothing was actually discovered. Unmitigated, that is indistinguishable from an
outbreak signal in the email and trains users to ignore the report.

Mitigation — still diff on `accessionVersion`, but **split the report into two
sections**, using the version-stripped `accession` against the prior run's history:

- *New sequences* — base accession not seen before. The real signal.
- *Revised sequences* — base accession seen, version bumped.

Both are re-analysed; only the headline count changes. Cheap, since the base accession
is just `accessionVersion.rsplit(".", 1)[0]`, and it keeps a 3 000-record re-ingest from
masking the five genuinely new genomes that arrived the same day.

Optional refinement if that proves too noisy: we already store the fetched FASTA per
date directory, so a revision can be checked for whether the *sequence* changed or only
the metadata, and metadata-only revisions reported separately again.

### Deduplication hazard

`_deduplicate_fasta` is content-based and **keeps the first occurrence**. If a target
ever draws from both NCBI and Pathoplexus, the same physical sequence arrives twice
under two different IDs (`MN908947.3` vs `PP_000LAWQ.4`), and which accession survives
depends on concatenation order. Unstable order means accessions churn between runs and
the monitor reports phantom "new" sequences.

Two requirements if sources are ever combined:

- Concatenate sources in a fixed, documented order.
- Use `insdcAccessionBase` (present on every INSDC-mirrored record, e.g. `AF086833`) to
  reconcile Pathoplexus records against NCBI ones before dedup, rather than relying on
  sequence identity.

Keeping the two sources on separate mastersheet rows avoids all of this and is the
recommended first iteration.

### Code placement

Mirrors the existing external-tool pattern: `src/primercast/external/pathoplexus.py`,
alongside `gget_virus.py`. Unlike gget it needs no subprocess or conda package — it is
plain HTTP — so it can be a module-level function rather than a CLI wrapper. The
dispatch point is `_build_gget_command` (`adapt_cli.py:406`), which would become a
source-aware builder keyed on a `source` column defaulting to `ncbi`.

## 6. GISAID

No equivalent is possible. GISAID has no open API; access is granted to a *registered
individual* under the Database Access Agreement, credentials cannot be shared, and
redistribution to parties who have not themselves agreed is prohibited. PrimerCast on
Cloud Run has no user accounts and one shared service account, so any server-side
GISAID fetch would breach those terms. The blocker is licensing, not engineering.

The practical option is a user-supplied-FASTA path: the user downloads from GISAID under
their own credentials and uploads, which the GUI's upload flow already supports.

## 7. Worked example

The four steps above, run against the live API on 2026-11-03. Reproduces as written.

```bash
B=https://lapis.pathoplexus.org/ebola-zaire/sample
COMMON="versionStatus=LATEST_VERSION&dataUseTerms=OPEN"

# 1. ID-only pull (here with a near-full-genome length filter) -> 3510 rows
curl -s "$B/details?$COMMON&lengthFrom=18000&fields=accessionVersion&dataFormat=tsv" > ids.tsv

# 2. diff ids.tsv against the previous run's accession file -> $NEW

# 3. sequences for just the new accessions
curl -s -X POST "$B/unalignedNucleotideSequences" -H 'Content-Type: application/json' \
  -d '{"accessionVersion":["PP_000LAXN.4","PP_000LAYL.4","PP_000LAZJ.4"]}'
# >PP_000LAXN.4
# ATGGATTCTCGTCCTCAGAAAGTCTGGATGACGCCGAGTCTCACTGAATCTGACATGGAT…

# 4. metadata for the same list
curl -s -X POST "$B/details" -H 'Content-Type: application/json' \
  -d '{"accessionVersion":["PP_000LAXN.4","PP_000LAYL.4","PP_000LAZJ.4"],
       "fields":["accessionVersion","length","geoLocCountry","earliestReleaseDate","insdcAccessionBase"],
       "dataFormat":"csv"}'
# accessionVersion,length,geoLocCountry,earliestReleaseDate,insdcAccessionBase
# PP_000LAXN.4,18959,,1999-02-10,AF086833
# PP_000LAYL.4,18959,,2000-12-14,AF272001
# PP_000LAZJ.4,18960,,2002-07-07,AF499101
```

Note the empty `geoLocCountry` on all three and the `earliestReleaseDate` values from
1999–2002 on records Pathoplexus itself only published in 2024 — the two traps from
§3 showing up in real output.

## 8. Decisions

Settled 2026-11-03:

| question | decision | consequence |
| --- | --- | --- |
| Which organisms? | All 16 (§2) | New `pathoplexus_organism` mastersheet column; per-segment rows for `andv`/`cchf` |
| Restricted data? | Fetch it, alongside OPEN (§3) | Four display/metadata obligations to build — terms columns, per-sequence Pathoplexus link, terms notice, no access gating |
| Revisions? | Report as new; diff on `accessionVersion` (§5) | Report must split *new* from *revised* so bulk re-ingests don't look like outbreaks |

### Build order

1. ✅ `src/primercast/external/pathoplexus.py` — the two-phase fetch (§5).
2. ✅ Source dispatch in `_monitor_fetch`, on a `source` column defaulting to `ncbi`;
   gget-only flags rejected rather than dropped silently.
3. ✅ New-vs-revised split, in the diff and the monitor email. Applied to NCBI too —
   same logic, and GenBank revises as well.
4. ✅ Restricted-data obligations in `_get_new_seq_table` and `_data_use_notice`.
5. ⬜ GUI: organism picker, source selector.

Tests: `tests/test_pathoplexus.py` (37, no network — `_request` is stubbed).

### Two mastersheet columns

| column | values |
| --- | --- |
| `source` | `ncbi` (default) or `pathoplexus` |
| `pathoplexus_organism` | one of the 16 in §2 |

Segmented organisms reuse the existing `Gene Segment` column, one row per segment.

### Per-segment metadata fields

Found while building, and not visible from the ebola-zaire schema the rest of this spec
was mapped against. On organisms whose genome really is multi-segment (`andv`, `cchf`),
LAPIS splits the per-segment metadata into one field per segment — `length_L`,
`totalAmbiguousNucs_M`, `insdcAccessionBase_S` — and the unsuffixed names **do not
exist**, so a query using them 400s. Affected: `length`, `totalAmbiguousNucs`,
`insdcAccessionBase`, `insdcAccessionFull`, `insdcVersion`, `completeness`, `lineage`,
`ncbiUpdateDate`.

Dengue does *not* do this despite having four "segments", because they are serotype
slots on a single sequence — it keeps the plain `length`. So the suffix rule keys on the
organism, not on whether a segment was given (`SEGMENTED_METADATA` in the module).

The fetcher suffixes on the way out and normalises back on the way in, so the metadata
CSV has a plain `Length` column whatever the organism.
