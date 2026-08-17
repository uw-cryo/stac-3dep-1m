#!/usr/bin/env python
"""Wrap each committed wesm:* collection summary in a one-element list.

STAC says a `summaries` value is a JSON Schema, a *set* (array of values), or a
Range object. Ours were bare scalars -- `"wesm:workunit_id": 218210` -- so every
collection.json failed validation on its first summary
("218210 is not valid under any of the given schemas"). The metadata was put in
summaries on purpose, because that is the block STAC Browser renders on the
collection page (Mar 2025, "render wesm metadata on collection page"), and the
comment left behind said so: "WARNING: this creates *invalid* STAC, be default
STAC-browser still renders it!". Validation was commented out in the same
commit, which is why nothing has complained since.

    "wesm:workunit_id": 218210      ->  "wesm:workunit_id": [218210]

A one-value set is legal STAC and still renders, so this keeps what the original
decision wanted and drops the invalidity. Verified both ways before writing:
the scalar form fails the collection schema, the wrapped form passes.

create_static_stac.wesm_summary_fields() now emits the wrapped form, so this
brings the already-committed collections to what a rebuild -- or a `refresh`
metadata repair -- would write. Without it the next refresh would see all 936
collections as drifted and rewrite them anyway, just in a diff mixed together
with real changes.

Values are rewritten in place, so key order is untouched and the diff is one
line per wesm field. Idempotent: a second run reports 0 changed. Nothing else in
the file is touched -- proj:code was already a list and is left alone.

Usage:
    pixi run wrap-wesm-summaries --dry-run
    pixi run wrap-wesm-summaries
"""

import argparse
import json
from pathlib import Path

import create_static_stac

CATALOG_DIR = Path("./catalog")
WESM_PREFIX = "wesm:"


def wrapped(collection):
    """The collection with every scalar wesm:* summary wrapped in a list.

    Returns a new dict; the caller decides whether anything changed. A value
    that is already a list is left exactly as it is, which is what makes a
    second run a no-op.
    """
    d = json.loads(json.dumps(collection))
    summaries = d.get("summaries") or {}
    for key, value in summaries.items():
        if key.startswith(WESM_PREFIX) and not isinstance(value, list):
            summaries[key] = [value]
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
    fields = 0
    for coll_path in sorted(CATALOG_DIR.glob("*/collection.json")):
        scanned += 1
        before = json.loads(coll_path.read_text())
        after = wrapped(before)
        if after == before:
            continue
        changed += 1
        fields += sum(
            1
            for k, v in (before.get("summaries") or {}).items()
            if k.startswith(WESM_PREFIX) and not isinstance(v, list)
        )
        if not args.dry_run:
            coll_path.write_text(create_static_stac.dump_stac_json(after))

    verb = "would wrap" if args.dry_run else "wrapped"
    print(f"{verb} {fields} summary values across {changed} of {scanned} collections")


if __name__ == "__main__":
    main()
