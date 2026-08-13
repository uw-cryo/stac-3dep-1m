#!/usr/bin/env python
"""Stamp file:size + file:checksum onto every item already in catalog/ (issue #17).

The catalog predates the STAC file extension, so ~124k committed items carry no
record of the bytes they describe. This establishes that baseline in place:

  * one paginated S3 listing per collection to find the live tiles,
  * one HEAD per tile for ContentLength + ChecksumCRC64NVME (~1100/s, so a few
    minutes for the whole catalog),
  * rewrite each item's asset with the same add_file_metadata() the builder uses.

No titiler round trip and no tile data is read, because none is needed: size and
checksum both come from S3 object metadata. Everything else in the item -- the
geometry, projection and raster statistics titiler derived -- is left untouched.

That makes this a *go-forward* baseline, not an audit. An item whose tile was
already re-staged before this ran gets today's size and checksum paired with
yesterday's geometry, and will look self-consistent forever after. Correcting
those needs `refresh --full-rebuild`, which re-derives every item from titiler;
this script deliberately does not, because a 3.5 h titiler pass is a much larger
and more fragile change than recording metadata S3 already has.

Idempotent: re-running rewrites only items whose recorded values disagree with
S3, so a second run is a no-op and prints 0 updated.

Usage:
    pixi run python scripts/backfill_file_metadata.py [--only PROJECT] [--dry-run]
"""

import argparse
import concurrent.futures
import json
from pathlib import Path

import create_static_stac

CATALOG_DIR = Path("./catalog")


def collection_dirs(only=None):
    """Child collection directories (those that hold a collection.json)."""
    return sorted(
        p
        for p in CATALOG_DIR.iterdir()
        if p.is_dir()
        and (p / "collection.json").is_file()
        and (only is None or p.name in only)
    )


def backfill_project(project, dry_run=False):
    """(n_items, n_updated, n_missing_checksum, n_orphaned) for one collection."""
    urls = create_static_stac.list_tiffs_in_project(project)
    live = create_static_stac.file_metadata(urls) if urls else {}

    n_items = n_updated = n_orphaned = 0
    for path in sorted((CATALOG_DIR / project).glob("*.json")):
        if path.name == "collection.json":
            continue
        n_items += 1
        meta = live.get(path.stem)
        if meta is None:
            # cataloged tile with no live tif: a prune candidate, which is
            # refresh_catalog.py's decision to make (and its guardrails to
            # apply), not this script's. Leave it exactly as it is.
            n_orphaned += 1
            continue
        item = json.loads(path.read_text())
        if create_static_stac.item_file_metadata(item) == meta:
            continue  # already current
        create_static_stac.add_file_metadata(item, meta)
        if not dry_run:
            path.write_text(create_static_stac.dump_stac_json(item))
        n_updated += 1

    n_missing = sum(1 for m in live.values() if "file:checksum" not in m)
    return n_items, n_updated, n_missing, n_orphaned


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--only",
        action="append",
        metavar="PROJECT",
        help="restrict the backfill to named collection(s)",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would change without writing anything",
    )
    ap.add_argument(
        "--threads",
        type=int,
        default=8,
        help="collections processed concurrently (each fans out its own HEADs)",
    )
    args = ap.parse_args()

    projects = [p.name for p in collection_dirs(args.only)]
    print(f"backfilling {len(projects)} collections", flush=True)

    totals = [0, 0, 0, 0]
    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.threads) as ex:
        futures = {ex.submit(backfill_project, p, args.dry_run): p for p in projects}
        for fut in concurrent.futures.as_completed(futures):
            project = futures[fut]
            result = fut.result()
            n_items, n_updated, n_missing, n_orphaned = result
            totals = [a + b for a, b in zip(totals, result)]
            done += 1
            if n_missing or n_orphaned:
                print(
                    f"  {project}: {n_updated}/{n_items} updated, "
                    f"{n_missing} without checksum, {n_orphaned} not in S3",
                    flush=True,
                )
            if done % 100 == 0:
                print(f"  ...{done}/{len(projects)} collections", flush=True)

    n_items, n_updated, n_missing, n_orphaned = totals
    print(f"\nitems seen:            {n_items}")
    print(f"items updated:         {n_updated}{' (dry run)' if args.dry_run else ''}")
    print(f"tiles without checksum:{n_missing}")
    print(f"items with no live tif:{n_orphaned} (left for refresh to decide)")


if __name__ == "__main__":
    main()
