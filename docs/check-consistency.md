# Catalog consistency checking

Status: **reconciled against `scripts/refresh_catalog.py`** (as checked out at `639248ce9`).

Originally a standalone proposal for [#11](https://github.com/uw-cryo/stac-3dep-1m/issues/11) / [#6](https://github.com/uw-cryo/stac-3dep-1m/issues/6).
`refresh_catalog.py` now implements the detection core, so this document keeps only (a) where the two
approaches differ, (b) what neither covers yet, and (c) the attribution question — *why* did a thing change.

## Approach: where we agree

Both landed on the same core, independently, and it is the right one: **paginated `ListObjectsV2` per project
prefix, diffed against the local catalog** — no per-tile HEAD sweep, no sampling. Measured: 946 project
prefixes in 6.4 s at 32 threads. Sampling one tile per collection would have missed `UT_WestEast_B22`
(75 of 760 tiles gone; a sampled tile has ~90% odds of being alive).

`refresh_catalog.py` also goes past what I had planned, in ways worth keeping:

- **It remediates, not just detects.** My plan deliberately stopped at a report. Guardrails
  (`--min-projects`, `--max-removed-tiles`), post-rebuild HEAD gates, and the unbiased existing-URL sample are
  all things a detect-only design would have had to grow anyway.
- **The `carried_empty` restage logic is a better answer than my open question.** I had "delete or flag?"
  unresolved; the never-prune-on-inconclusive-probe rule is the right default given #6 caught the bucket
  mid-repopulation.
- **Both pagination bugs are fixed** — `create_static_stac.py:296` and `create_all.py:13`. Those were real:
  `OR_SouthEast_D22` was the only collection sitting at exactly 1000 items (1165 on S3), and the project
  listing was at 959 against the 1000 cap.

## Approach: where they differ

1. **Item identity comes from the filename stem, not the asset href.** `catalog_state()` keys off `p.stem`;
   `item_asset_url()` is only consulted for probes. I had argued for parsing `assets.elevation.href` because
   that is the field that actually breaks downstream. Verified equivalent on a 1,500-item sample, and cheap
   either way (~35 s to parse all 123,659) — but as written, an item whose href is wrong while its filename is
   right is invisible. Low severity, easy to tighten.

2. **No internal-consistency check.** Items listed in `collection.json` but absent on disk (or vice versa),
   collections missing from root `catalog.json`, hrefs pointing at the wrong collection. This is the free,
   no-network half of the check, and it catches *our* bugs rather than USGS's. `refresh_catalog.py` maintains
   these invariants when it runs, but nothing verifies them.

3. **Diff granularity is the tile set only.** Which is correct for existence — and is exactly why the two gaps
   below are invisible to it.

## Gap 1: content drift (tile reprocessed in place)

Same key, new pixels — no add, no remove, so a set diff cannot see it. Arguably the worst failure mode, since
sampling silently returns stale elevations with no error anywhere.

The listing already returns `Size` and `ETag` for free; neither script records them. `LastModified` alone is
useless here — every tile in `WA_KingCounty_2021_B21` reads `2026-02-14`, which is the bulk prefix rewrite
noted in #6, not per-tile reprocessing.

Fix: add `file:size` + `file:checksum` to each asset at creation time (STAC `file` extension), so the catalog
carries its own drift baseline and downstream consumers can tell whether their copy is current. Backfill is
123k items. Alternative — a sidecar `checkpoints/s3-manifest.parquet` diffed run over run — is cheaper but
adds a second source of truth.

## Gap 2: WESM metadata drift — **closed** ([#18](https://github.com/uw-cryo/stac-3dep-1m/issues/18))

`refresh_catalog.py` used to rebuild a project only when its *tile set* changed. Collection summaries carry
`wesm:*` snapshotted at build time, and every item's `start_datetime`/`end_datetime` derives from WESM
`collect_start`/`collect_end` — the exact field SlideRule's AMS table keys on. A WESM revision that touched no
tiles therefore never reached the catalog.

`refresh` now diffs the snapshotted summaries against live `WESM.csv` on every run and repairs the drifted
collections **in place** (`wesm_diff` / `refresh_collection_metadata`): summaries, collection temporal extent,
and item datetimes only when the collect range itself moved. No titiler round trip — the answer to open
question 4 below is *metadata-only refresh*, and the guardrail is `--max-metadata-updates` (300) since a WESM
schema change would drift every collection at once.

The backlog this cleared was larger than the original measurement suggested — 192 of 946 collections, because
the drift includes not only USGS revisions but the `collapse_workunit_rows` fix (deterministic representative
row, min/max collect range over multi-workunit projects) that collections built before it never picked up:

| Field | Collections drifted |
| --- | --- |
| `workunit` / `workunit_id` | 115 |
| `lpc_pub_date` | 108 |
| `sourcedem_pub_date` | 106 |
| `collect_end` | 105 |
| `horiz_crs` | 44 |
| `sourcedem_update` | 33 |
| `dem_gsd_meters` | 30 |
| `lpc_update` | 22 |
| `ql` | 19 |
| `*_category` / `*_reason` | 13–14 each |
| `spec`, `vert_crs`, `geoid`, `p_method` | 3–8 each |

105 of those moved a collect range, i.e. ~39k item files whose `start_datetime`/`end_datetime` were wrong.
**Superseded (2026-08-12):** an earlier revision of this note claimed no cataloged collection was unresolvable
in WESM, and that `UT_StrawberryRiver_2019` "resolves again". It does not — it has no workunit *or* project row
in the live `WESM.csv`, and it is the only such collection of the 935 cataloged.

Note that the reported missing-list could not have caught it either way: `wesm_diff()` was only handed
`local - changed - removed`, and a project with a tile-set delta is in `changed`, so a collection that is *both*
drifting on S3 and absent from WESM never reached the check. Such a collection is now retired and pruned rather
than reported (issue #23) — see "WESM is the source of truth for membership" in `CLAUDE.md`.

## Attributing *why* something changed

> **Implemented (issue #19).** The analysis below is now code: `classify_removal()` in
> `scripts/refresh_catalog.py` labels every prune candidate, and only the affirmative classes auto-prune.
> See the label table in `CLAUDE.md`. The rest of this section records how the labels were derived.

The `--dry-run` output says *what* moved, never *why*. That gap is real, but it is largely fillable — just not
from the field you would first reach for.

**WESM's `*_reason` fields do not explain removals.** They describe spec compliance, not staging status. Every
one of the vanished `UT_FemaHQ_*` workunits still reads `onemeter_category = Meets`,
`onemeter_reason = "Meets 3DEP 1-m DEM requirements"` while its TIFFs are gone. Taking `_reason` at face value
would actively mislead.

Four signals that *do* attribute, in descending order of confidence:

**1. Withdrawn for cause — `onemeter_category` transition.** Affirmative and machine-readable.
`LA_BretonIslandTB_D24` went `Meets` → `Does not meet` (`"LPC does not meet"`) and is one of the 11 dead
collections. The source point cloud failed QA, so USGS pulled the 1 m product. This explains 1 of 11.
(The other 14 category changes all move *toward* validity — `Pending publication` → `Meets` — which is a
publication event, not a withdrawal.)

**2. Renamed / consolidated — grid-cell overlap against current holdings.** Not in WESM at all; derivable only
by comparing `xNNyNNN` cells to what is staged now. USGS is collapsing workunit-level folders into
project-level ones:

| Dead collection | Replacement | Cells recovered |
| --- | --- | --- |
| `AZ_BrawleyRillito_FEMA_2018` | `AZ_BrawleyRillito_2018_D19` | 80/80 |
| `UT_FEMAHQ_B2_2018` | `UT_FEMAHQ_2018_D18` | 48/48 |
| `NV_LasVegas_QL2_2016` | `NV_Las_Vegas_Region_2016_A16` | 51/51 |
| `UT_FemaHQ_B1_TL_2018` | `UT_FEMAHQ_2018_D18` (partial) | 22/85 |
| `NM_SantaFeCo_2014` | several newer `NM_*` | 54/90 |

Strongly correlated with a naming-convention migration: **8 of the 11 dead collections are 100 %
legacy-named** (`USGS_one_meter_*`, no UTM-zone token) against a 27.6 % catalog-wide baseline. USGS appears to
be restaging legacy products under the modern `USGS_1M_<zone>_*` convention and deleting the old files.
Note this also breaks naive tile-id comparison across collections — the legacy names carry no zone, so any
cell-overlap logic has to parse both conventions.

**3. TIFFs missing from an otherwise intact product folder — companion-file count.** Derived purely from the
S3 listing already performed; **do not use WESM for this** (see the caveat below). Where the project folder
survives, compare the surviving `browse/` and `metadata/` companion counts to our item count:

| Collection | Catalog items | Remnants | All rewritten |
| --- | --- | --- | --- |
| `NM_SantaFeCo_2014` | 90 | 90 browse + 90 metadata (+ `test_temp.gmc`) | 2026-05-21 |
| `NV_LasVegas_QL1_2016` | 30 | 30 browse + 30 metadata | 2026-02-14 |
| `NV_LasVegas_QL2_2016` | 51 | 51 browse + 51 metadata | 2026-02-14 |
| `LA_BretonIslandTB_D24` | 5 | none (one stray `.vrt`) | 2026-05-14 |

A one-to-one thumbnail and XML for every missing TIFF, all stamped with a single rewrite date, means the whole
folder was re-staged and only the TIFF payload failed to land. The one collection WESM *does* explain
(`LA_BretonIslandTB_D24`, withdrawn for cause) is also the one with no companions left — a clean withdrawal.
So the signal discriminates.

It does **not** establish that a rebuild is in flight: the two `NV_LasVegas_*` folders were rewritten on
2026-02-14 — the same mass re-staging event behind #6 — and six months later the TIFFs still have not
returned. Label these `tiffs-missing-from-intact-folder`, not "restaging".

**Caveat on WESM `*_update` dates — do not use them for this.** I originally read
`NM_SantaFeCo_2014`'s `sourcedem_update`/`lpc_update` moving to `2026/05/15` as "the source data was revised,
so the 1 m product is being regenerated". That does not survive checking the data dictionary:

- Both fields describe the **LPC and OPR source-DEM products, not the 1 m derivative**. WESM has no
  `onemeter_update` or `onemeter_pub_date` field at all — it carries *no* attribute describing 1 m staging
  status.
- `sourcedem_update` is defined as "The date an update was made to either the storage path **or** one or more
  source DEM data files" — a path move alone bumps it, which is exactly what a bulk re-stage does.
- `onemeter_category = "Meets"` means "Project/WU meets 3DEP specification(s) for the product type" —
  **spec compliance, not availability**. It does not assert that a 1 m product is staged.

The per-tile XMLs left behind are also unchanged in content (`pubdate 20200330`, `procdate 20190425`), so
nothing was actually revised — the files were re-uploaded.

Bottom line for `NM_SantaFeCo_2014`: **WESM cannot tell us whether it was erroneously added or deliberately
removed** — it has no field that speaks to 1 m staging. The available evidence is circumstantial and points to
an incomplete re-stage rather than a withdrawal, but it does not rule out a delisting that left companion
files behind. The `--dry-run` verdict `treating as removed` is therefore *unproven*, not demonstrably wrong.

**4. Workunit retired from WESM.** `UT_StrawberryRiver_2019` is simply gone from `WESM.csv` — unambiguous.

**Residual: genuinely unexplained.** `UT_FemaHQ_B1_TL_2018` (63 of 85 cells with no replacement) and
`UT_WestEast_B22`'s 75 vanished tiles show no WESM change, still `Meets`, no consolidating project. These are
the cases worth raising with USGS rather than silently pruning — and they line up with #11's "0.9 % of the AOI
is covered only by dead URLs".

### Proposal

Add an `--explain` pass that annotates each removal with one of `withdrawn-for-cause`, `superseded-by:<project>`,
`tiffs-missing-from-intact-folder`, `retired-from-wesm`, `unexplained`, using signals 1–4. Cheap — WESM is one
CSV already fetched per build, and both the cell index and the companion counts fall out of the S3 listing
already performed. Feed it into the PR body so a reviewer sees *why*, and gate pruning on it: never auto-prune
`tiffs-missing-from-intact-folder` or `unexplained`.

Note the labels are evidence classes, not statements of USGS intent — only `withdrawn-for-cause` and
`retired-from-wesm` rest on an affirmative USGS assertion.

## Open questions

1. **Should `tiffs-missing-from-intact-folder` and `unexplained` removals be pruned at all?** Current policy
   prunes both once probes 404. `NM_SantaFeCo_2014` would be pruned today; if USGS is mid-restage it would
   have to be rebuilt later, but the `NV_LasVegas_*` pair has sat in that state for six months, so waiting
   indefinitely is not obviously better. Worth asking USGS directly rather than inferring — there is no
   metadata field that answers it.
2. **Deleted or deprecated?** Still open. Deleting is what #11 asks for and is safest for consumers; a
   `superseded`-link approach preserves the record but pushes filtering onto every consumer.
3. **Which artifact is checked — `catalog/` or the release parquet?** `main` runs ahead of the tag that
   SlideRule's AMS table is built from, so "the repo is right" and "what people use is right" are different
   questions.
4. ~~**Does WESM drift trigger a full titiler rebuild, or a metadata-only collection refresh?**~~ Resolved:
   metadata-only refresh, implemented in `refresh_catalog.py` (see Gap 2 above).
