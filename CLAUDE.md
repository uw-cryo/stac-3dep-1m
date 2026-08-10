# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Which files to consider

Only consider tracked files in the repository. Do not consider untracked files, even if they are in the same directory as tracked files.

## Python Environment

Always use `pixi run python ...` when running Python commands. Do NOT create virtual environments (no `python -m venv`, `conda create`, `uv venv`, etc.). All Python execution should go through pixi.

## Commands

Defined as pixi tasks in `pixi.toml` (`pixi task list` shows descriptions):

```bash
pixi run create-stac --project WA_KingCounty_2021_B21 --overwrite  # one project (or --workunit)
pixi run create-all                # every S3 project folder not already in catalog/
pixi run refresh --dry-run         # diff catalog/ vs S3, report only
pixi run refresh                   # diff + rebuild/prune + validate (mutates catalog/)
ulimit -n 500000 && pixi run catalog2geoparquet   # full catalog -> catalog.parquet
pixi run collection2geoparquet catalog/<ID>/collection.json out.parquet
pixi run update-root-catalog       # link new children into catalog/catalog.json
pixi run list-collections          # regenerate collections.txt
pixi run ruff                      # ruff check --fix + ruff format
```

There is no test suite. `catalog2geoparquet` opens one file handle per item (~124k), hence the `ulimit -n` bump.

Local `refresh` runs abort *after* modifying the working tree if validation fails (CI gates the commit separately, so nothing is published there). Recover with:
`git restore catalog collections.txt && git clean -fd catalog`

## Architecture

The repository *is* the product: `catalog/` holds ~124k committed STAC Item JSONs (one per USGS 3DEP 1m GeoTIFF) plus a `collection.json` per project. `catalog.parquet` / `catalog.gti` are never committed (gitignored) — they are built in CI and published as release assets.

Data flow:

1. **S3 listing** — `s3://prd-tnm/StagedProducts/Elevation/1m/Projects/<project>/TIFF/*.tif`, unsigned boto3, **always paginated** (a bare `list_objects_v2` truncates at 1000 keys and has silently dropped tiles from large projects before). Tile lists come from listing `TIFF/` directly, *not* from `0_file_download_links.txt` — that file is missing for some projects and contains duplicates/wrong case in others.
2. **STAC Item generation** — `scripts/create_static_stac.py` POSTs each TIFF URL to a TiTiler `/cog/stac` endpoint (currently a private Lambda URL constant in the script; `titiler.xyz` is rate limited). Requests are batched async (100 at a time) with bounded retries. Two known server responses are handled specially: "Too many bins for data range" → retry with `with_raster=false`; 5xx → short sleep and retry.
3. **Collection metadata** — joined from USGS `WESM.csv` (`s3://prd-tnm/StagedProducts/Elevation/metadata/WESM.csv`) and attached to `collection.summaries` with a `wesm:` prefix (technically invalid STAC, but STAC Browser renders it). Layout is flat/self-contained (`TemplateLayoutStrategy(item_template="")`, `CatalogType.SELF_CONTAINED`) so `catalog/` is relocatable.
4. **Aggregation** — `catalog2geoparquet.py` walks the root catalog with `rustac` and writes zstd STAC-GeoParquet; `collection2geoparquet.py` embeds collection metadata for a single collection. `update_root_catalog.py` adds child links and regenerates the `catalog/collection.json` "All Cataloged" meta collection (union bbox + temporal extent across all collections).

### Project vs. workunit naming

The S3 folder name is sometimes the WESM *project*, sometimes the *workunit*, and sometimes neither. Callers try `--workunit` first, then fall back to `--project` (see `create_all.py` and `refresh_catalog.py:build_project`). Irreconcilable names live in the hand-curated `onemeter_folder_to_wesm` dict in `create_static_stac.py` — a build failure in CI usually means a new folder needs an entry there. When a project spans multiple workunits (~214 of 909 collections), `collapse_workunit_rows` takes min(collect_start)/max(collect_end) rather than an order-dependent `.iloc[0]`.

Some folders have no TIFFs at all; those are listed in `NO_TIFFS` in `create_all.py`. `NOTES.md` records the upstream quirks behind these workarounds — read it before "fixing" something that looks anomalous.

### Schema uniformity

`normalize_projection()` rewrites projection extension v1.x (`proj:epsg`) to v2.0.0 (`proj:code`) so the whole catalog — and therefore the GeoParquet column schema — stays uniform regardless of which TiTiler version answered. Anything that changes Item properties has to keep all ~124k items consistent or the parquet build produces a mixed schema.

### Automation

* `.github/workflows/update-catalog.yml` — weekly (Mon 06:00 UTC) + `workflow_dispatch` (`dry_run`, `allow_large_removals`). Runs `pixi run refresh` and opens a PR; no PR when nothing changed.
* `.github/workflows/release.yml` — monthly (1st, 06:17 UTC) + dispatch. Skips if `catalog/` is unchanged since the latest release *tag* (resolved via the tag, not `targetCommitish`). Otherwise rebuilds the parquet, asserts parquet row count == item-file count, writes `catalog.gti`, and publishes a CalVer `vYYYY.MM.DD` release so `releases/latest/download/catalog.parquet` stays current.

`refresh_catalog.py` is the interesting one — it is destructive by design and its guardrails exist because the USGS bucket has been observed mid-repopulation (issue #6):

* abort if S3 lists < `--min-projects` (900) folders;
* abort if the diff would remove > `--max-removed-tiles` (2000) tiles, unless `--allow-large-removals`;
* a cataloged project whose `TIFF/` went empty is pruned **only** if the folder prefix is gone, or every probed item URL returns 404 — a 200, a probe error, or nothing to probe means carry unchanged and re-decide next run. Never make pruning fire on network errors.
* new/changed item URLs must all HEAD 200; a random sample of *untouched* carried-over items must 404 below `--max-404-pct`.

Tiles removed in place also need their item JSONs unlinked explicitly after a rebuild, otherwise the same projects are flagged changed every run and the parquet row-count gate diverges.

## Consumers

The published `catalog.parquet` feeds GDAL's [GTI driver](https://gdal.org/en/stable/drivers/raster/gti.html) and SlideRule Earth raster sampling, and is rendered by stac-map from `raw.githubusercontent`. Release assets redirect, so stac-map cannot read them — large parquet files for browsing live in the separate `scottyhq/files` repo. Keep this in mind before changing asset names, the `assets.elevation.href` field (the GTI `LocationField`), or the release asset filenames.
