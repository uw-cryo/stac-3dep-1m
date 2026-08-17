# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Which files to consider

Only consider tracked files in the repository. Do not consider untracked files, even if they are in the same directory as tracked files.

## Filing issues

**Never open an issue in an upstream repository.** Not titiler, rio-tiler, rio-stac, GDAL, numpy, pystac, or anything else outside this repo — no exceptions, and no "it's clearly a bug there" shortcut. When something looks like an upstream bug, ask first, then offer a write-up (minimal reproduction, versions tested, expected vs actual) for the user to file manually under their own account.

Issues in *this* repo are fine when the user asks for one.

## Python Environment

Always use `pixi run python ...` when running Python commands. Do NOT create virtual environments (no `python -m venv`, `conda create`, `uv venv`, etc.). All Python execution should go through pixi.

## Commands

Defined as pixi tasks in `pixi.toml` (`pixi task list` shows descriptions):

```bash
pixi run create-stac --project WA_KingCounty_2021_B21 --overwrite  # one project (or --workunit)
pixi run create-all                # every S3 project folder not already in catalog/
pixi run refresh --dry-run         # diff catalog/ vs S3, report only
pixi run refresh                   # diff + rebuild/prune + validate (mutates catalog/)
pixi run backfill-file-metadata    # re-stamp file:size/file:checksum from S3 (idempotent)
pixi run audit                     # 3 tiles/collection: would a rebuild change them?
pixi run audit --project <ID>      # one collection, every tile
pixi run audit --all-tiles --part 3/12   # one runner's share of a split audit
pixi run audit --merge-reports <dir>     # combine the parts of a split audit
pixi run check-catalog             # catalog vs itself (~55 s, whole catalog)
pixi run check-catalog --project <ID> --check schema   # one collection, one check
pixi run check-catalog --parquet catalog.parquet       # + the built artifact, row for row
ulimit -n 500000 && pixi run catalog2geoparquet   # full catalog -> catalog.parquet
pixi run collection2geoparquet catalog/<ID>/collection.json out.parquet
pixi run wrap-wesm-summaries       # wesm:* summaries -> one-element lists (idempotent)
pixi run update-root-catalog       # link new children into catalog/catalog.json
pixi run list-collections          # regenerate collections.txt
pixi run ruff                      # ruff check --fix + ruff format
pixi run lint                      # non-mutating check + format --diff (the CI gate)
```

Ruff is scoped to `scripts/` by `include` in `ruff.toml` — `notebooks/` and `checkpoints/` are exploratory and several cells hold pasted shell output ruff cannot parse, so never widen that scope to "fix" a lint error there.

There is no test suite. `catalog2geoparquet` opens one file handle per item (~124k), hence the `ulimit -n` bump.

Local `refresh` runs abort *after* modifying the working tree if validation fails (CI gates the commit separately, so nothing is published there). Recover with:
`git restore catalog collections.txt && git clean -fd catalog`

## Architecture

The repository *is* the product: `catalog/` holds ~124k committed STAC Item JSONs (one per USGS 3DEP 1m GeoTIFF) plus a `collection.json` per project. `catalog.parquet` / `catalog.gti` are never committed (gitignored) — they are built in CI and published as release assets.

Data flow:

1. **S3 listing** — `s3://prd-tnm/StagedProducts/Elevation/1m/Projects/<project>/TIFF/*.tif`, unsigned boto3, **always paginated** (a bare `list_objects_v2` truncates at 1000 keys and has silently dropped tiles from large projects before). Tile lists come from listing `TIFF/` directly, *not* from `0_file_download_links.txt` — that file is missing for some projects and contains duplicates/wrong case in others.
2. **STAC Item generation** — `scripts/create_static_stac.py` POSTs each TIFF URL to a TiTiler `/cog/stac` endpoint (currently a private Lambda URL constant in the script). Requests are batched async (100 at a time) with bounded retries. Two known server responses are handled specially: "Too many bins for data range" → retry with `with_raster=false`; 5xx → short sleep and retry.

   **Every batched request goes to our own deployment.** The canonical public `titiler.xyz` is significantly rate limited — it is fine for a one-off spot check (confirming an endpoint's behavior, comparing versions) and unusable for anything per-tile. Ours is pinned old at **titiler 0.22.4** (rasterio 1.4.3, GDAL 3.9.3) against 2.2.1 upstream; upgrading it is issue #22, and `/healthz` on either reports the versions. When a behavior looks like a titiler quirk, check it against `titiler.xyz` once before assuming it is our version: the `asset_media_type` and `/cog/validate` behavior in issue #33 is byte-identical on both, so that one is not staleness.
3. **Collection metadata** — joined from USGS `WESM.csv` (`s3://prd-tnm/StagedProducts/Elevation/metadata/WESM.csv`) and attached to `collection.summaries` with a `wesm:` prefix, **each value a one-element list**. Summaries were chosen over `extra_fields` because that is the block STAC Browser renders (Mar 2025, "render wesm metadata on collection page"), but the values were bare scalars until 2026-08-17, which is illegal — a summary value must be a JSON Schema, a set, or a Range, so every collection failed validation on its first `wesm:` field. Wrapping each in a list makes it a legal one-value set and keeps the rendering; `wesm_summary_fields()` emits that form and `wesm_value()` reads one back (tolerating the old spelling). `scripts/wrap_wesm_summaries.py` migrated the 936 committed collections. Layout is flat/self-contained (`TemplateLayoutStrategy(item_template="")`, `CatalogType.SELF_CONTAINED`) so `catalog/` is relocatable.
4. **Aggregation** — `catalog2geoparquet.py` walks the root catalog with `rustac` and writes zstd STAC-GeoParquet; `collection2geoparquet.py` embeds collection metadata for a single collection. `update_root_catalog.py` adds child links and regenerates the `catalog/collection.json` "All Cataloged" meta collection (union bbox + temporal extent across all collections).

### Project vs. workunit naming

The S3 folder name is sometimes the WESM *project*, sometimes the *workunit*, and sometimes neither. Callers try `--workunit` first, then fall back to `--project` (see `create_all.py` and `refresh_catalog.py:build_project`). Irreconcilable names live in the hand-curated `onemeter_folder_to_wesm` dict in `create_static_stac.py` — a build failure in CI usually means a new folder needs an entry there. When a project spans multiple workunits (~214 of 909 collections), `collapse_workunit_rows` takes min(collect_start)/max(collect_end) rather than an order-dependent `.iloc[0]`.

Some folders have no TIFFs at all; those are listed in `NO_TIFFS` in `create_all.py`. `NOTES.md` records the upstream quirks behind these workarounds — read it before "fixing" something that looks anomalous.

### Schema uniformity

`normalize_projection()` rewrites projection extension v1.x (`proj:epsg`) to v2.0.0 (`proj:code`) so the whole catalog — and therefore the GeoParquet column schema — stays uniform regardless of which TiTiler version answered. Anything that changes Item properties has to keep all ~124k items consistent or the parquet build produces a mixed schema.

Every item also carries the STAC `file` extension's `file:size` and `file:checksum` on its `elevation` asset, stamped by `add_file_metadata()` (see below). Same rule applies: all ~124k or none.

### JSON serialization is pinned to the stdlib

pystac picks its serializer by what happens to be importable — orjson if present, the stdlib otherwise — and the two disagree on float repr: orjson writes `0.00009924415650406505` and `-3.4028230607370965e38` where the stdlib writes `9.924415650406505e-05` and `-3.4028230607370965e+38`. The catalog was built across environments that differed, so it accumulated **both** spellings (~18.4k items stdlib, 32 orjson, one file mixed) and an in-place edit silently reformatted whatever it touched.

`create_static_stac.StdlibStacIO` is now installed via `pystac.StacIO.set_default()`, so every write goes through `json.dumps(indent=2)` regardless of what is installed, and `dump_stac_json()` is the single helper for direct writes. The catalog has been normalized to match (32 files). **Keep it that way**: a float rendered two ways is a diff on every item that touches it.

The pin is what guarantees this, not the absence of orjson. `stac-geoparquet` — the only package that pulled orjson in — was dropped because nothing imports it (`rustac` does all the GeoParquet work), but any future dependency could pull it back without warning, and the failure mode is silent.

### Content drift: `file:size` + `file:checksum` (issue #17)

The refresh diff is over tile *sets*, so a tile reprocessed under its existing S3 key — same name, new pixels — produces no add and no remove and is invisible. `USGS_1M_12_x29y395_AZ_AubreyCherry_2020_D20` went from 10012×10012 to 1234×1549 on an off-grid origin and was caught only because its project independently lost 5 tiles.

`file:checksum` holds S3's **CRC64NVME** as a hex multihash: multicodec `crc64-nvme` is `0x0165`, varint `e5 02`, then the 8-byte length — so every checksum is `"e50208"` + 16 hex digits. AWS stores this as object metadata for all 124,407 tiles, so re-checking it costs one HEAD per tile (~1100/s, ~2 min for the catalog), not a re-read of 30.2 TB. Why not the alternatives:

* **`file:size` alone** cannot see a same-size pixel rewrite, and 12.6% of the catalog (15,622 tiles) is stored uncompressed, where the byte count is fixed by `proj:shape` × dtype and *cannot* move when pixels change.
* **ETag** is a hash of part hashes for 65% of these tiles, so a plain server-side re-copy changes it while no pixel moved. CRC64NVME is `FULL_OBJECT`, computed over the whole object independent of part layout.
* **LastModified** is useless here: all 124,407 objects carry a 2026 stamp from a bulk prefix rewrite (issue #6).
* **SHA-256** would be a stronger, more portable digest, but S3 does not hold one — computing it means reading all 30.2 TB, which makes it a write-once field that can never be re-verified on a weekly run. That is the opposite of what the issue needs.

Flow: `refresh` compares each item's recorded values against a live HEAD (`restaged_diff`), and any project with drift joins the rebuild list. `reusable_items()` then declines to reuse exactly the drifted items, so titiler is asked about those tiles and no others. Guardrails mirror the rest of the script — `--max-restaged-tiles` (2000) with `--allow-large-restaging`, and `--skip-checksum-check` to skip the HEAD pass entirely.

`backfill_file_metadata.py` established the baseline without a titiler round trip. It is a **go-forward** baseline, not an audit: an item whose tile was re-staged *before* the backfill got today's size and checksum paired with yesterday's geometry, and will look self-consistent forever.

Note `refresh --full-rebuild` does **not** correct those. It only changes *how* projects the tile-set diff already flagged are rebuilt ([refresh_catalog.py:1217](scripts/refresh_catalog.py#L1217)) — and a project whose tiles were reprocessed under their existing names is never flagged, which is the whole reason this issue exists. Rebuilding an affected project means `create-stac --project <ID> --overwrite --full`; finding which projects those are is what the audit below is for.

### Finding pre-backfill drift: `audit_catalog.py`

`refresh` cannot see that class of drift, by construction. `audit_catalog.py` (`pixi run audit`, and the `Audit Catalog` workflow_dispatch) can: it ignores the recorded metadata and re-derives each sampled item from the tile as it stands today, through `create_stac_item()` — **the same titiler endpoint the builder uses** — then diffs that against what is committed. So the audit exercises the production code path, and its result is directly actionable: a clean run means a rebuild would be a no-op, and a mismatch *is* the change a rebuild would make.

Reading the GeoTIFF header locally would be ~4x faster, but it can only confirm the `proj:*` fields. The WGS84 `geometry`/`bbox` and `proj:geometry` come from titiler reprojecting, and nothing read locally can check them — on drifted tiles those were wrong too. One code path that checks everything beats two that each check part of it.

Exactly these are ignored in the diff, determined empirically from a known-good tile that differs in them and nothing else: `links` (pystac rewrites them), `stac_extensions` (the file extension is added after titiler answers), `file:*` (titiler never sees S3 object metadata — they are checked separately against S3), and `statistics`/`histogram` (titiler's own nondeterminism: the same tile re-read returns `mean -0.43414297933755347` vs `-0.43414293580320523`). Everything else must match within 1 ULP.

A collection with no WESM row is reported, not worked around: since issue #23 those are pruned, so it would mean the catalog and WESM have diverged. Verified: 0 of 934 cataloged collections lack a WESM row.

**Findings and errors are different things and are kept apart.** A finding says the catalog is wrong — rebuilding that item would change it. An error means the tile could not be checked at all, which at high concurrency is nearly always the titiler lambda shedding load. Errored tiles are retried once at 1/8 the concurrency, and what survives that is reported separately: `--fail-on-mismatch` fails on findings, while `--max-error-pct` (default 1%) fails a run that could not reach enough tiles to conclude anything. Reporting a throttled request as drift would be exactly the false alarm this script exists to rule out.

**Concurrency is per tile, not per collection.** Sampling is per collection, but the work is pooled flat across every selected tile: an earlier version pooled over collections and walked items serially inside each one, which left every run bounded by its largest collection (`KS_Statewide_2018_A18` alone is 1691 items — ~46 min on its own, now 42 s at `--threads 32`). Phase 1 resolves collections to tile tasks (one S3 LIST plus a HEAD per *sampled* tile — it used to HEAD every tile in the project regardless of sample size, ~40x more requests than the default run reads); phase 2 is one pool over tiles.

`--part i/N` splits the collections across N runs, greedily bin-packed by tile count so the parts finish together (measured spread at N=12: 1.000x). It is deterministic and order-independent — seeded per collection by name, so a collection samples the same tiles whichever part it lands in — so `--part 3/12` means the same thing locally as in CI. `--merge-reports` combines the parts and **verifies every part reported**: a part whose job died would otherwise shrink the totals silently and the run would look clean because the tiles it would have flagged were never checked.

Measured ~40 tiles/s per runner at `--threads 32`, and it scales across runners (4 concurrent clients aggregated 108.7 tiles/s), so the ceiling is not the runner count but the lambda's account concurrency — past ~1000 concurrent invocations it sheds load, which shows up as unchecked tiles. 12 parts × 32 threads leaves room; that is why `parallel_jobs` defaults to 12 rather than the 60 concurrent runners the plan allows.

Observed on a 10-tile-per-collection sample (9105 tiles): **17 mismatched across 8 collections**, all the same signature — items claiming 10012×10012 against tiles now 10000×10000 or 10001×10000, i.e. USGS re-staged those tiles without the 6 m overlap and snapped them to the grid. Drift is **partial within a project** (`AZ_NavajoCorridor_2020_D20` audits 6/20 bad, `AZ_Safford_QL2_2016` 3/15, `UT_Central_Ql2_TL_2018` 3/13), so a small sample under-detects — use `--project <ID>` to audit a suspect collection exhaustively before rebuilding it.

### Internal consistency: `check_catalog.py` (issue #20)

`refresh` checks the catalog against USGS and `audit` checks it against titiler. Neither asks whether the catalog agrees with *itself*, which is the free half of the problem and the half that catches our bugs. `pixi run check-catalog` reads committed files and does the whole catalog (936 collections / 125,187 items) in ~55 s, which is what makes it usable as a per-PR and pre-release gate rather than an occasional sweep.

Nine checks, selectable with `--check`:

| Check | Question |
| --- | --- |
| `root` | are `catalog.json`'s children exactly the collection directories, and does every child href resolve? |
| `meta` | is the "All Cataloged" extent what those collections imply (overall bbox first, then one per collection in sorted order)? |
| `links` | are a collection's `rel="item"` links exactly the item files beside it, in sorted-by-id order? |
| `items` | id == filename stem == asset href stem, href names the item's *own* collection, bbox == geometry bounds, `proj:shape`×gsd spans `proj:bbox`, `file:*` well-formed |
| `summaries` | collection bbox == union of its items, temporal extent + every item's `start_datetime`/`end_datetime` == the snapshotted `wesm:collect_*`, `proj:code` summary in first-seen order |
| `schema` | one item shape and one collection shape catalog-wide — every key *and its position* |
| `format` | every file byte-identical to `json.dumps(indent=2)` (the orjson pin above) |
| `validate` | do sampled objects pass the published STAC JSON schemas? |
| `parquet` | does `catalog.parquet` say the same thing as the items it was walked from? (needs `--parquet`) |

The `items` check closes the gap the issue names: `catalog_state()` takes item identity from the filename stem, so an item whose href points at another project is invisible to everything else we have.

`schema` compares each item against the *modal* shape rather than a hardcoded one, so it needs no maintenance when the schema legitimately moves — but it fires the moment part of the catalog moves and the rest does not, which is what produces a mixed GeoParquet column schema.

**Tracked files only, by default**, because the committed tree *is* the published product: a locally built collection `update-root-catalog` has not linked yet is work in progress, not a broken catalog. `--untracked` walks the filesystem instead and is what CI passes after `refresh` has mutated the tree but before anything is committed — there `git ls-files` would still list the pruned items and miss the new ones. Both modes skip a directory with no `collection.json`, the same rule `refresh_catalog.collection_dirs()` follows.

**`validate` is the only check that reaches the network**, and only for the JSON schemas themselves. pystac bundles the 13 core v1.1.0 schemas, so what is actually fetched is the three extension schemas (file, projection, raster) — once, then cached for the run. That is why the warm-up probes an *extension* schema and not `item.json`: probing a bundled schema succeeds offline, and every item would then fail its extension fetch one at a time. Offline the check is skipped with one note and the run continues, because an unreachable schema says nothing about the catalog (verified by simulating a dead network: one note, exit 0, every other check still runs). Two things make it sampled rather than exhaustive: it costs ~11 ms per item (23 min for the catalog, against 17 s for everything else), and `schema` already proves there is exactly one item shape catalog-wide, so the marginal item adds little. It validates every collection, the root catalog, the meta collection, and `--validate-sample` items per collection (default 1, seeded per collection so a finding reproduces).

Collections are validated **as committed** — no exemptions. That is only true since the `wesm:*` values were wrapped in one-element lists (see step 3 above); before that every collection failed on its first `wesm:` field, and an earlier revision of this checker stripped the block before validating. The invariant is now checked directly too: a `wesm:*` summary that is not a one-element list is a `summaries` finding naming `pixi run wrap-wesm-summaries`, which says what to do about it instead of surfacing as a schema error 30 lines deep. Current state: 936/936 collections, the root catalog, the meta collection and every sampled item pass.

**`parquet` compares the published artifact to the repo.** `catalog.parquet` is gitignored and rebuilt in CI, so nothing here had ever verified it says what the items say — `release.yml` asserted the row count and stopped, which cannot see a row whose href or footprint drifted. The check compares row count and id sets both ways, then per row: `collection`, `assets.elevation.href` (the GTI `LocationField`), `file:size`, `file:checksum`, `proj:code`, `proj:shape`, the three datetimes, `bbox`, and the geometry's coordinates decoded from WKB. 15 s for 125,187 rows.

Coordinates there are compared at 1 ULP, and they need to be: **rustac's JSON float parse and Python's disagree in the last bit on ~47% of coordinates** (an item reading `-119.49674045532203` lands in the parquet as `-119.49674045532204`). Everything else is compared exactly. Verified: a 1-ULP shift is not reported, a 1e-6 one is.

**Findings fail, notes do not.** A finding is a disagreement between two things we wrote and exits 1. A note is a documented, harmless divergence: today the 11 collections carrying WESM `lpc`/`sourcedem`/`metadata` links from an older builder. The 9 items with no `raster:bands` (titiler cannot histogram a tile whose valid pixels span one float32 ULP — `docs/upstream-titiler-hist-issue.md`) are listed by id in `KNOWN_NO_RASTER_BANDS`, not tolerated by count, so a *tenth* is a finding and one that regains bands is reported as a stale entry to delete.

Current state: **0 findings, 11 notes**, including the parquet check against a freshly built `catalog.parquet`. Verified against 46 injected fault classes (wrong id, href pointing at another project, deleted/extra item file, unsorted links, drifted collection bbox, stale meta extent, stray property key, reordered asset keys, orjson float repr, a dropped/duplicated/edited parquet row, a collection missing its license, a wesm summary unwrapped back to a scalar, …) — each one is caught by the check that owns it.

### Automation

* `.github/workflows/update-catalog.yml` — weekly (Mon 06:00 UTC) + `workflow_dispatch` (`dry_run`, `allow_large_removals`). Runs `pixi run refresh` and opens a PR; no PR when nothing changed.
* `.github/workflows/lint.yml` — `pixi run lint` on every PR and push to `main`; fails on a ruff lint error or an unformatted file under `scripts/`.
* `.github/workflows/audit-catalog.yml` — `workflow_dispatch` only, `permissions: contents: read` (no commits, no PR, no release). Inputs: `projects` (space/comma separated; blank sweeps everything), `sample`, `all_tiles`, `parallel_jobs`, `seed`. Three jobs: `prepare` emits the matrix (`parallel_jobs` capped at the number of collections selected, so naming one collection runs exactly one job), `audit` runs the parts with `fail-fast: false`, and `summarize` merges them and owns the exit code — the parts deliberately do *not* pass `--fail-on-mismatch`, so drift in one part cannot be mistaken for a broken part. `prepare`/`summarize` sparse-checkout `scripts` only; they never need the 1.1 GB of item JSON.
* `.github/workflows/check-catalog.yml` — `pixi run check-catalog` on every PR and push to `main` that touches `catalog/` or `scripts/`, plus dispatch; `permissions: contents: read`. The weekly refresh PR is opened with `GITHUB_TOKEN` and PRs opened that way **do not trigger workflows**, so `update-catalog.yml` runs the check itself (with `--untracked --exit-zero`) rather than relying on this one: findings are appended to the PR body and a final step fails the run *after* the PR exists, so a broken refresh is both visible and reviewable. `release.yml` runs it as a gate before the parquet build — ~20 s against ~15 min, and the parquet inherits whatever inconsistency the catalog has.
* `.github/workflows/release.yml` — monthly (1st, 06:17 UTC) + dispatch. Skips if `catalog/` is unchanged since the latest release *tag* (resolved via the tag, not `targetCommitish`). Otherwise checks consistency, rebuilds the parquet, checks the parquet against the items row for row (this replaced the row-count assert), writes `catalog.gti`, and publishes a CalVer `vYYYY.MM.DD` release so `releases/latest/download/catalog.parquet` stays current.

`refresh_catalog.py` is the interesting one — it is destructive by design and its guardrails exist because the USGS bucket has been observed mid-repopulation (issue #6):

* abort if S3 lists < `--min-projects` (900) folders;
* abort if `WESM.csv` has < `--min-wesm-rows` (3000) rows — a truncated table would retire every collection at once;
* abort if the diff would remove > `--max-removed-tiles` (2000) tiles, unless `--allow-large-removals`;
* abort if > `--max-wesm-retired` (5) cataloged collections lost their WESM row, unless `--allow-large-wesm-retirement`;
* a cataloged project whose `TIFF/` went empty is pruned **only** if the folder prefix is gone, or every probed item URL returns 404 — a 200, a probe error, or nothing to probe means carry unchanged and re-decide next run. Never make pruning fire on network errors.
* new/changed item URLs must all HEAD 200; a random sample of *untouched* carried-over items must 404 below `--max-404-pct`.
* abort if more than `--max-metadata-updates` (300) collections drift in WESM, unless `--allow-large-metadata-updates` — a WESM schema change (column added/renamed) drifts every collection at once.
* abort if more than `--max-restaged-tiles` (2000) tiles changed content under an existing key, unless `--allow-large-restaging` — a genuine bulk reprocessing upstream would otherwise queue one titiler request per tile across the whole catalog.

A tile-set diff cannot see a WESM revision that adds or removes no tiles (issue #18), so `refresh` also compares each `collection.json`'s snapshotted `wesm:*` summaries against live `WESM.csv` and repairs the drifted ones **in place** — summaries, collection temporal extent, and (only when `collect_start`/`collect_end` moved) every item's `start_datetime`/`end_datetime`. No titiler round trip; the tiles are untouched. Both sides of the comparison go through `create_static_stac.wesm_summary_fields()` so a serialization difference cannot masquerade as drift, and the rewrite is byte-identical to a full rebuild apart from those fields. Disable with `--skip-wesm-check`.

### WESM is the source of truth for membership

A cataloged collection whose folder name no longer resolves to a WESM workunit *or* project has been retired upstream, and is pruned even while tifs linger in the bucket (issue #23) — `UT_StrawberryRiver_2019` was the first case. Symmetrically, an S3 folder with no WESM row is never built: it would fail its WESM lookup on every run forever. Nothing is lost either way, because both directions are re-derived from live data each run — if USGS re-lists the workunit, the folder is rebuilt from S3 automatically. `--skip-wesm-check` disables both and falls back to the S3-only diff.

### Attributing removals (issue #19)

The tile diff says *what* vanished, never *why*, so `classify_removal()` labels every prune candidate with an evidence class before anything is deleted:

| Label | Signal | Auto-prunes? |
| --- | --- | --- |
| `retired-from-wesm` | no workunit/project row in `WESM.csv` | yes |
| `withdrawn-for-cause` | live `onemeter_category` is `Does not meet` | yes |
| `superseded-by:<project>` | ≥ `SUPERSEDE_MIN_FRAC` (0.9) of the footprint's grid cells now staged under one other project | yes |
| `tiffs-missing-from-intact-folder` | `browse/` + `metadata/` companions survive for every cataloged tile — only the tifs are gone | **no** |
| `unexplained` | nothing above matched | **no** |

The last two are *held*: reported in the PR body, left on disk, re-decided next run. They are the cases worth raising with USGS rather than silently pruning, and `--allow-unexplained-prunes` is the deliberate override. These are evidence classes, not statements of USGS intent — WESM has no attribute covering 1 m staging at all, so `*_reason` fields describe spec compliance and cannot explain a removal. Grid cells come from the tile id (`USGS_1M_<zone>_x<X>y<Y>_…`); the UTM zone is part of the key because x/y repeat in every zone. `--explain` extends the same attribution to *partial* tile loss inside surviving projects, at the cost of two extra S3 listings per project.

Tiles removed in place also need their item JSONs unlinked explicitly after a rebuild, otherwise the same projects are flagged changed every run and the parquet row-count gate diverges.

## Consumers

The published `catalog.parquet` feeds GDAL's [GTI driver](https://gdal.org/en/stable/drivers/raster/gti.html) and SlideRule Earth raster sampling, and is rendered by stac-map from `raw.githubusercontent`. Release assets redirect, so stac-map cannot read them — large parquet files for browsing live in the separate `scottyhq/files` repo. Keep this in mind before changing asset names, the `assets.elevation.href` field (the GTI `LocationField`), or the release asset filenames.
