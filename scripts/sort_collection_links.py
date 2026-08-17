#!/usr/bin/env python
"""Put every committed collection.json into the order a rebuild now produces.

create_static_stac.create_stac_catalog() sorts items by id before
collection.add_items(), so link order and the proj:code summary no longer
depend on how many tiles came from titiler versus reuse. This brings the
already-committed collections to that same order without a titiler round trip
-- the alternative is one request per tile across the whole catalog to change
nothing but the order of a list.

Two things are order-dependent, and only these two. The wesm:* summaries are
lists too since 2026-08 (wrap_wesm_summaries.py), but each holds exactly one
value, so nothing about them can be reordered:

    * the rel="item" links, which are add_items() order
    * the proj:code summary, which pystac's Summarizer builds by walking those
      same links, so its order is *first-seen in item order* -- not sorted.
      329 collections have more than one code.

Other links (root, self, parent, license) keep their relative position: pystac
emits them in the order they were added, and only the item links move.

Idempotent: a second run reports 0 changed.
"""

import argparse
import json
from pathlib import Path

import create_static_stac

CATALOG_DIR = Path("./catalog")


def sorted_collection(coll, item_ids_in_order, proj_codes_by_item):
    """The collection dict with item links and proj:code in rebuild order.

    Returns a new dict; the caller decides whether anything changed.
    """
    d = json.loads(json.dumps(coll))

    # item links, sorted by the id they point at, left where the block already
    # sits so root/self/parent/license keep their relative position
    links = d.get("links", [])
    item_idx = [i for i, link in enumerate(links) if link.get("rel") == "item"]
    if item_idx:
        by_href = {link["href"]: link for link in (links[i] for i in item_idx)}
        ordered = [
            by_href[f"./{item_id}.json"]
            for item_id in item_ids_in_order
            if f"./{item_id}.json" in by_href
        ]
        # any link whose target is not among the items on disk stays put rather
        # than being dropped: this script reorders, it does not prune
        leftover = [
            link for link in (links[i] for i in item_idx) if link not in ordered
        ]
        for slot, link in zip(item_idx, ordered + leftover):
            links[slot] = link

    # proj:code in first-seen order over the sorted items, which is what the
    # Summarizer produces from the reordered links
    summaries = d.get("summaries", {})
    if "proj:code" in summaries:
        seen = []
        for item_id in item_ids_in_order:
            code = proj_codes_by_item.get(item_id)
            if code is not None and code not in seen:
                seen.append(code)
        # only reorder what is already there; never invent or drop a code
        if sorted(seen) == sorted(summaries["proj:code"]):
            summaries["proj:code"] = seen
    return d


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--dry-run", action="store_true", help="report what would change, write nothing"
    )
    args = ap.parse_args()

    if not CATALOG_DIR.is_dir():
        raise SystemExit("run from the repository root (catalog/ not found)")

    changed = 0
    scanned = 0
    for coll_path in sorted(CATALOG_DIR.glob("*/collection.json")):
        scanned += 1
        project = coll_path.parent.name
        item_paths = sorted(
            p for p in coll_path.parent.glob("*.json") if p.name != "collection.json"
        )
        item_ids = [p.stem for p in item_paths]
        proj_codes = {}
        for p in item_paths:
            try:
                proj_codes[p.stem] = json.loads(p.read_text())["properties"].get(
                    "proj:code"
                )
            except Exception:
                proj_codes[p.stem] = None

        before = json.loads(coll_path.read_text())
        after = sorted_collection(before, item_ids, proj_codes)
        if after == before:
            continue
        changed += 1
        print(f"  {project}")
        if not args.dry_run:
            coll_path.write_text(create_static_stac.dump_stac_json(after))

    verb = "would reorder" if args.dry_run else "reordered"
    print(f"\n{verb} {changed} of {scanned} collections")


if __name__ == "__main__":
    main()
