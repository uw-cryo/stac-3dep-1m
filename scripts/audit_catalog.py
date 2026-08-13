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

Tiles are audited from one flat pool, not project by project: sampling is
per-collection but the work is not, and pooling per collection left the run
bounded by its largest collection (KS_Statewide_2018_A18 alone is 1692 tiles).

Usage:
    pixi run audit                           # 3 tiles from every collection
    pixi run audit --sample 10               # deeper sample
    pixi run audit --project AZ_Eastern_D24  # one collection, every tile
    pixi run audit --all-tiles               # everything
    pixi run audit --all-tiles --part 3/12   # just the 3rd of 12 equal parts
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

# Collections resolved concurrently in the planning phase. Each one is an S3
# LIST plus a HEAD per sampled tile, so this is cheap next to the audit itself.
PLAN_THREADS = 16

# Cost charged to a collection beyond its tiles when balancing parts: the S3
# LIST and the WESM lookup happen once per collection whether it contributes
# one tile or a thousand, and a part of 400 tiny collections is otherwise
# packed as though it were free.
PER_COLLECTION_COST = 0.5


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


def audit_item(task):
    """(findings, error) for one tile.

    The two are kept apart deliberately. A finding is a statement about the
    catalog -- rebuilding this item would change it. An error means the tile
    could not be checked at all, which at high concurrency is usually the
    titiler lambda shedding load, and reporting that as drift would be a false
    alarm of exactly the kind this script exists to rule out.
    """
    _, path, live, datetime_str = task
    try:
        item = json.loads(path.read_text())
    except Exception as e:
        return [], f"item unreadable: {e}"

    asset = item.get("assets", {}).get(create_static_stac.ASSET_NAME, {})
    url = asset.get("href")
    if not url:
        return ["item has no elevation asset href"], None
    if live is None:
        return ["tile not present in S3 listing"], None

    findings = []
    for field in create_static_stac.FILE_FIELDS:
        have, want = asset.get(field), live.get(field)
        if have is not None and want is not None and have != want:
            findings.append(f"{field}: item {have!r} != s3 {want!r}")

    try:
        fresh = create_static_stac.normalize_projection(
            json.loads(create_static_stac.create_stac_item(url, datetime_str))
        )
    except Exception as e:
        return findings, f"re-derive failed: {str(e).splitlines()[0][:160]}"

    return findings + deep_diff(strip_for_compare(item), strip_for_compare(fresh)), None


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


def plan_project(project, sample, exhaustive, seed, wesm_df):
    """([(project, path, live_meta, datetime_str), ...], note) for one collection.

    live_meta is the tile's S3 file metadata, or None when the tile is not in
    the project's listing at all -- which audit_item reports as a finding
    without spending a titiler request on it.
    """
    paths = item_paths(project)
    if not paths:
        return [], "no items"

    datetime_str = project_datetime(project, wesm_df)
    if datetime_str is None:
        return [], "no WESM row (should have been pruned -- see issue #23)"

    # seeded per project rather than drawn from a shared stream, so a project
    # samples the same tiles no matter which part it lands in or how many
    # other projects are being audited alongside it
    if not exhaustive and sample < len(paths):
        paths = sorted(random.Random(f"{seed}:{project}").sample(paths, sample))

    try:
        urls = create_static_stac.list_tiffs_in_project(project)
        by_id = {url.split("/")[-1][:-4]: url for url in urls}
        # HEAD only what is actually audited: this used to HEAD every tile in
        # the project regardless of the sample size, which for the default
        # 3-tile sample is ~40x more requests than the run reads
        wanted = [by_id[p.stem] for p in paths if p.stem in by_id]
        live = create_static_stac.file_metadata(
            wanted, threads=min(16, len(wanted) or 1)
        )
    except Exception as e:
        return [], f"S3 listing failed: {e}"

    return [(project, p, live.get(p.stem), datetime_str) for p in paths], None


def split_parts(projects, weights, nparts):
    """Projects split into nparts groups of roughly equal cost.

    Greedy longest-processing-time bin packing: heaviest first into whichever
    part is currently lightest. Collections vary from 1 to 1692 tiles, so
    slicing the list evenly would leave one part running long after the rest.
    Deterministic, so `--part 3/12` means the same thing in CI and locally.
    """
    parts = [[] for _ in range(nparts)]
    load = [0.0] * nparts
    for project in sorted(projects, key=lambda p: (-weights[p], p)):
        i = min(range(nparts), key=lambda i: (load[i], i))
        parts[i].append(project)
        load[i] += weights[project]
    return [sorted(p) for p in parts]


def parse_part(text):
    """'3/12' -> (3, 12), validated."""
    try:
        i, n = (int(x) for x in text.split("/", 1))
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected i/N, got {text!r}") from None
    if n < 1 or not 1 <= i <= n:
        raise argparse.ArgumentTypeError(f"part {i} of {n} is out of range")
    return i, n


def write_step_summary(report):
    """Render a report to the GitHub job summary, if we are in Actions."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    findings, errors, notes = report["findings"], report["errors"], report["notes"]
    with open(path, "a") as f:
        f.write("## Catalog audit\n\n")
        f.write(
            f"**{len(findings)} mismatched** of {report['tiles_checked']:,} tiles "
            f"checked across {report['collections']:,} collections "
            f"({report['scope']}). A mismatch means rebuilding that item would "
            "change it.\n\n"
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
        if errors:
            f.write(
                f"\n**{len(errors)} tiles could not be checked** (after a retry). "
                "These are not catalog drift — they are tiles the audit failed to "
                "reach, usually titiler shedding load.\n\n"
            )
            f.write("| Item | Collection | Error |\n| --- | --- | --- |\n")
            for item_id, d in sorted(errors.items())[:25]:
                f.write(f"| `{item_id}` | `{d['project']}` | {d['error']} |\n")
            if len(errors) > 25:
                f.write(f"\n… and {len(errors) - 25} more (see the artifact).\n")
        if notes:
            f.write("\n**Skipped:**\n\n")
            for p, n in sorted(notes.items()):
                f.write(f"- `{p}` — {n}\n")


def print_totals(report):
    print(f"\ncollections audited: {report['collections']}")
    print(f"tiles checked:       {report['tiles_checked']}")
    print(f"tiles mismatched:    {len(report['findings'])}")
    if report["errors"]:
        print(f"tiles unchecked:     {len(report['errors'])} (errors, not drift)")
    if report["notes"]:
        print(f"collections skipped: {len(report['notes'])}")


def merge_reports(directory):
    """Combine the per-part reports written by a matrix run into one.

    Verifies every part reported: a part whose job died would otherwise shrink
    the totals silently, and the merged run would look clean because the tiles
    it would have flagged were never checked.
    """
    paths = sorted(Path(directory).rglob("*.json"))
    if not paths:
        sys.exit(f"no reports found under {directory}")

    merged = {
        "collections": 0,
        "tiles_checked": 0,
        "findings": {},
        "errors": {},
        "notes": {},
        "scope": None,
        "sample_per_collection": None,
        "seed": None,
        "parts": [],
    }
    nparts = None
    for path in paths:
        r = json.loads(path.read_text())
        merged["collections"] += r["collections"]
        merged["tiles_checked"] += r["tiles_checked"]
        merged["findings"].update(r["findings"])
        merged["errors"].update(r["errors"])
        merged["notes"].update(r["notes"])
        merged["scope"] = r["scope"]
        merged["sample_per_collection"] = r["sample_per_collection"]
        merged["seed"] = r["seed"]
        if r.get("part"):
            i, n = r["part"]
            merged["parts"].append(i)
            nparts = n

    missing = []
    if nparts is not None:
        missing = sorted(set(range(1, nparts + 1)) - set(merged["parts"]))
    merged["parts"] = sorted(merged["parts"])
    merged["missing_parts"] = missing
    print(f"merged {len(paths)} reports")
    return merged


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
        help="audit every tile of every selected collection",
    )
    ap.add_argument(
        "--seed",
        type=int,
        default=0,
        help="sampling seed, so a reported finding can be reproduced",
    )
    ap.add_argument(
        "--part",
        type=parse_part,
        metavar="I/N",
        help="audit only part I of N, collections balanced by tile count "
        "(for splitting one audit across parallel runners)",
    )
    ap.add_argument(
        "--threads",
        type=int,
        default=32,
        help="concurrent titiler requests (default 32)",
    )
    ap.add_argument("--json", type=Path, help="write the full report here")
    ap.add_argument(
        "--merge-reports",
        type=Path,
        metavar="DIR",
        help="combine the per-part reports under DIR instead of auditing",
    )
    ap.add_argument(
        "--fail-on-mismatch",
        action="store_true",
        help="exit 1 if anything mismatched (for CI)",
    )
    ap.add_argument(
        "--max-error-pct",
        type=float,
        default=1.0,
        help="fail if more than this %% of tiles could not be checked (default 1.0). "
        "A handful of unreachable tiles is noise; a run that mostly failed to "
        "reach titiler is not evidence the catalog is clean",
    )
    args = ap.parse_args()

    if args.merge_reports:
        report = merge_reports(args.merge_reports)
        print_totals(report)
        if report["missing_parts"]:
            print(f"MISSING parts: {report['missing_parts']}")
        write_step_summary(report)
        if args.json:
            args.json.write_text(json.dumps(report, indent=2))
            print(f"report written to {args.json}")
        if report["missing_parts"]:
            sys.exit(
                f"parts {report['missing_parts']} never reported -- "
                "the merged result is incomplete, not clean"
            )
        return finish(report, args)

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

    if args.part:
        part, nparts = args.part
        if nparts > len(projects):
            sys.exit(
                f"--part {part}/{nparts} but only {len(projects)} collections "
                "selected; use fewer parts"
            )
        # weighted by what this run will actually do to each collection, which
        # is the whole collection only when auditing exhaustively
        counts = {p: len(item_paths(p)) for p in projects}
        weights = {
            p: (n if exhaustive else min(sample, n)) + PER_COLLECTION_COST
            for p, n in counts.items()
        }
        projects = split_parts(projects, weights, nparts)[part - 1]

    scope = "every tile" if exhaustive else f"up to {sample} tiles"
    where = f" (part {args.part[0]} of {args.part[1]})" if args.part else ""
    print(f"auditing {len(projects)} collections{where} ({scope} each)", flush=True)

    # loaded once: get_wesm_series() would otherwise re-read the CSV per project
    wesm_df = create_static_stac.load_wesm()

    # Phase 1: resolve each collection to its tile-level tasks. Cheap (one S3
    # LIST plus a HEAD per sampled tile) and worth doing up front, so phase 2
    # can balance every tile against every other rather than per collection.
    tasks = []
    notes = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=PLAN_THREADS) as ex:
        futures = {
            ex.submit(plan_project, p, sample, exhaustive, args.seed, wesm_df): p
            for p in projects
        }
        for fut in concurrent.futures.as_completed(futures):
            project_tasks, note = fut.result()
            tasks += project_tasks
            if note:
                notes[futures[fut]] = note
                print(f"  ?  {futures[fut]}: {note}", flush=True)
    tasks.sort(key=lambda t: (t[0], t[1]))
    print(f"{len(tasks)} tiles to check", flush=True)

    # Phase 2: one flat pool over tiles. Pooling per collection instead left
    # every run bounded by its largest collection, since items inside one were
    # walked serially.
    findings, errors = {}, {}

    def checked(task):
        """audit_item, but an unexpected exception is an error, not a crash."""
        try:
            return audit_item(task)
        except Exception as e:  # noqa: BLE001 - never lose a long run to one tile
            return [], f"{type(e).__name__}: {str(e).splitlines()[0][:160]}"

    def run(pending, threads, label):
        failed = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as ex:
            results = zip(pending, ex.map(checked, pending))
            for done, (task, (problems, error)) in enumerate(results, 1):
                project, path = task[0], task[1]
                if problems:
                    findings[path.stem] = {"project": project, "problems": problems}
                    print(f"  MISMATCH {path.stem}", flush=True)
                    for p in problems:
                        print(f"      {p}", flush=True)
                if error:
                    failed.append((task, error))
                    print(f"  ERROR {path.stem}: {error}", flush=True)
                if done % 2000 == 0:
                    print(f"  ...{done}/{len(pending)} tiles {label}", flush=True)
        return failed

    failed = run(tasks, args.threads, "checked")

    # One retry at low concurrency. Nearly every error here is the lambda
    # shedding load, and backing off clears it -- but retrying at full width
    # would just reproduce the conditions that caused it.
    if failed:
        print(
            f"\nretrying {len(failed)} unchecked tiles at low concurrency", flush=True
        )
        for task, error in run(
            [t for t, _ in failed], max(2, args.threads // 8), "retried"
        ):
            errors[task[1].stem] = {"project": task[0], "error": error}

    report = {
        "collections": len(projects),
        "tiles_checked": len(tasks),
        "tiles_mismatched": len(findings),
        "tiles_errored": len(errors),
        "sample_per_collection": None if exhaustive else sample,
        "scope": scope,
        "seed": args.seed,
        "part": list(args.part) if args.part else None,
        "findings": findings,
        "errors": errors,
        "notes": notes,
    }
    print_totals(report)
    write_step_summary(report)
    if args.json:
        args.json.write_text(json.dumps(report, indent=2))
        print(f"report written to {args.json}")
    return finish(report, args)


def finish(report, args):
    """Exit code. Drift and unreachable tiles fail for different reasons."""
    if not args.fail_on_mismatch:
        return
    checked = report["tiles_checked"]
    error_pct = 100.0 * len(report["errors"]) / checked if checked else 0.0
    if error_pct > args.max_error_pct:
        sys.exit(
            f"{len(report['errors'])} of {checked} tiles ({error_pct:.1f}%) could "
            f"not be checked, over --max-error-pct {args.max_error_pct} -- this "
            "run is inconclusive, not clean"
        )
    if report["findings"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
