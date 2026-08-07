#!/usr/bin/env python
"""Incremental refresh of the static STAC catalog against current USGS holdings.

Designed to run unattended (nightly CI) but equally usable locally:

1. Discover the current 1m DEM tile set with a *paginated* S3 listing of
   s3://prd-tnm/StagedProducts/Elevation/1m/Projects/ (the same source of
   truth create_all.py uses, without the 1000-key truncation).
2. Diff against the local ./catalog item set:
     - new projects        -> build with create_static_stac.py
     - changed projects    -> rebuild with create_static_stac.py --overwrite
       (tile added/removed within an existing project, e.g. restaged data)
     - removed projects    -> prune catalog/<project> and root catalog link
3. Guardrails (issue #6: the bucket has been observed mid-repopulation):
     - abort if S3 lists fewer than --min-projects project folders
     - abort if the diff would remove more than --max-removed-tiles tiles
       (override with --allow-large-removals after human review)
4. Validate with HTTP HEAD spot checks:
     - every new/changed item URL sampled up to --sample-new must return 200
     - a random sample (--sample-existing) of carried-over item URLs must
       404 at a rate below --max-404-pct
5. Write a machine-readable summary (--summary) plus GitHub Actions outputs
   (changed / changelog / counts) when GITHUB_OUTPUT is set.

Exit codes: 0 = success (changed or no-op), 1 = guardrail/validation failure.

Usage:
  python scripts/refresh_catalog.py --dry-run          # report the diff only
  python scripts/refresh_catalog.py --summary refresh_summary.json
"""

import argparse
import concurrent.futures
import json
import os
import random
import shutil
import subprocess
import sys
import time
from pathlib import Path

import boto3
import requests
from botocore import UNSIGNED
from botocore.client import Config

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
    for page in paginator.paginate(Bucket=BUCKET, Prefix=PROJECTS_PREFIX, Delimiter="/"):
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
    """{project: {item_id: url}} for the entire bucket; skips tif-less folders."""
    folders = list_project_folders()
    print(f"S3 lists {len(folders)} project folders", flush=True)
    holdings = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as ex:
        for project, tifs in zip(folders, ex.map(list_project_tifs, folders)):
            if tifs:
                holdings[project] = tifs
    n_tiles = sum(len(v) for v in holdings.values())
    print(f"{len(holdings)} projects contain {n_tiles} tifs", flush=True)
    return holdings


# ------------------------------------------------------------ local catalog
def catalog_state():
    """{project: {item_id}} for the local static catalog (item JSON files)."""
    state = {}
    for coll in sorted(CATALOG_DIR.iterdir()):
        if not coll.is_dir():
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


# ------------------------------------------------------------------ rebuild
def build_project(project, overwrite=False):
    """Run create_static_stac.py for one project; True on success.

    Mirrors create_all.py: try as WESM workunit first, then as project.
    """
    script = Path(__file__).parent / "create_static_stac.py"
    for flag in ("--workunit", "--project"):
        cmd = [sys.executable, str(script), flag, project]
        if overwrite:
            cmd.append("--overwrite")
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
    names = sorted(p.name for p in CATALOG_DIR.iterdir() if p.is_dir())
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
    print(f"HEAD {label}: {len(codes)} checked, {n404} x 404, {nerr} errors", flush=True)
    return len(codes), n404, nerr


# ------------------------------------------------------------------ summary
def github_output(**kwargs):
    out = os.environ.get("GITHUB_OUTPUT")
    if not out:
        return
    with open(out, "a") as f:
        for k, v in kwargs.items():
            f.write(f"{k}={v}\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true",
                    help="report the diff and exit without modifying anything")
    ap.add_argument("--only", action="append", metavar="PROJECT",
                    help="restrict the diff/rebuild to named project(s) (testing)")
    ap.add_argument("--min-projects", type=int, default=900,
                    help="abort if S3 lists fewer project folders than this")
    ap.add_argument("--max-removed-tiles", type=int, default=2000,
                    help="abort if the diff would remove more tiles than this")
    ap.add_argument("--allow-large-removals", action="store_true",
                    help="override --max-removed-tiles after human review")
    ap.add_argument("--sample-new", type=int, default=200,
                    help="max new/changed item URLs to HEAD-check (all must be 200)")
    ap.add_argument("--sample-existing", type=int, default=300,
                    help="random carried-over item URLs to HEAD-check")
    ap.add_argument("--max-404-pct", type=float, default=1.0,
                    help="fail if more than this %% of sampled existing URLs 404")
    ap.add_argument("--threads", type=int, default=8,
                    help="S3 listing / HEAD check concurrency")
    ap.add_argument("--summary", type=Path, default=None,
                    help="write a JSON run summary to this path")
    args = ap.parse_args()

    if not CATALOG_DIR.is_dir():
        sys.exit("run from the repository root (catalog/ not found)")

    t0 = time.time()
    holdings = discover_s3(threads=args.threads)
    local = catalog_state()

    if args.only:
        holdings = {p: holdings.get(p, {}) for p in args.only}
        holdings = {p: v for p, v in holdings.items() if v}
        local = {p: v for p, v in local.items() if p in set(args.only)}
    elif len(holdings) < args.min_projects:
        sys.exit(f"GUARDRAIL: only {len(holdings)} projects listed on S3 "
                 f"(< {args.min_projects}); bucket may be mid-repopulation (see issue #6)")

    new = sorted(set(holdings) - set(local))
    removed = sorted(set(local) - set(holdings))
    changed = sorted(p for p in set(holdings) & set(local)
                     if set(holdings[p]) != local[p])

    n_tiles_added = (sum(len(holdings[p]) for p in new)
                     + sum(len(set(holdings[p]) - local[p]) for p in changed))
    n_tiles_removed = (sum(len(local[p]) for p in removed)
                       + sum(len(local[p] - set(holdings[p])) for p in changed))
    n_local = sum(len(v) for v in local.values())
    n_s3 = sum(len(v) for v in holdings.values())

    print(f"\ncatalog items: {n_local}  |  S3 tifs: {n_s3}")
    print(f"diff: +{n_tiles_added} tiles / -{n_tiles_removed} tiles  "
          f"({len(new)} new, {len(changed)} changed, {len(removed)} removed projects)")
    for p in new:
        print(f"  NEW      {p} ({len(holdings[p])} tiles)")
    for p in changed:
        print(f"  CHANGED  {p} (+{len(set(holdings[p]) - local[p])} / "
              f"-{len(local[p] - set(holdings[p]))})")
    for p in removed:
        print(f"  REMOVED  {p} ({len(local[p])} tiles)")

    changed_any = bool(new or changed or removed)
    summary = {
        "s3_projects": len(holdings), "s3_tiles": n_s3,
        "catalog_projects_before": len(local), "catalog_items_before": n_local,
        "new_projects": new, "changed_projects": changed, "removed_projects": removed,
        "tiles_added": n_tiles_added, "tiles_removed": n_tiles_removed,
        "dry_run": args.dry_run, "build_failures": [],
    }

    if not changed_any:
        print("no changes -- catalog is current")
        github_output(changed="false", changelog="no changes")
        if args.summary:
            summary["elapsed_s"] = round(time.time() - t0, 1)
            args.summary.write_text(json.dumps(summary, indent=1))
        return

    if (n_tiles_removed > args.max_removed_tiles and not args.allow_large_removals
            and not args.dry_run):
        sys.exit(f"GUARDRAIL: refusing to remove {n_tiles_removed} tiles "
                 f"(> {args.max_removed_tiles}); rerun with --allow-large-removals "
                 "after reviewing the diff")

    if args.dry_run:
        print("dry run -- exiting before rebuild")
        github_output(changed="false", changelog="dry run")
        if args.summary:
            summary["elapsed_s"] = round(time.time() - t0, 1)
            args.summary.write_text(json.dumps(summary, indent=1))
        return

    # ------------------------------------------------------------- rebuild
    failures = []
    for p in new:
        if not build_project(p):
            failures.append(p)
    for p in changed:
        if not build_project(p, overwrite=True):
            failures.append(p)
    for p in removed:
        prune_project(p)
    built_new = [p for p in new if p not in failures]
    sync_root_catalog(removed=removed)
    # add new children + regenerate the meta collection (catalog/collection.json)
    import update_root_catalog
    update_root_catalog.update_root_catalog()
    update_root_catalog.create_root_collection()
    write_collections_txt()
    if failures:
        print(f"WARNING: {len(failures)} project builds failed (kept previous "
              f"version if any): {failures}", flush=True)

    # ---------------------------------------------------------- validation
    new_urls = []
    for p in built_new + [p for p in changed if p not in failures]:
        new_urls += list(holdings[p].values())
    random.shuffle(new_urls)
    n, n404, nerr = head_check(new_urls[: args.sample_new], "new/changed", args.threads)
    if n404:
        sys.exit(f"VALIDATION: {n404}/{n} new/changed URLs return 404 -- not committing")

    carried = [(p, i) for p, ids in catalog_state().items()
               for i in ids if p in local and i in local.get(p, set())]
    sample = random.sample(carried, min(args.sample_existing, len(carried)))
    urls = [u for u in (item_asset_url(p, i) for p, i in sample) if u]
    n, n404, nerr = head_check(urls, "existing", args.threads)
    if n and 100.0 * n404 / n > args.max_404_pct:
        sys.exit(f"VALIDATION: {n404}/{n} sampled existing URLs return 404 "
                 f"(> {args.max_404_pct}%) -- catalog/S3 disagree, not committing")

    # ------------------------------------------------------------- summary
    parts = [f"+{n_tiles_added}/-{n_tiles_removed} tiles"]
    if new:
        parts.append(f"new: {', '.join(new)}")
    if changed:
        parts.append(f"rebuilt: {', '.join(changed)}")
    if removed:
        parts.append(f"pruned: {', '.join(removed)}")
    if failures:
        parts.append(f"FAILED: {', '.join(failures)}")
    changelog = "; ".join(parts)

    n_after = sum(len(v) for v in catalog_state().values())
    summary.update({
        "catalog_items_after": n_after, "build_failures": failures,
        "changelog": changelog, "elapsed_s": round(time.time() - t0, 1),
    })
    if args.summary:
        args.summary.write_text(json.dumps(summary, indent=1))
    github_output(changed="true", changelog=changelog,
                  n_items=n_after, n_failures=len(failures))
    print(f"\nrefresh complete in {summary['elapsed_s']}s: {changelog}")
    print(f"catalog items: {n_local} -> {n_after}")


if __name__ == "__main__":
    main()
