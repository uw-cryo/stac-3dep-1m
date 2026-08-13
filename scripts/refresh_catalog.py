#!/usr/bin/env python
"""Incremental refresh of the static STAC catalog against current USGS holdings.

Designed to run unattended (scheduled CI) but equally usable locally:

1. Discover the current 1m DEM tile set with a *paginated* S3 listing of
   s3://prd-tnm/StagedProducts/Elevation/1m/Projects/ (the same source of
   truth create_all.py uses, without the 1000-key truncation).
2. Diff against the local ./catalog item set:
     - new projects        -> build with create_static_stac.py
     - changed projects    -> rebuild with create_static_stac.py --overwrite
       (tile added/removed within an existing project, e.g. restaged data)
     - removed projects    -> prune catalog/<project> and root catalog link
     - WESM metadata drift -> metadata-only refresh of catalog/<project>
       (collection summaries + temporal extent, item datetimes; no titiler)
   WESM is the source of truth for membership: a cataloged collection whose
   workunit/project row is gone has been retired upstream and is pruned even
   while tifs linger in the bucket, and an S3 folder with no WESM row is not
   built in the first place (issue #23).
3. Attribute every prune to an evidence class before acting on it (issue #19).
   Only `retired-from-wesm`, `withdrawn-for-cause` and `superseded-by:<project>`
   prune automatically; `tiffs-missing-from-intact-folder` and `unexplained`
   are held for human review (override with --allow-unexplained-prunes).
4. Guardrails (issue #6: the bucket has been observed mid-repopulation):
     - abort if S3 lists fewer than --min-projects project folders
     - abort if WESM.csv has fewer than --min-wesm-rows rows
     - abort if the diff would remove more than --max-removed-tiles tiles
       (override with --allow-large-removals after human review)
     - abort if more than --max-wesm-retired collections lost their WESM row
       (override with --allow-large-wesm-retirement)
     - abort if more than --max-metadata-updates collections drifted in WESM
       (override with --allow-large-metadata-updates)
5. Validate with HTTP HEAD spot checks:
     - every new/changed item URL sampled up to --sample-new must return 200
     - a random sample (--sample-existing) of carried-over item URLs must
       404 at a rate below --max-404-pct
6. Write a machine-readable summary (--summary), a markdown PR body with a
   per-collection table (--pr-body), plus GitHub Actions outputs (changed /
   title / subject / changelog / counts) when GITHUB_OUTPUT is set.

Exit codes: 0 = success (changed or no-op), 1 = guardrail/validation failure.

Usage:
  python scripts/refresh_catalog.py --dry-run          # report the diff only
  python scripts/refresh_catalog.py --dry-run --pr-body /tmp/body.md
  python scripts/refresh_catalog.py --summary refresh_summary.json
"""

import argparse
import concurrent.futures
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import boto3
import requests
from botocore import UNSIGNED
from botocore.client import Config

# sibling script (scripts/ is on sys.path when this file is run directly), so the
# refresh and the builder agree on how a folder name maps to a WESM row and how
# that row is serialized into collection summaries
import create_static_stac

BUCKET = "prd-tnm"
PROJECTS_PREFIX = "StagedProducts/Elevation/1m/Projects/"
CATALOG_DIR = Path("catalog")
ROOT_CATALOG = CATALOG_DIR / "catalog.json"
COLLECTIONS_TXT = Path("collections.txt")

s3 = boto3.client("s3", config=Config(signature_version=UNSIGNED))


# ---------------------------------------------------------------- discovery
def list_project_folders():
    """All project folder names under the 1m Projects/ prefix (paginated)."""
    paginator = s3.get_paginator("list_objects_v2")
    folders = []
    for page in paginator.paginate(
        Bucket=BUCKET, Prefix=PROJECTS_PREFIX, Delimiter="/"
    ):
        folders += [c["Prefix"].split("/")[-2] for c in page.get("CommonPrefixes", [])]
    return folders


def list_project_tifs(project):
    """Map item id -> https URL for every .tif in a project (paginated)."""
    paginator = s3.get_paginator("list_objects_v2")
    prefix = f"{PROJECTS_PREFIX}{project}/TIFF"
    tifs = {}
    for page in paginator.paginate(Bucket=BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith(".tif"):
                item_id = key.split("/")[-1][:-4]
                tifs[item_id] = f"https://{BUCKET}.s3.amazonaws.com/{key}"
    return tifs


def discover_s3(threads=8):
    """(holdings, folders): {project: {item_id: url}} for tif-bearing projects,
    plus the set of *all* project folder names (some folders hold only
    browse/metadata remnants, or are mid-restage with tifs temporarily gone --
    the distinction matters for prune decisions)."""
    folders = list_project_folders()
    print(f"S3 lists {len(folders)} project folders", flush=True)
    holdings = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as ex:
        for project, tifs in zip(folders, ex.map(list_project_tifs, folders)):
            if tifs:
                holdings[project] = tifs
    n_tiles = sum(len(v) for v in holdings.values())
    print(f"{len(holdings)} projects contain {n_tiles} tifs", flush=True)
    return holdings, set(folders)


# ------------------------------------------------------------ local catalog
def collection_dirs():
    """Child collection directories: those that actually hold a collection.json.

    A directory without one is not a collection. Locally it is usually output
    from another script -- create_collection_parquets.py writes <ID>.parquet
    into each collection folder, and its runs left 11 such directories here.
    Counting them as collections put stray names in collections.txt and, worse,
    made them look like cataloged projects with zero items on S3, i.e. prune
    candidates -- and prune_project() is an rmtree.
    """
    return sorted(
        p
        for p in CATALOG_DIR.iterdir()
        if p.is_dir() and (p / "collection.json").is_file()
    )


def catalog_state(only=None):
    """{project: {item_id}} for the local static catalog (item JSON files).

    `only` restricts it to the named projects, so the before/after item counts
    stay on the same footing when --only narrows the run.
    """
    state = {}
    for coll in collection_dirs():
        if only is not None and coll.name not in only:
            continue
        ids = {p.stem for p in coll.glob("*.json") if p.name != "collection.json"}
        state[coll.name] = ids
    return state


def item_asset_url(project, item_id):
    """Asset href recorded in an existing item JSON (None if unreadable)."""
    path = CATALOG_DIR / project / f"{item_id}.json"
    try:
        item = json.loads(path.read_text())
        return item["assets"]["elevation"]["href"]
    except Exception:
        return None


# ------------------------------------------------- content drift (issue #17)
# The diff above is over tile *sets*, so a tile reprocessed under its existing
# key -- same name, new pixels -- produces no add and no remove and is invisible.
# Items carry file:size + file:checksum (S3's FULL_OBJECT CRC64NVME) precisely so
# that case has a baseline to fail against; re-checking it costs one HEAD per
# tile, not a re-read of the tile. A project with any drifted tile joins the
# rebuild list, and create_static_stac.reusable_items() then re-derives exactly
# the drifted tiles from titiler and reuses the rest.
def catalog_file_metadata(project):
    """{item_id: recorded file metadata} for one collection's items.

    Items predating the backfill record nothing; they yield {} and so can never
    look drifted.
    """
    meta = {}
    for path in sorted((CATALOG_DIR / project).glob("*.json")):
        if path.name == "collection.json":
            continue
        try:
            item = json.loads(path.read_text())
        except Exception:
            continue  # unreadable -> the rebuild path deals with it
        meta[path.stem] = create_static_stac.item_file_metadata(item)
    return meta


def restaged_diff(projects, holdings, threads=8):
    """{project: [item_id, ...]} for tiles whose bytes changed under the same key.

    Only fields the item actually carries are compared, so a not-yet-backfilled
    item is never mistaken for a drifted one.
    """
    drift = {}

    def check(project):
        live = create_static_stac.file_metadata(holdings[project].values())
        recorded = catalog_file_metadata(project)
        return sorted(
            item_id
            for item_id, have in recorded.items()
            if have
            and item_id in live
            and any(have[k] != live[item_id].get(k) for k in have)
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as ex:
        for project, ids in zip(projects, ex.map(check, projects)):
            if ids:
                drift[project] = ids
    return drift


# ------------------------------------------------------------ WESM metadata
# A rebuild triggers on tile-set change, so a WESM revision that adds or removes
# no tiles would otherwise never reach the catalog (issue #18): collection wesm:*
# summaries are snapshotted at build time, and every item's start_datetime /
# end_datetime derives from WESM collect_start / collect_end -- the field
# SlideRule's AMS 3DEP1M table keys on (issue #11), where a stale value is a
# wrong answer rather than a missing one.
def wesm_row(project, df):
    """Live WESM row for a cataloged folder name, or None if WESM has none.

    Mirrors build_project: try the folder as a workunit first, then as a project
    (the S3 folder name is sometimes one, sometimes the other, and sometimes
    neither -- see onemeter_folder_to_wesm in create_static_stac.py).
    """
    for is_workunit in (True, False):
        try:
            return create_static_stac.get_wesm_series(
                project, is_workunit=is_workunit, df=df, warn=False
            )
        except (ValueError, KeyError, IndexError):
            continue
    return None


def collection_wesm_summaries(project):
    """wesm:* summaries recorded in a collection.json (None if unreadable)."""
    path = CATALOG_DIR / project / "collection.json"
    try:
        summaries = json.loads(path.read_text()).get("summaries", {})
    except Exception:
        return None
    return {k: v for k, v in summaries.items() if k.startswith("wesm:")}


def wesm_diff(projects, df):
    """(drift, rows) for the given cataloged projects.

    drift  {project: [drifted summary field, ...]}
    rows   {project: live WESM row} for the drifted projects

    Both sides of the comparison are produced by wesm_summary_fields(), so a
    serialization difference cannot masquerade as drift. Projects with no live
    WESM row never reach here: they are retired and pruned before the drift
    check runs (see classify_removal / RETIRED_FROM_WESM).
    """
    drift, rows = {}, {}
    for project in projects:
        have = collection_wesm_summaries(project)
        if have is None:
            continue
        series = wesm_row(project, df)
        if series is None:
            continue
        want = create_static_stac.wesm_summary_fields(series)
        fields = sorted(k for k in set(have) | set(want) if have.get(k) != want.get(k))
        if fields:
            drift[project] = fields
            rows[project] = series
    return drift, rows


def refresh_collection_metadata(project, series):
    """Rewrite the WESM-derived fields of one collection in place.

    Orders of magnitude cheaper than a rebuild: no titiler round trip, and the
    tile set is untouched. Collection summaries and temporal extent are always
    rewritten; item start_datetime/end_datetime only when the collect range
    actually moved (rare, and the expensive case -- every item file in the
    collection). Returns the number of item files rewritten.
    """
    # the same string the builder hands titiler, so a metadata refresh and a full
    # rebuild land on identical datetimes
    start, end = create_static_stac.get_titiler_datetime(series).split("/")
    if "NaT" in start or "NaT" in end:
        raise ValueError(f"unusable WESM collect range ({start}/{end})")

    path = CATALOG_DIR / project / "collection.json"
    collection = json.loads(path.read_text())
    summaries = collection.get("summaries", {})
    for key in [k for k in summaries if k.startswith("wesm:")]:
        del summaries[key]
    summaries.update(create_static_stac.wesm_summary_fields(series))
    collection["summaries"] = summaries
    collection["extent"]["temporal"]["interval"] = [[start, end]]
    path.write_text(create_static_stac.dump_stac_json(collection))

    n_items = 0
    for item_path in sorted((CATALOG_DIR / project).glob("*.json")):
        if item_path.name == "collection.json":
            continue
        item = json.loads(item_path.read_text())
        props = item.get("properties", {})
        if props.get("start_datetime") == start and props.get("end_datetime") == end:
            continue
        props["start_datetime"] = start
        props["end_datetime"] = end
        item_path.write_text(create_static_stac.dump_stac_json(item))
        n_items += 1
    return n_items


# ------------------------------------------------------- removal attribution
# The tile diff reports *what* vanished but never *why*, which leaves a reviewer
# no basis to judge a prune (issue #19). Attribute each removal to an evidence
# class first, and let only the classes that rest on an affirmative USGS
# assertion -- or on the data demonstrably living somewhere else -- prune
# automatically.
#
# These are evidence classes, not statements of USGS intent. WESM has no
# attribute covering 1m staging at all: every vanished UT_FemaHQ_* workunit
# still reads onemeter_reason "Meets 3DEP 1-m DEM requirements" while its tifs
# are gone, so the *_reason fields describe spec compliance, not staging.
RETIRED_FROM_WESM = "retired-from-wesm"
WITHDRAWN_FOR_CAUSE = "withdrawn-for-cause"
SUPERSEDED_BY = "superseded-by"
TIFFS_MISSING = "tiffs-missing-from-intact-folder"
UNEXPLAINED = "unexplained"
UNATTRIBUTED = "unattributed"

# A replacement project has to cover essentially the whole footprint to explain
# a removal; partial overlap is normal between neighbouring projects.
SUPERSEDE_MIN_FRAC = 0.9

# Reasons strong enough to destroy a collection without a human looking first.
AUTO_PRUNABLE = (RETIRED_FROM_WESM, WITHDRAWN_FOR_CAUSE, SUPERSEDED_BY, UNATTRIBUTED)

CELL_RE = re.compile(r"x(\d+)y(\d+)")


def auto_prunable(label):
    """Whether a removal reason justifies pruning without human review."""
    return label.startswith(AUTO_PRUNABLE)


def parse_cell(item_id):
    """(utm_zone, x, y) grid cell of a USGS 1m tile id, or None.

    Ids look like USGS_1M_12_x49y444_UT_StrawberryRiver_2019. The cell is only
    comparable across projects because the UTM zone is part of the key -- x/y
    alone repeat in every zone.
    """
    parts = item_id.split("_")
    if len(parts) < 4 or parts[0] != "USGS" or parts[1] != "1M":
        return None
    m = CELL_RE.fullmatch(parts[3])
    return (parts[2], m.group(1), m.group(2)) if m else None


def build_cell_index(holdings):
    """{grid cell: {project currently holding it}} over all S3 holdings."""
    index = {}
    for project, tifs in holdings.items():
        for item_id in tifs:
            cell = parse_cell(item_id)
            if cell:
                index.setdefault(cell, set()).add(project)
    return index


def companion_counts(project):
    """{'browse': (n, cells), 'metadata': (n, cells)} still staged for a project.

    USGS stages one .jpg and one .xml per tile alongside the .tif. A folder
    whose tifs vanished while every companion survives looks like an incomplete
    re-stage rather than a withdrawal, so it is the one signal that argues
    *against* pruning.
    """
    out = {}
    paginator = s3.get_paginator("list_objects_v2")
    for sub, ext in (("browse", ".jpg"), ("metadata", ".xml")):
        cells, n = set(), 0
        for page in paginator.paginate(
            Bucket=BUCKET, Prefix=f"{PROJECTS_PREFIX}{project}/{sub}/"
        ):
            for obj in page.get("Contents", []):
                name = obj["Key"].split("/")[-1]
                if not name.endswith(ext):
                    continue
                n += 1
                cell = parse_cell(name[: -len(ext)])
                if cell:
                    cells.add(cell)
        out[sub] = (n, cells)
    return out


def classify_removal(project, item_ids, df, cell_index):
    """(label, evidence) attributing why a cataloged project's tiles vanished.

    Signals are checked in order of how much they actually commit to: an
    affirmative WESM assertion first, then the footprint demonstrably held by
    another project, then the folder-shape signal suggesting nothing was
    withdrawn at all. Anything unmatched stays `unexplained` and is never
    auto-pruned -- those are the cases worth raising with USGS.
    """
    series = wesm_row(project, df)
    if series is None:
        return RETIRED_FROM_WESM, "no workunit or project row in WESM.csv"

    category = series.onemeter_category
    if category == "Does not meet":
        was = (collection_wesm_summaries(project) or {}).get("wesm:onemeter_category")
        moved = f", was {was!r} at build time" if was and was != category else ""
        return (
            WITHDRAWN_FOR_CAUSE,
            f"WESM onemeter_category is 'Does not meet'{moved} "
            f"({series.onemeter_reason})",
        )

    cells = {c for c in map(parse_cell, item_ids) if c}
    if cells:
        covering = {}
        for cell in cells:
            for other in cell_index.get(cell, ()):
                if other != project:
                    covering[other] = covering.get(other, 0) + 1
        if covering:
            # ties broken by name so the label is reproducible run to run
            best, n = max(covering.items(), key=lambda kv: (kv[1], kv[0]))
            if n >= SUPERSEDE_MIN_FRAC * len(cells):
                return (
                    f"{SUPERSEDED_BY}:{best}",
                    f"{n}/{len(cells)} grid cells are now staged under {best}",
                )

    comp = companion_counts(project)
    n_browse, browse_cells = comp["browse"]
    n_meta, meta_cells = comp["metadata"]
    if cells and browse_cells and meta_cells:
        intact = browse_cells >= cells and meta_cells >= cells
    else:
        intact = n_browse >= len(item_ids) and n_meta >= len(item_ids)
    if intact and (n_browse or n_meta):
        return (
            TIFFS_MISSING,
            f"browse/ ({n_browse}) and metadata/ ({n_meta}) companions survive for "
            f"all {len(item_ids)} cataloged tiles -- only the tifs are gone",
        )

    return (
        UNEXPLAINED,
        f"WESM still reads onemeter_category {category!r}, no project covers the "
        f"footprint, and {n_browse} browse / {n_meta} metadata companions remain",
    )


# ------------------------------------------------------------------ rebuild
def build_project(project, overwrite=False, full=False):
    """Run create_static_stac.py for one project; True on success.

    Mirrors create_all.py: try as WESM workunit first, then as project.
    A rebuild reuses the items already on disk and asks titiler only about new
    tiles; full=True re-derives every item instead (see --full-rebuild).
    """
    script = Path(__file__).parent / "create_static_stac.py"
    for flag in ("--workunit", "--project"):
        cmd = [sys.executable, str(script), flag, project]
        if overwrite:
            cmd.append("--overwrite")
        if full:
            cmd.append("--full")
        try:
            subprocess.run(cmd, check=True)
            return True
        except subprocess.CalledProcessError:
            print(f"  {project}: failed as {flag.lstrip('-')}", flush=True)
    return False


def prune_project(project):
    shutil.rmtree(CATALOG_DIR / project)


def sync_root_catalog(removed):
    """Remove pruned child links from catalog/catalog.json (direct JSON edit).

    Additions and the meta collection are handled by update_root_catalog.py,
    which only knows how to add children -- removal happens here.
    """
    cat = json.loads(ROOT_CATALOG.read_text())
    removed_hrefs = {f"./{p}/collection.json" for p in removed}
    cat["links"] = [ln for ln in cat["links"] if ln.get("href") not in removed_hrefs]
    ROOT_CATALOG.write_text(json.dumps(cat, indent=2))


def write_collections_txt():
    names = [p.name for p in collection_dirs()]
    COLLECTIONS_TXT.write_text("\n".join(names) + "\n")


# --------------------------------------------------------------- validation
def head_status(url, retries=2):
    for attempt in range(retries + 1):
        try:
            r = requests.head(url, timeout=60)
            if r.status_code in (429, 500, 502, 503, 504):
                raise requests.HTTPError(f"HTTP {r.status_code}")
            return r.status_code
        except Exception:
            time.sleep(2**attempt)
    return -1


def head_check(urls, label, threads=8):
    """HEAD every url; return (n_checked, n_404, n_error)."""
    if not urls:
        return 0, 0, 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as ex:
        codes = list(ex.map(head_status, urls))
    n404 = sum(1 for c in codes if c == 404)
    nerr = sum(1 for c in codes if c not in (200, 404))
    print(
        f"HEAD {label}: {len(codes)} checked, {n404} x 404, {nerr} errors", flush=True
    )
    return len(codes), n404, nerr


# ------------------------------------------------------------------ summary
# GitHub rejects a pull request body over 65536 characters, so long tables are
# capped well short of that and the remainder left to the run summary.
PR_BODY_MAX_ROWS = 150


def _table(rows, headers, align=None):
    """Markdown table. `align` is one 'l'/'r' per column; counts default to 'r',
    and anything else (a reason label, a field list) reads better left."""
    align = align or "l" + "r" * (len(headers) - 1)
    out = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---:" if a == "r" else "---" for a in align) + " |",
    ]
    for r in rows[:PR_BODY_MAX_ROWS]:
        out.append("| " + " | ".join(str(c) for c in r) + " |")
    if len(rows) > PR_BODY_MAX_ROWS:
        out.append(f"| _… and {len(rows) - PR_BODY_MAX_ROWS} more_ | | |")
    return "\n".join(out)


def _section(title, rows, headers, collapse_over=25, align=None):
    """A heading plus table, collapsed behind <details> when it gets long."""
    if not rows:
        return ""
    body = _table(rows, headers, align)
    if len(rows) > collapse_over:
        return (
            f"<details>\n<summary><b>{title}</b> (click to expand)</summary>\n\n"
            f"{body}\n\n</details>\n"
        )
    return f"#### {title}\n\n{body}\n"


def render_pr_body(summary, run_url=None):
    """Markdown PR body: headline counts plus a per-collection table."""
    details = summary["details"]
    failures = set(summary.get("build_failures") or [])

    def rows(action, cols):
        return [cols(p, d) for p, d in sorted(details.items()) if d["action"] == action]

    new = rows("new", lambda p, d: (f"`{p}`", f"+{d['added']}"))
    rebuilt = rows(
        "rebuilt",
        lambda p, d: (
            f"`{p}`",
            f"+{d['added']}" if d["added"] else "—",
            f"−{d['removed']}" if d["removed"] else "—",
            f"~{d['restaged']}" if d.get("restaged") else "—",
        ),
    )
    # tiles reprocessed under an existing key: no add, no remove, invisible to a
    # tile-set diff, and caught only by the recorded checksum (issue #17)
    restaged = [
        (f"`{p}`", f"~{d['restaged']}")
        for p, d in sorted(details.items())
        if d.get("restaged")
    ]
    # the reason is the whole point of the prune table: a bare tile count gives
    # a reviewer nothing to judge the removal by (issue #19)
    pruned = rows(
        "pruned",
        lambda p, d: (
            f"`{p}`",
            f"−{d['removed']}",
            f"`{d.get('reason', UNATTRIBUTED)}`",
        ),
    )
    metadata = rows(
        "metadata",
        lambda p, d: (
            f"`{p}`",
            ", ".join(f"`{f.removeprefix('wesm:')}`" for f in d["fields"]),
            d.get("items_updated") or "—",
        ),
    )

    added, removed = summary["tiles_added"], summary["tiles_removed"]
    before = summary["catalog_items_before"]
    after = summary.get("catalog_items_after")

    counts = [f"{len(new)} new", f"{len(rebuilt)} rebuilt", f"{len(pruned)} pruned"]
    if metadata:
        counts.append(f"{len(metadata)} metadata-only")
    n_restaged = summary.get("tiles_restaged") or 0

    out = [
        "Automated run of `pixi run refresh` — S3 diff, incremental rebuild, "
        "prune, WESM metadata refresh, HEAD validation "
        "(see `scripts/refresh_catalog.py`).",
        "",
        f"**+{added:,} / −{removed:,} tiles**"
        + (f" / **~{n_restaged:,} re-staged**" if n_restaged else "")
        + f" across {len(details):,} collections — {', '.join(counts)}.",
        "",
        f"Catalog items: {before:,}" + (f" → **{after:,}**" if after else ""),
        "",
    ]
    out.append(_section("New collections", new, ["Collection", "Tiles"]))
    out.append(
        _section(
            "Rebuilt collections",
            rebuilt,
            ["Collection", "Added", "Removed", "Re-staged"],
        )
    )
    if restaged:
        out.append(
            _section(
                "Re-staged in place", restaged, ["Collection", "Tiles"], align="lr"
            )
        )
        out.append(
            "> Same S3 key, different bytes — invisible to a tile-set diff and "
            "caught only by the `file:checksum` recorded at build time (issue "
            "#17). These items were re-derived from titiler; every other tile in "
            "the collection was reused unchanged.\n"
        )
    out.append(
        _section(
            "Pruned collections", pruned, ["Collection", "Tiles", "Reason"], align="lrl"
        )
    )
    # evidence is a sentence per collection, too wide for the table above
    evidence = [
        f"- `{p}` — {d['evidence']}"
        for p, d in sorted(details.items())
        if d["action"] == "pruned" and d.get("evidence")
    ]
    if evidence:
        out.append(
            "<details>\n<summary>Prune evidence</summary>\n\n"
            + "\n".join(evidence[:PR_BODY_MAX_ROWS])
            + "\n\n</details>\n"
        )
    # tiles untouched: only WESM-derived collection metadata moved, plus item
    # datetimes in the collections whose collect range itself changed
    out.append(
        _section(
            "WESM metadata updates",
            metadata,
            ["Collection", "Fields", "Items updated"],
            align="llr",
        )
    )

    if summary.get("held_projects"):
        held_reasons = summary.get("held_reasons") or {}
        lines = "\n".join(
            f"> - `{p}` — `{held_reasons.get(p, ['?', ''])[0]}`: "
            f"{held_reasons.get(p, ['', ''])[1]}"
            for p in summary["held_projects"][:10]
        )
        out.append(
            "> **⏸️ Held for review:** these collections lost their tiles on S3 "
            "but the removal could not be attributed to an affirmative signal, "
            "so they were **not** pruned — re-decided next run, or force with "
            f"`--allow-unexplained-prunes`.\n>\n{lines}\n"
        )
    if summary.get("new_unresolved_projects"):
        names = summary["new_unresolved_projects"]
        shown = ", ".join(f"`{p}`" for p in names[:10])
        more = f" … and {len(names) - 10} more" if len(names) > 10 else ""
        out.append(
            f"> **Not built (no WESM row):** {shown}{more} — present on S3 but "
            "absent from `WESM.csv`, which is the source of truth for catalog "
            "membership; built automatically if USGS re-lists them.\n"
        )
    if summary.get("metadata_failures"):
        names = ", ".join(f"`{p}`" for p in summary["metadata_failures"])
        out.append(
            f"> **⚠️ Metadata refresh failed:** {names} — previous values kept, "
            "retried next run.\n"
        )
    if summary.get("carried_empty_projects"):
        names = ", ".join(f"`{p}`" for p in summary["carried_empty_projects"])
        out.append(
            f"> **Carried, not pruned:** {names} — TIFF listing empty but the "
            "removal probe was inconclusive; re-probed next run.\n"
        )
    if failures:
        names = ", ".join(f"`{p}`" for p in sorted(failures))
        out.append(
            f"> **⚠️ Build failures:** {names} — previous version kept. Usually a "
            "WESM name mismatch (see `onemeter_folder_to_wesm` in "
            "`scripts/create_static_stac.py`).\n"
        )

    if run_url:
        out.append(f"Full diff summary in the [workflow run]({run_url}).")
    out.append(
        "After merging, `release.yml` (monthly cron, or manual "
        "workflow_dispatch) rebuilds `catalog.parquet`/`catalog.gti` and "
        "publishes a dated release."
    )
    return "\n".join(out).replace("\n\n\n", "\n\n") + "\n"


def run_url():
    """URL of the running workflow job, or None outside Actions."""
    server = os.environ.get("GITHUB_SERVER_URL")
    repo = os.environ.get("GITHUB_REPOSITORY")
    run_id = os.environ.get("GITHUB_RUN_ID")
    if server and repo and run_id:
        return f"{server}/{repo}/actions/runs/{run_id}"
    return None


def github_output(**kwargs):
    out = os.environ.get("GITHUB_OUTPUT")
    if not out:
        return
    with open(out, "a") as f:
        for k, v in kwargs.items():
            # values may embed S3-derived folder names: collapse whitespace so
            # the k=v single-line format can't be corrupted, and truncate (the
            # full changelog always lands in the summary JSON / job summary)
            v = " ".join(str(v).split())
            if len(v) > 500:
                v = v[:500] + " ..."
            f.write(f"{k}={v}\n")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="report the diff and exit without modifying anything",
    )
    ap.add_argument(
        "--only",
        action="append",
        metavar="PROJECT",
        help="restrict the diff/rebuild to named project(s) (testing)",
    )
    ap.add_argument(
        "--min-projects",
        type=int,
        default=900,
        help="abort if S3 lists fewer project folders than this",
    )
    # NOTE: this cap counts tiles, not projects -- several small projects can
    # be pruned in one night without tripping it (acceptable blast radius given
    # the folder-present/items-reachable carry logic below)
    ap.add_argument(
        "--max-removed-tiles",
        type=int,
        default=2000,
        help="abort if the diff would remove more tiles than this",
    )
    ap.add_argument(
        "--allow-large-removals",
        action="store_true",
        help="override --max-removed-tiles after human review",
    )
    ap.add_argument(
        "--allow-unexplained-prunes",
        action="store_true",
        help="also prune collections whose removal could not be attributed to "
        f"an affirmative signal ({TIFFS_MISSING} / {UNEXPLAINED}); by default "
        "those are held for review and re-decided next run",
    )
    ap.add_argument(
        "--explain",
        action="store_true",
        help="also attribute *partial* tile loss in rebuilt projects, not just "
        "whole-collection prunes (extra S3 listings per project)",
    )
    ap.add_argument(
        "--min-wesm-rows",
        type=int,
        default=3000,
        help="abort if WESM.csv has fewer rows than this (a truncated table "
        "would retire collections wholesale)",
    )
    ap.add_argument(
        "--max-wesm-retired",
        type=int,
        default=5,
        help="abort if more cataloged collections than this lost their WESM row",
    )
    ap.add_argument(
        "--allow-large-wesm-retirement",
        action="store_true",
        help="override --max-wesm-retired after human review",
    )
    ap.add_argument(
        "--full-rebuild",
        action="store_true",
        help="re-derive every item of a rebuilt project from titiler instead of "
        "reusing unchanged ones (slow: one request per tile, and it re-reads "
        "tiles titiler may no longer be able to answer for)",
    )
    ap.add_argument(
        "--skip-wesm-check",
        action="store_true",
        help="skip the WESM summary comparison (tile diff only)",
    )
    ap.add_argument(
        "--skip-checksum-check",
        action="store_true",
        help="skip the file:size/file:checksum comparison that detects tiles "
        "re-staged under an existing key (issue #17); saves one HEAD per tile",
    )
    ap.add_argument(
        "--max-restaged-tiles",
        type=int,
        default=2000,
        help="abort if more tiles than this were re-staged in place (each one "
        "costs a titiler request to re-derive)",
    )
    ap.add_argument(
        "--allow-large-restaging",
        action="store_true",
        help="override --max-restaged-tiles after human review",
    )
    # a WESM schema change (column added or renamed) drifts every collection at
    # once -- a legitimate but very large diff, so make a human confirm it
    ap.add_argument(
        "--max-metadata-updates",
        type=int,
        default=300,
        help="abort if more cataloged collections than this drifted in WESM",
    )
    ap.add_argument(
        "--allow-large-metadata-updates",
        action="store_true",
        help="override --max-metadata-updates after human review",
    )
    ap.add_argument(
        "--sample-new",
        type=int,
        default=200,
        help="max new/changed item URLs to HEAD-check (all must be 200)",
    )
    ap.add_argument(
        "--sample-existing",
        type=int,
        default=300,
        help="random carried-over item URLs to HEAD-check",
    )
    ap.add_argument(
        "--max-404-pct",
        type=float,
        default=1.0,
        help="fail if more than this %% of sampled existing URLs 404",
    )
    ap.add_argument(
        "--threads", type=int, default=8, help="S3 listing / HEAD check concurrency"
    )
    ap.add_argument(
        "--summary",
        type=Path,
        default=None,
        help="write a JSON run summary to this path",
    )
    ap.add_argument(
        "--pr-body",
        type=Path,
        default=None,
        help="write the rendered markdown PR body to this path "
        "(also written on --dry-run, for local preview)",
    )
    args = ap.parse_args()

    if not CATALOG_DIR.is_dir():
        sys.exit("run from the repository root (catalog/ not found)")

    t0 = time.time()
    holdings, folders = discover_s3(threads=args.threads)
    only = set(args.only) if args.only else None
    local = catalog_state(only)

    if only:
        holdings = {p: v for p, v in holdings.items() if p in only and v}
        folders = folders & only
    elif len(holdings) < args.min_projects:
        sys.exit(
            f"GUARDRAIL: only {len(holdings)} projects listed on S3 "
            f"(< {args.min_projects}); bucket may be mid-repopulation (see issue #6)"
        )

    # WESM is the source of truth for catalog membership. A collection whose
    # workunit/project row is gone has been retired upstream, so it is pruned
    # even while tifs linger in the bucket, and an unresolvable S3 folder is not
    # built in the first place -- otherwise it fails its WESM lookup and raises
    # the same warning on every run forever (issue #23). Nothing is lost either
    # way: if USGS re-lists the workunit the folder is rebuilt from S3.
    wesm_df = None if args.skip_wesm_check else create_static_stac.load_wesm()
    wesm_retired, new_unresolved = [], []
    if wesm_df is not None:
        if len(wesm_df) < args.min_wesm_rows:
            sys.exit(
                f"GUARDRAIL: WESM.csv has only {len(wesm_df)} rows "
                f"(< {args.min_wesm_rows}); refusing to retire collections "
                "against a possibly truncated table"
            )
        wesm_retired = sorted(p for p in local if wesm_row(p, wesm_df) is None)
        if (
            len(wesm_retired) > args.max_wesm_retired
            and not args.allow_large_wesm_retirement
            and not args.dry_run
        ):
            sys.exit(
                f"GUARDRAIL: {len(wesm_retired)} cataloged collections have no "
                f"WESM row (> {args.max_wesm_retired}); a revision that drops "
                "many workunits at once needs review -- rerun with "
                "--allow-large-wesm-retirement"
            )

    retired = set(wesm_retired)
    new = sorted(set(holdings) - set(local))
    if wesm_df is not None:
        new_unresolved = sorted(p for p in new if wesm_row(p, wesm_df) is None)
        new = [p for p in new if p not in set(new_unresolved)]
    changed = sorted(
        p
        for p in (set(holdings) & set(local)) - retired
        if set(holdings[p]) != local[p]
    )

    # Tiles re-staged under their existing key are invisible to the set diff
    # above (issue #17): check the recorded file:size/file:checksum against S3.
    # One HEAD per tile, ~2 min for the whole catalog.
    restaged = {}
    if not args.skip_checksum_check:
        restaged = restaged_diff(
            sorted((set(holdings) & set(local)) - retired),
            holdings,
            threads=args.threads,
        )
        # a project that only drifted still needs a rebuild to pick it up
        changed = sorted(set(changed) | set(restaged))

    # Prune classification: a cataloged project with no tifs on S3 is either
    # truly deleted (folder prefix gone) or a folder remnant / mid-restage
    # (folder still present, e.g. only browse/ thumbnails remain, issue #6).
    # For the latter, prune only on affirmative evidence: every probed item
    # URL returns 404. Any 200 -> carry (possible restage in progress); probe
    # errors or nothing to probe -> inconclusive -> carry with a warning and
    # let a later run decide (pruning is destructive, so never prune on
    # network errors or unreadable item JSONs).
    removed, carried_empty, held = [], [], []
    reasons = {}
    cell_index = build_cell_index(holdings)

    # retired upstream: pruned on WESM's say-so, whatever S3 still holds
    for p in wesm_retired:
        reasons[p] = classify_removal(p, local[p], wesm_df, cell_index)
        removed.append(p)
        print(
            f"  RETIRED  {p}: {reasons[p][1]} -- pruning {len(local[p])} tiles",
            flush=True,
        )

    for p in sorted(set(local) - set(holdings) - retired):
        if p in folders:
            urls = [
                u for u in (item_asset_url(p, i) for i in sorted(local[p])[:5]) if u
            ]
            codes = [head_status(u) for u in urls]
            if any(c == 200 for c in codes):
                carried_empty.append(p)
                print(
                    f"  CARRIED  {p}: TIFF/ empty but folder present and items "
                    "still reachable -- possible restage in progress, not pruning",
                    flush=True,
                )
                continue
            if not (codes and all(c == 404 for c in codes)):
                carried_empty.append(p)
                print(
                    f"  CARRIED  {p}: TIFF/ empty but probe inconclusive "
                    f"(HEAD codes {codes}) -- carrying unchanged, will re-probe "
                    "next run",
                    flush=True,
                )
                continue

        # Reachability says the tiles are gone; ask *why* before destroying the
        # collection (issue #19). Without WESM there is nothing to attribute
        # with, so --skip-wesm-check keeps the pre-#19 reachability-only rule.
        if wesm_df is None:
            reasons[p] = (UNATTRIBUTED, "WESM check skipped (--skip-wesm-check)")
        else:
            reasons[p] = classify_removal(p, local[p], wesm_df, cell_index)
        label, evidence = reasons[p]

        if auto_prunable(label) or args.allow_unexplained_prunes:
            removed.append(p)
            print(f"  REMOVED  {p}: {label} -- {evidence}", flush=True)
        else:
            held.append(p)
            print(
                f"  HELD     {p}: {label} -- {evidence}; not pruning without "
                "review (--allow-unexplained-prunes overrides)",
                flush=True,
            )

    # WESM revisions that add or remove no tiles are invisible to the diff above,
    # so compare the snapshotted collection summaries too (issue #18). Projects
    # about to be rebuilt or pruned are skipped: a rebuild re-reads WESM anyway,
    # and a failed rebuild leaves the drift to be caught next run.
    wesm_drift, wesm_rows = {}, {}
    if wesm_df is not None:
        wesm_drift, wesm_rows = wesm_diff(
            sorted(set(local) - set(changed) - set(removed)), wesm_df
        )

    # Per-project tile deltas, computed once and reused by the console log, the
    # summary JSON and the PR body table.
    details = {}
    for p in new:
        details[p] = {"action": "new", "added": len(holdings[p]), "removed": 0}
    for p in changed:
        d = {
            "action": "rebuilt",
            "added": len(set(holdings[p]) - local[p]),
            "removed": len(local[p] - set(holdings[p])),
        }
        # partial tile loss inside a surviving project gets no attribution by
        # default -- the collection is not at stake, and each classification
        # costs two more S3 listings
        if args.explain and d["removed"] and wesm_df is not None:
            lost = local[p] - set(holdings[p])
            d["reason"], d["evidence"] = classify_removal(p, lost, wesm_df, cell_index)
        if p in restaged:
            d["restaged"] = len(restaged[p])
        details[p] = d
    for p in removed:
        label, evidence = reasons.get(p, (UNATTRIBUTED, ""))
        details[p] = {
            "action": "pruned",
            "added": 0,
            "removed": len(local[p]),
            "reason": label,
            "evidence": evidence,
        }
    for p, fields in sorted(wesm_drift.items()):
        details[p] = {
            "action": "metadata",
            "added": 0,
            "removed": 0,
            "fields": fields,
            "items_updated": 0,
        }

    n_tiles_added = sum(d["added"] for d in details.values())
    n_tiles_removed = sum(d["removed"] for d in details.values())
    n_tiles_restaged = sum(len(v) for v in restaged.values())
    n_local = sum(len(v) for v in local.values())
    n_s3 = sum(len(v) for v in holdings.values())

    print(f"\ncatalog items: {n_local}  |  S3 tifs: {n_s3}")
    print(
        f"diff: +{n_tiles_added} tiles / -{n_tiles_removed} tiles / "
        f"~{n_tiles_restaged} re-staged  "
        f"({len(new)} new, {len(changed)} changed, {len(removed)} removed, "
        f"{len(wesm_drift)} metadata-only projects)"
    )
    for p in new:
        print(f"  NEW      {p} ({details[p]['added']} tiles)")
    for p in changed:
        extra = f", {details[p]['restaged']} re-staged" if p in restaged else ""
        print(
            f"  CHANGED  {p} (+{details[p]['added']} / -{details[p]['removed']}{extra})"
        )
    for p in removed:
        print(f"  REMOVED  {p} ({details[p]['removed']} tiles, {details[p]['reason']})")
    for p, fields in sorted(wesm_drift.items()):
        names = ", ".join(f.removeprefix("wesm:") for f in fields)
        print(f"  METADATA {p} ({names})")
    for p in held:
        print(f"  HELD     {p} ({len(local[p])} tiles, {reasons[p][0]})")
    if new_unresolved:
        print(
            f"  {len(new_unresolved)} S3 folders have no WESM row and were not "
            f"built: {', '.join(new_unresolved[:10])}"
            + (" ..." if len(new_unresolved) > 10 else "")
        )

    changed_any = bool(new or changed or removed or wesm_drift or restaged)
    summary = {
        "s3_projects": len(holdings),
        "s3_tiles": n_s3,
        "catalog_projects_before": len(local),
        "catalog_items_before": n_local,
        "new_projects": new,
        "changed_projects": changed,
        "removed_projects": removed,
        "carried_empty_projects": carried_empty,
        "held_projects": held,
        "held_reasons": {p: list(reasons[p]) for p in held},
        "wesm_retired_projects": wesm_retired,
        "new_unresolved_projects": new_unresolved,
        "metadata_projects": sorted(wesm_drift),
        "restaged_projects": sorted(restaged),
        "tiles_added": n_tiles_added,
        "tiles_removed": n_tiles_removed,
        "tiles_restaged": n_tiles_restaged,
        "dry_run": args.dry_run,
        "build_failures": [],
        "metadata_failures": [],
        "details": details,
    }
    # "+1429/-678 tiles" -- short enough for a PR/commit title on its own; a
    # metadata-only run has no tile delta to report, so it names itself
    title = f"+{n_tiles_added}/-{n_tiles_removed} tiles"
    if n_tiles_restaged:
        title += f", {n_tiles_restaged} re-staged"
    if wesm_drift:
        title += f", {len(wesm_drift)} WESM metadata"
    if not (new or changed or removed):
        title = f"WESM metadata for {len(wesm_drift)} collections"

    if not changed_any:
        print("no changes -- catalog is current")
        github_output(changed="false", changelog="no changes")
        if args.summary:
            summary["elapsed_s"] = round(time.time() - t0, 1)
            args.summary.write_text(json.dumps(summary, indent=1))
        return

    if (
        n_tiles_removed > args.max_removed_tiles
        and not args.allow_large_removals
        and not args.dry_run
    ):
        sys.exit(
            f"GUARDRAIL: refusing to remove {n_tiles_removed} tiles "
            f"(> {args.max_removed_tiles}); rerun with --allow-large-removals "
            "after reviewing the diff"
        )

    # A genuine bulk reprocessing at USGS would drift every tile at once, which
    # means a 124k-request titiler pass and a catalog-wide diff -- worth a human
    # look first. (A bulk *re-copy* that preserves the bytes does not trip this:
    # CRC64NVME is FULL_OBJECT and content-derived, unlike ETag or LastModified.)
    if (
        n_tiles_restaged > args.max_restaged_tiles
        and not args.allow_large_restaging
        and not args.dry_run
    ):
        sys.exit(
            f"GUARDRAIL: {n_tiles_restaged} tiles were re-staged in place "
            f"(> {args.max_restaged_tiles}); rebuilding them all means one "
            "titiler request each -- review the diff, then rerun with "
            "--allow-large-restaging"
        )

    if (
        len(wesm_drift) > args.max_metadata_updates
        and not args.allow_large_metadata_updates
        and not args.dry_run
    ):
        sys.exit(
            f"GUARDRAIL: {len(wesm_drift)} collections drifted in WESM "
            f"(> {args.max_metadata_updates}); a WESM schema change hits every "
            "collection at once -- review the diff, then rerun with "
            "--allow-large-metadata-updates"
        )

    if args.dry_run:
        print("dry run -- exiting before rebuild")
        github_output(changed="false", changelog="dry run", title=title)
        if args.summary:
            summary["elapsed_s"] = round(time.time() - t0, 1)
            args.summary.write_text(json.dumps(summary, indent=1))
        if args.pr_body:
            args.pr_body.write_text(render_pr_body(summary, run_url()))
            print(f"PR body preview written to {args.pr_body}")
        return

    # ------------------------------------------------------------- rebuild
    failures = []
    for p in new:
        if not build_project(p):
            failures.append(p)
    for p in changed:
        if not build_project(p, overwrite=True, full=args.full_rebuild):
            failures.append(p)
    for p in removed:
        prune_project(p)
    # a rebuilt collection.json only links the current tile set, but item
    # files for tiles removed *in place* must be unlinked explicitly --
    # otherwise the file-count/walk-count parquet gate diverges and the same
    # projects get flagged as changed (and rebuilt) every night
    for p in changed:
        if p in failures:
            continue
        keep = set(holdings[p])
        for f in (CATALOG_DIR / p).glob("*.json"):
            if f.name != "collection.json" and f.stem not in keep:
                f.unlink()
    built_new = [p for p in new if p not in failures]

    # metadata-only refreshes, before the meta collection is regenerated below
    # (create_root_collection unions the per-collection temporal extents this
    # step can move). A failure here is per-collection and non-fatal: the old
    # values stay put and the drift is re-detected next run.
    metadata_failures, n_items_touched = [], 0
    for p in sorted(wesm_drift):
        try:
            n = refresh_collection_metadata(p, wesm_rows[p])
        except Exception as e:
            metadata_failures.append(p)
            details.pop(p, None)
            print(f"  {p}: metadata refresh failed ({e})", flush=True)
            continue
        details[p]["items_updated"] = n
        n_items_touched += n
        print(
            f"  METADATA {p}: summaries updated"
            + (f", {n} item datetimes rewritten" if n else ""),
            flush=True,
        )

    sync_root_catalog(removed=removed)
    # add new children + regenerate the meta collection (catalog/collection.json)
    import update_root_catalog

    update_root_catalog.update_root_catalog()
    update_root_catalog.create_root_collection()
    write_collections_txt()
    if failures:
        print(
            f"WARNING: {len(failures)} project builds failed (kept previous "
            f"version if any): {failures}",
            flush=True,
        )

    # ---------------------------------------------------------- validation
    new_urls = []
    for p in built_new + [p for p in changed if p not in failures]:
        new_urls += list(holdings[p].values())
    random.shuffle(new_urls)
    n, n404, nerr = head_check(new_urls[: args.sample_new], "new/changed", args.threads)
    if n404:
        sys.exit(
            f"VALIDATION: {n404}/{n} new/changed URLs return 404 -- not committing"
        )
    if n and nerr > max(2, 0.1 * n):
        sys.exit(
            f"VALIDATION: {nerr}/{n} new/changed HEAD checks errored -- "
            "network looks degraded, not committing"
        )

    # sample only projects NOT successfully rebuilt this run (untouched ones,
    # carried remnants, and failed rebuilds): rebuilt projects' URLs were just
    # HEAD-checked above, and including them would bias this gate away from
    # its purpose -- catching 404 drift in projects the run didn't touch
    rebuilt_ok = {p for p in changed if p not in failures} | set(built_new)
    carried = [
        (p, i)
        for p, ids in catalog_state(only).items()
        for i in ids
        if p not in rebuilt_ok and p in local and i in local.get(p, set())
    ]
    sample = random.sample(carried, min(args.sample_existing, len(carried)))
    urls = [u for u in (item_asset_url(p, i) for p, i in sample) if u]
    n, n404, nerr = head_check(urls, "existing", args.threads)
    if n and 100.0 * n404 / n > args.max_404_pct:
        sys.exit(
            f"VALIDATION: {n404}/{n} sampled existing URLs return 404 "
            f"(> {args.max_404_pct}%) -- catalog/S3 disagree, not committing"
        )
    if n and nerr > max(2, 0.1 * n):
        sys.exit(
            f"VALIDATION: {nerr}/{n} existing HEAD checks errored -- "
            "network looks degraded, not committing"
        )

    # ------------------------------------------------------------- summary
    parts = [f"+{n_tiles_added}/-{n_tiles_removed} tiles"]
    if new:
        parts.append(f"new: {', '.join(new)}")
    if changed:
        parts.append(f"rebuilt: {', '.join(changed)}")
    if removed:
        parts.append(f"pruned: {', '.join(removed)}")
    metadata_done = sorted(p for p in wesm_drift if p not in metadata_failures)
    if metadata_done:
        parts.append(f"metadata: {', '.join(metadata_done)}")
    if failures:
        parts.append(f"FAILED: {', '.join(failures)}")
    if metadata_failures:
        parts.append(f"METADATA FAILED: {', '.join(metadata_failures)}")
    changelog = "; ".join(parts)

    n_after = sum(len(v) for v in catalog_state(only).values())
    summary.update(
        {
            "catalog_items_after": n_after,
            "build_failures": failures,
            "metadata_projects": metadata_done,
            "metadata_failures": metadata_failures,
            "metadata_items_updated": n_items_touched,
            "changelog": changelog,
            "elapsed_s": round(time.time() - t0, 1),
        }
    )
    if args.summary:
        args.summary.write_text(json.dumps(summary, indent=1))
    if args.pr_body:
        args.pr_body.write_text(render_pr_body(summary, run_url()))
    # `title` and `subject` stay short enough to read in a PR list / git log;
    # the exhaustive project list lives in the PR body table and the summary JSON
    subject = (
        f"{title} ({len(new)} new, {len(changed)} rebuilt, "
        f"{len(removed)} pruned, {len(metadata_done)} metadata)"
    )
    github_output(
        changed="true",
        changelog=changelog,
        title=title,
        subject=subject,
        n_items=n_after,
        n_failures=len(failures),
    )
    print(f"\nrefresh complete in {summary['elapsed_s']}s: {changelog}")
    print(f"catalog items: {n_local} -> {n_after}")


if __name__ == "__main__":
    main()
