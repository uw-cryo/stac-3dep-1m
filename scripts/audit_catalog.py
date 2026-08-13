#!/usr/bin/env python
"""Audit committed STAC items by asking whether a rebuild would change them.

file:size/file:checksum (issue #17) give the catalog a *go-forward* baseline:
they were recorded from live S3, so a tile re-staged before that backfill now
pairs today's bytes with yesterday's geometry and looks self-consistent forever.
Nothing in the refresh diff can surface those, because the tile names never
moved. This can, by ignoring the recorded metadata entirely and re-deriving the
item from the tile as it stands today.

The re-derivation goes through create_static_stac.create_stac_item -- the same
titiler endpoint the builder uses -- so what is audited is the actual production
code path, and a clean result means a rebuild would be a no-op. A GeoTIFF header
read would be ~4x faster but can only confirm the proj:* fields; the WGS84
geometry/bbox and proj:geometry are produced by titiler reprojecting, and
nothing read locally can check them. On a drifted tile in
AZ_NavajoCorridor_2020_D20 those were wrong too.

Each sampled tile is checked for three things:

    * the tile still exists in the project's S3 listing
    * file:size / file:checksum still match S3 object metadata -- the titiler
      diff below cannot see these, since titiler never sees object metadata
    * a freshly derived item matches the committed one everywhere else

Usage:
    pixi run audit                           # 3 tiles from every collection
    pixi run audit --sample 10               # deeper sample
    pixi run audit --project AZ_Eastern_D24  # one collection, every tile
    pixi run audit --all-tiles               # everything (~3.5 h at ~10 tiles/s)
"""

import argparse
import concurrent.futures
import copy
import json
import math
import os
import random
import sys
from pathlib import Path

import create_static_stac

CATALOG_DIR = Path("./catalog")

# Differences a rebuild always produces, which are not drift. Determined
# empirically by diffing a known-good tile, which differs in these and in
# nothing else:
#   links            pystac rewrites them wholesale in normalize_hrefs()/save()
#   stac_extensions  the file extension is added by us, after titiler answers
#   file:size        titiler never sees S3 object metadata (checked separately
#   file:checksum      against S3, above, so dropping them here is not a gap)
#   statistics       titiler's own nondeterminism: the same tile re-read returns
#   histogram        mean -0.43414297933755347 vs -0.43414293580320523
IGNORED_ITEM_KEYS = ("links", "stac_extensions")
IGNORED_BAND_KEYS = ("statistics", "histogram")

# 1-ULP coordinate noise is expected from the same endpoint run to run (it
# returned 47.40217321706787 against a committed 47.40217321706786); anything
# above this is a real disagreement
REL_TOL = 1e-12
ABS_TOL = 1e-9


def collection_dirs(only=None):
    return sorted(
        p
        for p in CATALOG_DIR.iterdir()
        if p.is_dir()
        and (p / "collection.json").is_file()
        and (only is None or p.name in only)
    )


def item_paths(project):
    return sorted(
        p for p in (CATALOG_DIR / project).glob("*.json") if p.name != "collection.json"
    )


def strip_for_compare(item):
    """Item reduced to the fields a rebuild is expected to reproduce exactly."""
    d = copy.deepcopy(item)
    for key in IGNORED_ITEM_KEYS:
        d.pop(key, None)
    for asset in d.get("assets", {}).values():
        for field in create_static_stac.FILE_FIELDS:
            asset.pop(field, None)
        for band in asset.get("raster:bands") or []:
            for key in IGNORED_BAND_KEYS:
                band.pop(key, None)
    return d


def deep_diff(a, b, path=""):
    """Paths where a and b disagree, tolerating float noise."""
    if isinstance(a, dict) and isinstance(b, dict):
        out = []
        for key in sorted(set(a) | set(b)):
            if key not in a:
                out.append(f"{path}/{key}: absent in item, fresh has {b[key]!r}")
            elif key not in b:
                out.append(f"{path}/{key}: item has {a[key]!r}, absent in fresh")
            else:
                out += deep_diff(a[key], b[key], f"{path}/{key}")
        return out
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return [f"{path}: length {len(a)} != {len(b)}"]
        out = []
        for i, (x, y) in enumerate(zip(a, b)):
            out += deep_diff(x, y, f"{path}[{i}]")
        return out
    if (
        isinstance(a, (int, float))
        and isinstance(b, (int, float))
        and not isinstance(a, bool)
        and not isinstance(b, bool)
    ):
        if math.isclose(a, b, rel_tol=REL_TOL, abs_tol=ABS_TOL):
            return []
        return [f"{path}: item {a!r} != fresh {b!r}"]
    if a != b:
        return [f"{path}: item {a!r} != fresh {b!r}"]
    return []


def audit_item(path, live, datetime_str):
    """[] if a rebuild would leave this item unchanged, else the differences."""
    try:
        item = json.loads(path.read_text())
    except Exception as e:
        return [f"item unreadable: {e}"]

    asset = item.get("assets", {}).get(create_static_stac.ASSET_NAME, {})
    url = asset.get("href")
    if not url:
        return ["item has no elevation asset href"]
    if live is None:
        return ["tile not present in S3 listing"]

    problems = []
    for field in create_static_stac.FILE_FIELDS:
        have, want = asset.get(field), live.get(field)
        if have is not None and want is not None and have != want:
            problems.append(f"{field}: item {have!r} != s3 {want!r}")

    try:
        fresh = create_static_stac.normalize_projection(
            json.loads(create_static_stac.create_stac_item(url, datetime_str))
        )
    except Exception as e:
        return problems + [f"re-derive failed: {str(e).splitlines()[0][:160]}"]

    return problems + deep_diff(strip_for_compare(item), strip_for_compare(fresh))


def project_datetime(project, wesm_df):
    """The datetime range a rebuild would stamp on this project's items.

    Mirrors refresh_catalog.build_project: the S3 folder name is sometimes the
    WESM workunit and sometimes the project, so try both. Since issue #23 a
    cataloged collection that resolves to neither is pruned, so None here means
    the catalog and WESM have diverged -- worth reporting, not working around.
    """
    for is_workunit in (True, False):
        try:
            series = create_static_stac.get_wesm_series(
                project, is_workunit=is_workunit, df=wesm_df, warn=False
            )
        except (ValueError, KeyError, IndexError):
            continue
        return create_static_stac.get_titiler_datetime(series)
    return None


def audit_project(project, sample, rng, exhaustive, wesm_df):
    """(n_checked, {item_id: [problem, ...]}, note) for one collection."""
    paths = item_paths(project)
    if not paths:
        return 0, {}, "no items"
    if not exhaustive and sample < len(paths):
        paths = rng.sample(paths, sample)
    paths = sorted(paths)

    datetime_str = project_datetime(project, wesm_df)
    if datetime_str is None:
        return 0, {}, "no WESM row (should have been pruned -- see issue #23)"

    try:
        urls = create_static_stac.list_tiffs_in_project(project)
        live = create_static_stac.file_metadata(urls)
    except Exception as e:
        return 0, {}, f"S3 listing failed: {e}"

    findings = {}
    for path in paths:
        problems = audit_item(path, live.get(path.stem), datetime_str)
        if problems:
            findings[path.stem] = problems
    return len(paths), findings, None


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--project",
        action="append",
        metavar="PROJECT",
        help="audit only this collection, every tile of it unless --sample is given",
    )
    ap.add_argument(
        "--sample",
        type=int,
        default=None,
        help="tiles sampled per collection (default 3; ignored with --all-tiles)",
    )
    ap.add_argument(
        "--all-tiles",
        action="store_true",
        help="audit every tile of every selected collection (~3.5 h)",
    )
    ap.add_argument(
        "--seed",
        type=int,
        default=0,
        help="sampling seed, so a reported finding can be reproduced",
    )
    ap.add_argument(
        "--threads", type=int, default=12, help="collections audited concurrently"
    )
    ap.add_argument("--json", type=Path, help="write the full report here")
    ap.add_argument(
        "--fail-on-mismatch",
        action="store_true",
        help="exit 1 if anything mismatched (for CI)",
    )
    args = ap.parse_args()

    if not CATALOG_DIR.is_dir():
        sys.exit("run from the repository root (catalog/ not found)")

    # naming a project means you want that project looked at properly, so it is
    # exhaustive unless --sample says otherwise
    exhaustive = args.all_tiles or (args.project and args.sample is None)
    sample = args.sample if args.sample is not None else 3
    projects = [
        p.name for p in collection_dirs(set(args.project) if args.project else None)
    ]
    if args.project:
        missing = sorted(set(args.project) - set(projects))
        if missing:
            sys.exit(f"not in catalog/: {', '.join(missing)}")
    scope = "every tile" if exhaustive else f"up to {sample} tiles"
    print(f"auditing {len(projects)} collections ({scope} each)", flush=True)

    # loaded once: get_wesm_series() would otherwise re-read the CSV per project
    wesm_df = create_static_stac.load_wesm()

    rng = random.Random(args.seed)
    seeds = {p: random.Random(rng.random()) for p in projects}

    n_checked = 0
    findings = {}
    notes = {}
    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.threads) as ex:
        futures = {
            ex.submit(audit_project, p, sample, seeds[p], exhaustive, wesm_df): p
            for p in projects
        }
        for fut in concurrent.futures.as_completed(futures):
            project = futures[fut]
            checked, found, note = fut.result()
            n_checked += checked
            done += 1
            if note:
                notes[project] = note
                print(f"  ?  {project}: {note}", flush=True)
            for item_id, problems in found.items():
                findings[item_id] = {"project": project, "problems": problems}
                print(f"  MISMATCH {item_id}", flush=True)
                for p in problems:
                    print(f"      {p}", flush=True)
            if done % 100 == 0:
                print(f"  ...{done}/{len(projects)} collections", flush=True)

    print(f"\ncollections audited: {len(projects)}")
    print(f"tiles checked:       {n_checked}")
    print(f"tiles mismatched:    {len(findings)}")
    if notes:
        print(f"collections skipped: {len(notes)}")

    report = {
        "collections": len(projects),
        "tiles_checked": n_checked,
        "tiles_mismatched": len(findings),
        "sample_per_collection": None if exhaustive else sample,
        "seed": args.seed,
        "findings": findings,
        "notes": notes,
    }
    if args.json:
        args.json.write_text(json.dumps(report, indent=2))
        print(f"report written to {args.json}")

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write("## Catalog audit\n\n")
            f.write(
                f"**{len(findings)} mismatched** of {n_checked:,} tiles checked "
                f"across {len(projects):,} collections ({scope}). A mismatch means "
                "rebuilding that item would change it.\n\n"
            )
            if findings:
                f.write("| Item | Collection | Difference |\n| --- | --- | --- |\n")
                for item_id, d in sorted(findings.items())[:100]:
                    for p in d["problems"]:
                        f.write(f"| `{item_id}` | `{d['project']}` | {p} |\n")
                if len(findings) > 100:
                    f.write(f"\n… and {len(findings) - 100} more (see the artifact).\n")
            else:
                f.write("Every audited item is what a rebuild would produce.\n")
            if notes:
                f.write("\n**Skipped:**\n\n")
                for p, n in sorted(notes.items()):
                    f.write(f"- `{p}` — {n}\n")

    if findings and args.fail_on_mismatch:
        sys.exit(1)


if __name__ == "__main__":
    main()
