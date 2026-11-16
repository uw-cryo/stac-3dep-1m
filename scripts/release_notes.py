#!/usr/bin/env python
"""
Write the markdown body of a catalog release: what changed since the previous
release, and how much of CONUS the catalog now covers.

The collection lists come from git -- `catalog/` between the previous release
tag and the commit being released -- so they cost nothing and need no network.
Coverage comes from the release's catalog.parquet (and, for the change since
last time, the previous release's): a cell of the USGS 10 km grid counts as
covered when its center falls inside at least one tile footprint. The cells
that make up CONUS are data/conus_10km_cells.csv (see build_conus_grid.py).

Footprints are used rather than the x/y in tile names because names are not
uniform (`USGS_one_meter_x22y411_...` carries no UTM zone) and a tile is staged
in its project's zone, not always the zone its cell is in.

Usage (CI, before the tag exists):
pixi run release-notes --previous-ref v2026.10.01 --parquet catalog.parquet \
    --previous-parquet prev.parquet --tag v2026.11.01 -o release-notes.md

Retroactively, for an existing release:
pixi run release-notes --ref v2026.11.01 --previous-ref v2026.10.01 \
    --parquet v2026.11.01.parquet --previous-parquet v2026.10.01.parquet \
    --tag v2026.11.01
"""

import argparse
import csv
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq
import shapely

from build_conus_grid import DEFAULT_OUTPUT as CONUS_CELLS_CSV, cell_centers

GRID_URL = (
    "https://prd-tnm.s3.amazonaws.com/index.html?prefix="
    "StagedProducts/Elevation/1m/FullExtentSpatialMetadata/"
)
STAC_BROWSER = (
    "https://radiantearth.github.io/stac-browser/#/external/raw.githubusercontent.com"
)
STAC_MAP = "https://developmentseed.org/stac-map/?href="
KOPAH = "https://s3.kopah.uw.edu/tacolab/stac-3dep-1m"
# a section longer than this is folded into a <details> block
FOLD_ROWS = 25
# GitHub caps a release body at 125,000 characters, and --generate-notes
# appends the merged-PR list after ours. A catalog-wide sweep (every collection
# updated) overruns that, so past this budget links are dropped, then long
# sections are cut to SHORT_ROWS with a pointer to the full diff.
MAX_CHARS = 100_000
SHORT_ROWS = 200


def git(*args):
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, check=True
    ).stdout


def catalog_tree(ref):
    """{collection id: set of item file names} at `ref`.

    A directory counts as a collection only if it holds a collection.json, the
    same rule refresh_catalog.collection_dirs() follows. The root catalog.json
    and the "All Cataloged" meta collection sit directly in catalog/ and are
    not collections of tiles.
    """
    files = {}
    for path in git("ls-tree", "-r", "--name-only", ref, "--", "catalog/").split():
        parts = path.split("/")
        if len(parts) == 3:
            files.setdefault(parts[1], set()).add(parts[2])
    return {
        cid: names - {"collection.json"}
        for cid, names in files.items()
        if "collection.json" in names
    }


def read_blobs(specs):
    """Contents of `<ref>:<path>` blobs, one `git cat-file --batch` for all."""
    if not specs:
        return {}
    out = subprocess.run(
        ["git", "cat-file", "--batch"],
        input="\n".join(specs).encode() + b"\n",
        capture_output=True,
        check=True,
    ).stdout
    blobs, pos = {}, 0
    for spec in specs:
        header_end = out.index(b"\n", pos)
        header = out[pos:header_end].split()
        if header[-1] == b"missing":
            blobs[spec], pos = None, header_end + 1
            continue
        size = int(header[2])
        blobs[spec] = out[header_end + 1 : header_end + 1 + size]
        pos = header_end + 1 + size + 1
    return blobs


@dataclass
class Change:
    tiles: int = 0
    added: int = 0
    removed: int = 0
    modified: int = 0
    wesm: list = field(default_factory=list)  # wesm:* summary keys that changed
    collection: dict | None = None


def wesm_summaries(collection):
    """wesm:* summaries as plain values, so only a value change is a change.

    Before v2026.08.17 they were bare scalars; since then each is a one-element
    list (see wrap_wesm_summaries.py). Unwrap as create_static_stac.wesm_value()
    does, or that migration reads as every field of every collection changing.
    """
    summaries = (collection or {}).get("summaries", {})
    return {
        k: v[0] if isinstance(v, list) and len(v) == 1 else v
        for k, v in summaries.items()
        if k.startswith("wesm:")
    }


# git's empty tree: the baseline for a first release, where everything is new
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def catalog_changes(prev_ref, ref):
    before, after = catalog_tree(prev_ref), catalog_tree(ref)
    modified = {}
    diff = git("diff", "--name-status", "--no-renames", prev_ref, ref, "--", "catalog/")
    for line in diff.splitlines():
        status, path = line.split("\t", 1)
        parts = path.split("/")
        if status == "M" and len(parts) == 3 and parts[2] != "collection.json":
            modified[parts[1]] = modified.get(parts[1], 0) + 1
    touched = {
        line.split("\t", 1)[1].split("/")[1]
        for line in diff.splitlines()
        if line.split("\t", 1)[1].count("/") == 2
    }

    new = sorted(after.keys() - before.keys())
    removed = sorted(before.keys() - after.keys())
    updated = sorted((touched & after.keys() & before.keys()))

    specs = [f"{ref}:catalog/{c}/collection.json" for c in new + updated]
    specs += [f"{prev_ref}:catalog/{c}/collection.json" for c in updated + removed]
    blobs = {k: json.loads(v) for k, v in read_blobs(specs).items() if v}

    changes = {"new": {}, "updated": {}, "removed": {}}
    for c in new:
        changes["new"][c] = Change(
            tiles=len(after[c]),
            added=len(after[c]),
            collection=blobs.get(f"{ref}:catalog/{c}/collection.json"),
        )
    for c in updated:
        old = wesm_summaries(blobs.get(f"{prev_ref}:catalog/{c}/collection.json"))
        cur_collection = blobs.get(f"{ref}:catalog/{c}/collection.json")
        cur = wesm_summaries(cur_collection)
        changes["updated"][c] = Change(
            tiles=len(after[c]),
            added=len(after[c] - before[c]),
            removed=len(before[c] - after[c]),
            modified=modified.get(c, 0),
            wesm=sorted(k for k in old.keys() | cur.keys() if old.get(k) != cur.get(k)),
            collection=cur_collection,
        )
    for c in removed:
        changes["removed"][c] = Change(
            tiles=len(before[c]),
            removed=len(before[c]),
            collection=blobs.get(f"{prev_ref}:catalog/{c}/collection.json"),
        )
    totals = {
        "collections": (len(before), len(after)),
        "tiles": (
            sum(map(len, before.values())),
            sum(map(len, after.values())),
        ),
    }
    return changes, totals


def load_conus_cells(path=CONUS_CELLS_CSV):
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    zones = [int(r["utm_zone"]) for r in rows]
    xs = [int(r["name"][1:].split("y")[0]) for r in rows]
    ys = [int(r["name"].split("y")[1]) for r in rows]
    lon, lat = cell_centers(zones, xs, ys)
    return shapely.points(lon, lat)


def covered_cells(parquet_path, centers):
    """How many of the CONUS cell centers fall inside some tile footprint."""
    wkb = pq.read_table(parquet_path, columns=["geometry"]).column("geometry")
    footprints = shapely.from_wkb(wkb.to_numpy(zero_copy_only=False))
    hits = shapely.STRtree(footprints).query(centers, predicate="within")[0]
    return len(np.unique(hits))


def first(collection, key):
    v = (collection or {}).get("summaries", {}).get(key)
    return v[0] if isinstance(v, list) and v else v


def collected(collection):
    start, end = (
        first(collection, "wesm:collect_start"),
        first(collection, "wesm:collect_end"),
    )
    if not start:
        return ""
    start, end = start.replace("/", "-"), (end or "").replace("/", "-")
    return start if not end or end == start else f"{start} to {end}"


def link(cid, tag, repo, links=True):
    if not tag or not links:
        return f"`{cid}`"
    url = f"{STAC_BROWSER}/{repo}/refs/tags/{tag}/catalog/{cid}/collection.json"
    return f"[{cid}]({url})"


def describe(ch):
    parts = []
    if ch.added:
        parts.append(f"+{ch.added:,} tiles")
    if ch.removed:
        parts.append(f"-{ch.removed:,} tiles")
    if ch.modified:
        parts.append(f"{ch.modified:,} items updated")
    if ch.wesm:
        keys = ", ".join(k.removeprefix("wesm:") for k in ch.wesm[:4])
        more = f", +{len(ch.wesm) - 4} more" if len(ch.wesm) > 4 else ""
        parts.append(f"WESM metadata ({keys}{more})")
    return "; ".join(parts) or "collection metadata"


def table(header, rows):
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(map(str, r)) + " |" for r in rows]
    return "\n".join(lines)


def section(title, header, rows, max_rows=None, more=""):
    if not rows:
        return []
    shown = rows if max_rows is None else rows[:max_rows]
    body = table(header, shown)
    if len(shown) < len(rows):
        body += f"\n\n...and {len(rows) - len(shown):,} more{more}."
    if len(rows) > FOLD_ROWS:
        body = (
            f"<details><summary>Show all {len(rows)}</summary>\n\n{body}\n\n</details>"
        )
    return [f"### {title} ({len(rows)})", "", body, ""]


def delta(before, after, fmt="{:+,}"):
    if before is None:
        return ""
    return fmt.format(after - before)


def render(changes, totals, coverage, prev_ref, tag, repo, links=True, max_rows=None):
    n_cells, cov_before, cov_after = coverage
    (col_before, col_after), (tiles_before, tiles_after) = totals.values()
    pct = lambda n: None if n is None else 100 * n / n_cells  # noqa: E731

    out = []
    if tag:
        out += [
            f"Browse this release on [stac-map]({STAC_MAP}{KOPAH}/{tag}.parquet) "
            f"or [STAC Browser]({STAC_BROWSER}/{repo}/refs/tags/{tag}/catalog/catalog.json).",
            "",
        ]
    out += [
        "## Catalog summary",
        "",
        table(
            ["", "This release", f"Previous ({prev_ref or 'none'})", "Change"],
            [
                [
                    "Collections",
                    f"{col_after:,}",
                    f"{col_before:,}",
                    delta(col_before, col_after),
                ],
                [
                    "Tiles",
                    f"{tiles_after:,}",
                    f"{tiles_before:,}",
                    delta(tiles_before, tiles_after),
                ],
                [
                    "CONUS 10 km cells covered",
                    f"{cov_after:,} / {n_cells:,}",
                    "" if cov_before is None else f"{cov_before:,}",
                    delta(cov_before, cov_after),
                ],
                [
                    "CONUS coverage",
                    f"{pct(cov_after):.2f}%",
                    "" if cov_before is None else f"{pct(cov_before):.2f}%",
                    delta(pct(cov_before), pct(cov_after), "{:+.2f} pp"),
                ],
            ],
        ),
        "",
        f"<sub>Coverage: a cell of the [USGS 1 m 10 km grid]({GRID_URL}) inside "
        "CONUS counts as covered when its center falls inside at least one tile "
        "footprint. Tiles outside CONUS (Hawaii, Puerto Rico, USVI) count toward "
        "Tiles but not coverage.</sub>",
        "",
        "## Collection changes",
        "",
    ]
    if not any(changes.values()):
        out += ["No collection changed.", ""]
    more = ""
    if prev_ref and tag:
        more = f" (full diff: https://github.com/{repo}/compare/{prev_ref}...{tag})"
    out += section(
        "New collections",
        ["Collection", "Tiles", "Collected", "Quality level"],
        [
            [
                link(c, tag, repo, links),
                f"{ch.tiles:,}",
                collected(ch.collection),
                first(ch.collection, "wesm:ql") or "",
            ]
            for c, ch in changes["new"].items()
        ],
        max_rows,
        more,
    )
    out += section(
        "Updated collections",
        ["Collection", "Tiles", "Change"],
        [
            [link(c, tag, repo, links), f"{ch.tiles:,}", describe(ch)]
            for c, ch in changes["updated"].items()
        ],
        max_rows,
        more,
    )
    out += section(
        "Removed collections",
        ["Collection", "Tiles"],
        [[f"`{c}`", f"{ch.tiles:,}"] for c, ch in changes["removed"].items()],
        max_rows,
        more,
    )
    return "\n".join(out).rstrip() + "\n"


def main(args=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--ref", default="HEAD", help="commit being released (default HEAD)")
    p.add_argument(
        "--previous-ref",
        help="previous release tag (omit for a first release: every collection is new)",
    )
    p.add_argument("--parquet", required=True, help="this release's catalog.parquet")
    p.add_argument(
        "--previous-parquet",
        help="previous release's catalog.parquet; without it the coverage change is left blank",
    )
    p.add_argument("--tag", help="this release's tag, for links (omit for none)")
    p.add_argument(
        "--repo",
        default=os.environ.get("GITHUB_REPOSITORY", "uw-cryo/stac-3dep-1m"),
    )
    p.add_argument("-o", "--output", type=Path, help="write here instead of stdout")
    a = p.parse_args(args)

    changes, totals = catalog_changes(a.previous_ref or EMPTY_TREE, a.ref)
    centers = load_conus_cells()
    cov_after = covered_cells(a.parquet, centers)
    cov_before = None
    if a.previous_parquet and Path(a.previous_parquet).exists():
        cov_before = covered_cells(a.previous_parquet, centers)
    for links, max_rows in [(True, None), (False, None), (False, SHORT_ROWS)]:
        notes = render(
            changes,
            totals,
            (len(centers), cov_before, cov_after),
            a.previous_ref,
            a.tag,
            a.repo,
            links,
            max_rows,
        )
        if len(notes) <= MAX_CHARS:
            break
    if a.output:
        a.output.write_text(notes)
    else:
        sys.stdout.write(notes)


if __name__ == "__main__":
    main()
