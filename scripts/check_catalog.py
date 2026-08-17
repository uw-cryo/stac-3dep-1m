#!/usr/bin/env python
"""Check the committed catalog against itself. No titiler, no S3, no tile reads.

The other two checkers both ask about the outside world. refresh_catalog.py asks
whether USGS still holds what we say it does; audit_catalog.py asks whether a
titiler rebuild would produce what we committed. Neither asks whether the
catalog agrees with *itself*, which is the free half of the problem and the half
that catches our bugs rather than USGS's (issue #20).

Everything is derived from the committed files, so a full pass is ~55 s and can
gate a release or a refresh PR:

    root/meta   catalog.json's children are exactly the collection directories,
                and the "All Cataloged" extent is what those collections imply
    links       each collection.json's rel="item" links are exactly the item
                files next to it, in the order a rebuild now produces
    items       id == filename stem == asset href stem, and the href names the
                item's own collection -- refresh_catalog.catalog_state() takes
                item identity from the filename, so an item whose href points at
                another project is invisible to every other check we have
    summaries   collection extent, proj:code and wesm:collect_* against the
                items they claim to summarize
    schema      one shape for every item, catalog-wide: a stray key or a
                different key order is what turns catalog.parquet into a mixed
                column schema
    format      every file byte-identical to json.dumps(indent=2) -- the
                stdlib-vs-orjson float repr pin (see CLAUDE.md)
    validate    sampled objects against the published STAC JSON schemas. The
                one check that reaches the network, and only for the schemas;
                offline it is skipped with a note rather than failing
    parquet     catalog.parquet row for row against the items it was walked
                from -- the published artifact GDAL's GTI driver and SlideRule
                build from, which nothing in the repo verified before. Runs
                only when --parquet names a file

Deliberately not checked: whether a tile exists, whether its bytes moved, or
whether its pixels match its geometry. Those need the network and already have
owners (`pixi run refresh`, `pixi run audit`).

Two documented divergences are reported as notes rather than findings, because
they are known and harmless: the 9 items with no raster:bands (titiler cannot
histogram a tile whose valid pixels span one float32 ULP -- see
docs/upstream-titiler-hist-issue.md), and 11 collections carrying WESM
lpc/sourcedem/metadata links from an older builder. A *tenth* raster-less item
is a finding: the exception list is explicit so a new one cannot hide in it.

Usage:
    pixi run check-catalog                        # everything but parquet
    pixi run check-catalog --project WA_KingCounty_2021_B21
    pixi run check-catalog --check schema --check format
    pixi run check-catalog --parquet catalog.parquet   # + the built artifact
    pixi run check-catalog --validate-sample 10   # deeper schema validation
    pixi run check-catalog --json report.json --markdown summary.md
"""

import argparse
import concurrent.futures
import json
import math
import os
import random
import re
import subprocess
import sys
import types
from collections import Counter
from pathlib import Path

import create_static_stac

CATALOG_DIR = Path("./catalog")
ROOT_CATALOG = CATALOG_DIR / "catalog.json"
META_COLLECTION = CATALOG_DIR / "collection.json"

# Mirrors create_static_stac.list_tiffs_in_project(): every asset href is this
# prefix plus <project>/TIFF/<item id>.tif, with no exceptions in 124,407 items.
# It is the GTI LocationField, so a wrong one breaks consumers, not just us.
ASSET_HREF_PREFIX = (
    "https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/1m/Projects/"
)

RASTER_EXT = "https://stac-extensions.github.io/raster/v1.1.0/schema.json"

# Items that carry no raster:bands at all, because titiler answers their tile
# with 500 "Too many bins for data range" and the with_raster=false fallback is
# all or nothing. All nine are perfectly flat water; the diagnosis and the
# upstream write-up are in docs/upstream-titiler-hist-issue.md. Listed rather
# than tolerated by count so that a tenth is a finding -- and so that one
# gaining bands (the tile being re-staged with varied pixels) shows up as a
# stale entry to delete.
KNOWN_NO_RASTER_BANDS = {
    "USGS_1M_18_x47y433_DE_Statewide_B23",
    "USGS_1M_19_x39y467_MA_CentralEastern_2021_B21",
    "USGS_1M_16_x69y515_MI_FEMA_2019_C19",
    "USGS_1M_16_x71y512_MI_FEMA_2019_C19",
    "USGS_1M_16_x71y515_MI_FEMA_2019_C19",
    "USGS_1M_17_x25y512_MI_FEMA_2019_C19",
    "USGS_1M_17_x26y515_MI_FEMA_2019_C19",
    "USGS_1M_17_x62y410_VA_West_Chesapeake_Bay_Watershed_Lidar_2017_B17",
    "USGS_1M_15_x56y492_WI_Statewide_2019_A19",
}

# Links every item carries, and nothing else: the flat self-contained layout
# means all three point at the collection.json sitting beside it.
ITEM_LINKS = [
    ("root", "./collection.json", "application/json"),
    ("collection", "./collection.json", "application/json"),
    ("parent", "./collection.json", "application/json"),
]

# Coordinate agreement is checked at 1 ULP, matching audit_catalog: these are
# the same numbers written by two paths (bbox vs geometry, collection bbox vs
# the union of its items), so they agree exactly today, but a rebuild is
# entitled to the last bit.
REL_TOL = 1e-12
ABS_TOL = 1e-9

# proj:transform origin vs proj:bbox is a metre-scale comparison of UTM
# coordinates near 1e6, where 1 ULP is ~1e-10 m; the span check multiplies a gsd
# that is itself only 1.0 to ~1e-13, so it needs room a coordinate compare does
# not.
SPAN_ABS_TOL = 1e-6

CHECKS = (
    ("root", "root catalog.json children against the collection directories"),
    ("meta", "the All Cataloged collection's extent against the collections"),
    ("links", "each collection.json's item links against the items on disk"),
    ("items", "each item's identity, asset href, geometry and file metadata"),
    ("summaries", "each collection's extent and summaries against its items"),
    ("schema", "one item shape and one collection shape, catalog-wide"),
    ("format", "every file byte-identical to json.dumps(indent=2)"),
    ("validate", "sampled objects against the published STAC JSON schemas"),
    ("parquet", "catalog.parquet row for row against the item files"),
)
CHECK_NAMES = [name for name, _ in CHECKS]

# `parquet` has nothing to check without one, and building it is a separate
# ~4-minute job, so it runs only when --parquet names a file.
ON_DEMAND_CHECKS = {"parquet"}
DEFAULT_CHECKS = [name for name in CHECK_NAMES if name not in ON_DEMAND_CHECKS]

# A STAC summary value must be a JSON Schema, a set, or a Range object. The
# wesm:* block held bare scalars until 2026-08, which made every collection fail
# validation; each value is now a one-element list (scripts/wrap_wesm_summaries.py)
# and collections validate as committed. The list-ness is itself checked below,
# so a scalar creeping back is a finding rather than a slow reversion.
WESM_PREFIX = "wesm:"

# Findings printed per check before the rest are left to the JSON report.
EXAMPLES = 10

THREADS = 8


def finding(check, message, collection=None, item=None):
    return {
        "check": check,
        "collection": collection,
        "item": item,
        "message": message,
    }


def close(a, b, abs_tol=ABS_TOL):
    return isinstance(a, (int, float)) and math.isclose(
        a, b, rel_tol=REL_TOL, abs_tol=abs_tol
    )


def bounds(geometry):
    """[minx, miny, maxx, maxy] of a Polygon's rings."""
    coords = [c for ring in geometry["coordinates"] for c in ring]
    xs = [c[0] for c in coords]
    ys = [c[1] for c in coords]
    return [min(xs), min(ys), max(xs), max(ys)]


def wesm_interval(summaries):
    """(start, end) ISO strings for a collection's snapshotted WESM dates.

    Through the builder's own converter, so a formatting difference cannot
    masquerade as drift -- the same rule refresh_catalog.py follows when it
    compares live WESM against these summaries (issue #18). The converter
    returns "<start>/<end>" and both halves are ISO by then, so the separator is
    unambiguous.
    """
    row = types.SimpleNamespace(
        collect_start=create_static_stac.wesm_value(summaries, "wesm:collect_start"),
        collect_end=create_static_stac.wesm_value(summaries, "wesm:collect_end"),
    )
    return create_static_stac.get_titiler_datetime(row).split("/")


# --------------------------------------------------------------- file loading


def tracked_paths(untracked=False):
    """{project: [item paths]} for each collection, from git by default.

    Only tracked files count (CLAUDE.md), because the committed tree *is* the
    published product: a locally built collection that update_root_catalog.py
    has not linked yet is work in progress, not a broken catalog, and reporting
    it every run would train the reader to ignore the report.

    --untracked walks the filesystem instead, and is the right mode when the
    question is about the working tree rather than the commit -- CI passes it
    after `refresh` has mutated catalog/ but before anything is committed, where
    `git ls-files` would still list the pruned items and miss the new ones. It is
    also the fallback outside a git work tree.

    Either way a directory without a collection.json is not a collection, the
    same rule refresh_catalog.collection_dirs() follows: create_collection_parquets.py
    leaves 11 such directories here, holding a .parquet and nothing else.
    """
    if not untracked:
        try:
            out = subprocess.run(
                ["git", "ls-files", "-z", "--", str(CATALOG_DIR)],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
        except (subprocess.CalledProcessError, FileNotFoundError):
            untracked = True
        else:
            items = {}
            for name in out.split("\0"):
                parts = name.split("/")
                if len(parts) != 3 or not parts[2].endswith(".json"):
                    continue
                items.setdefault(parts[1], [])
                if parts[2] != "collection.json":
                    items[parts[1]].append(Path(name))
            return {p: sorted(v) for p, v in items.items()}

    return {
        d.name: sorted(p for p in d.glob("*.json") if p.name != "collection.json")
        for d in sorted(CATALOG_DIR.iterdir())
        if d.is_dir() and (d / "collection.json").is_file()
    }


def load(path, check_format):
    """(dict, finding) for one JSON file. Exactly one of the two is None.

    The format check rides along with the read because the raw text is already
    in hand: pystac picks its serializer by what happens to be importable, and
    orjson's float repr differs from the stdlib's, so an environment that
    reformats a file it merely touched is a diff on every item it touched.
    """
    try:
        raw = path.read_text()
    except OSError as e:
        return None, finding("root", f"{path}: unreadable ({e})")
    try:
        d = json.loads(raw)
    except json.JSONDecodeError as e:
        return None, finding("root", f"{path}: invalid JSON ({e})")
    if check_format and create_static_stac.dump_stac_json(d) != raw:
        return d, finding(
            "format",
            f"{path}: not byte-identical to json.dumps(indent=2) -- an orjson "
            "environment reformatted it",
        )
    return d, None


# --------------------------------------------------------------------- items


def item_signature(item, asset):
    """The item's shape: every key, in order, minus the raster block.

    Membership *and* order, because the catalog is written by several paths
    (titiler, reuse, backfill, in-place WESM repair) and a key landing in a
    different slot rewrites the file on the next rebuild for no reason.
    raster:bands is excluded here and checked per item instead: nine items
    legitimately lack it, and one absence must not split the whole catalog into
    two schemas.
    """
    return (
        item.get("stac_version"),
        tuple(item),
        tuple(item.get("properties", {})),
        tuple(item.get("assets", {})),
        tuple(k for k in asset if k != "raster:bands"),
        tuple(e for e in item.get("stac_extensions", []) if e != RASTER_EXT),
    )


def check_item(path, project, item, checks):
    """Findings for one item, and the facts its collection needs from it."""
    out = []
    item_id = path.stem

    def add(msg):
        out.append(finding("items", msg, collection=project, item=item_id))

    if "items" in checks:
        if item.get("id") != item_id:
            add(f"id is {item.get('id')!r}, filename says {item_id!r}")
        if item.get("collection") != project:
            add(f"collection field is {item.get('collection')!r}, lives in {project!r}")
        if item.get("type") != "Feature":
            add(f"type is {item.get('type')!r}, not Feature")

        links = [
            (x.get("rel"), x.get("href"), x.get("type")) for x in item.get("links", [])
        ]
        if links != ITEM_LINKS:
            add(f"links are {links}, expected {ITEM_LINKS}")

    asset = (item.get("assets") or {}).get(create_static_stac.ASSET_NAME)
    if asset is None:
        if "items" in checks:
            add(f"no {create_static_stac.ASSET_NAME!r} asset")
        return out, None

    if "items" in checks:
        want = f"{ASSET_HREF_PREFIX}{project}/TIFF/{item_id}.tif"
        if asset.get("href") != want:
            add(f"asset href is {asset.get('href')!r}, expected {want!r}")

        geometry = item.get("geometry") or {}
        bbox = item.get("bbox") or []
        if geometry.get("type") != "Polygon" or len(bbox) != 4:
            add(f"geometry is {geometry.get('type')!r} with a {len(bbox)}-value bbox")
        else:
            if not all(close(a, b) for a, b in zip(bbox, bounds(geometry))):
                add(f"bbox {bbox} is not the bounds of its geometry {bounds(geometry)}")
            if not (
                -180 <= bbox[0] < bbox[2] <= 180 and -90 <= bbox[1] < bbox[3] <= 90
            ):
                add(f"bbox {bbox} is not degrees, or is inverted")

        out += check_item_projection(item, project, item_id)
        out += check_item_file_metadata(asset, project, item_id)

        bands = asset.get("raster:bands")
        if not bands and item_id not in KNOWN_NO_RASTER_BANDS:
            add("no raster:bands (a rebuild would carry them; see issue #17 notes)")
        if bool(bands) != (RASTER_EXT in item.get("stac_extensions", [])):
            add(
                f"raster:bands present={bool(bands)} but raster extension declared="
                f"{RASTER_EXT in item.get('stac_extensions', [])}"
            )
        out += check_item_statistics(bands or [], project, item_id)

    properties = item.get("properties") or {}
    facts = {
        "bbox": item.get("bbox"),
        "start": properties.get("start_datetime"),
        "end": properties.get("end_datetime"),
        "code": properties.get("proj:code"),
        "signature": item_signature(item, asset),
        "no_bands": not asset.get("raster:bands"),
    }
    return out, facts


def check_item_projection(item, project, item_id):
    """proj:* against itself: shape, transform and bbox describe one raster."""
    out = []
    p = item.get("properties") or {}

    def add(msg):
        out.append(finding("items", msg, collection=project, item=item_id))

    code = p.get("proj:code")
    # normalize_projection() pops proj:epsg and sets proj:code, but a CRS with
    # no EPSG code leaves the item claiming projection v2.0.0 while carrying no
    # code at all -- unreachable with today's 13 NAD83 UTM zones, silent if not
    if not isinstance(code, str) or not re.fullmatch(r"EPSG:\d+", code):
        add(f"proj:code is {code!r}, expected EPSG:<n>")

    shape, transform, pbox = (
        p.get("proj:shape"),
        p.get("proj:transform"),
        p.get("proj:bbox"),
    )
    geometry = p.get("proj:geometry")
    if not (
        isinstance(shape, list)
        and len(shape) == 2
        and all(isinstance(n, int) and n > 0 for n in shape)
    ):
        add(f"proj:shape is {shape!r}")
        return out
    if not (isinstance(pbox, list) and len(pbox) == 4) or not (
        isinstance(transform, list) and len(transform) >= 6
    ):
        add(f"proj:bbox is {pbox!r} with proj:transform {transform!r}")
        return out

    height, width = shape
    if not (close(transform[2], pbox[0]) and close(transform[5], pbox[3])):
        add(
            f"proj:transform origin ({transform[2]}, {transform[5]}) is not the "
            f"upper-left of proj:bbox {pbox}"
        )
    if not close(
        pbox[0] + width * transform[0], pbox[2], abs_tol=SPAN_ABS_TOL
    ) or not close(pbox[3] + height * transform[4], pbox[1], abs_tol=SPAN_ABS_TOL):
        add(
            f"proj:shape {shape} at {transform[0]} m does not span proj:bbox {pbox} "
            "-- the item advertises a footprint its own pixel grid cannot cover"
        )
    if isinstance(geometry, dict) and geometry.get("coordinates"):
        if not all(close(a, b) for a, b in zip(pbox, bounds(geometry))):
            add(f"proj:bbox {pbox} is not the bounds of proj:geometry")
    else:
        add(f"proj:geometry is {geometry!r}")
    return out


def check_item_file_metadata(asset, project, item_id):
    """file:size / file:checksum well-formed. Their *values* need S3 (refresh)."""
    out = []
    size = asset.get("file:size")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        out.append(
            finding("items", f"file:size is {size!r}", collection=project, item=item_id)
        )
    checksum = asset.get("file:checksum")
    prefix = create_static_stac.CRC64NVME_MULTIHASH_PREFIX
    if not isinstance(checksum, str) or not re.fullmatch(
        rf"{prefix}[0-9a-f]{{16}}", checksum
    ):
        out.append(
            finding(
                "items",
                f"file:checksum is {checksum!r}, expected {prefix} + 16 hex digits",
                collection=project,
                item=item_id,
            )
        )
    return out


def check_item_statistics(bands, project, item_id):
    """Band statistics that contradict themselves.

    The tolerance is not decoration: mean is accumulated in float64 over float32
    pixels, and on 213 flat-water items it lands ~2.6e-7 above a maximum that
    equals the minimum. That is float32 resolution, not a wrong number.
    """
    out = []
    for i, band in enumerate(bands):
        stats = band.get("statistics") or {}
        if not stats:
            continue
        lo, hi, mean = stats.get("minimum"), stats.get("maximum"), stats.get("mean")
        if None in (lo, hi, mean):
            continue
        if lo > hi or not (
            lo - abs(lo) * 1e-6 - 1e-6 <= mean <= hi + abs(hi) * 1e-6 + 1e-6
        ):
            out.append(
                finding(
                    "items",
                    f"band {i} statistics: mean {mean} outside [{lo}, {hi}]",
                    collection=project,
                    item=item_id,
                )
            )
        valid = stats.get("valid_percent")
        if valid is not None and not 0 <= valid <= 100:
            out.append(
                finding(
                    "items",
                    f"band {i} valid_percent is {valid}",
                    collection=project,
                    item=item_id,
                )
            )
    return out


# ----------------------------------------------------------- schema validation
#
# The one check that reaches the network -- for the published JSON schemas
# themselves, never for a tile. A handful of URLs, fetched once and cached in
# the validator for the rest of the run. Unreachable schemas are a note and a
# skip, not findings: being offline says nothing about the catalog.

_validator = None
_validator_error = None


def stac_validator():
    """The shared pystac validator, or None if its schemas cannot be fetched."""
    global _validator, _validator_error
    if _validator is not None or _validator_error is not None:
        return _validator
    try:
        from pystac.validation.stac_validator import JsonSchemaSTACValidator

        validator = JsonSchemaSTACValidator()
        # Warmed on an *extension* schema, not a core one: pystac bundles the 13
        # core v1.1.0 schemas, so probing item.json would succeed offline and
        # every item would then fail its extension fetch one at a time. This is
        # the fetch that actually needs the network, and doing it here rather
        # than inside the thread pool makes an outage one note instead of 934.
        validator._get_schema(create_static_stac.FILE_EXT)
        _validator = validator
    except Exception as e:  # noqa: BLE001 - offline, DNS, a 503: all the same here
        _validator_error = f"{type(e).__name__}: {str(e).splitlines()[0][:120]}"
    return _validator


def validate_object(obj, object_type, collection=None, item=None):
    """Findings from validating one STAC dict against its schemas."""
    import pystac
    import pystac.errors

    validator = stac_validator()
    if validator is None:
        return []
    try:
        validator.validate(
            obj,
            object_type,
            obj.get("stac_version", pystac.get_stac_version()),
            obj.get("stac_extensions", []),
            None,
        )
    except pystac.errors.STACValidationError as e:
        return [finding("validate", schema_error(e), collection=collection, item=item)]
    except pystac.errors.GetSchemaError:
        # the network dropped mid-run: a schema we cannot read says nothing
        # about the object, so this is silence, not a finding
        return []
    except Exception as e:  # noqa: BLE001 - never lose a long run to one object
        return [
            finding(
                "validate",
                f"could not be validated: {type(e).__name__}: {str(e)[:120]}",
                collection=collection,
                item=item,
            )
        ]
    return []


def schema_error(error):
    """A STACValidationError as one line: where it failed, and why."""
    source = getattr(error, "source", None)
    if isinstance(source, (list, tuple)):
        source = source[0] if source else None
    if source is None or not hasattr(source, "message"):
        return f"fails its schema: {str(error).splitlines()[0][:200]}"
    source = most_specific(source)
    where = "/".join(str(p) for p in source.absolute_path) or "(root)"
    return f"fails its schema at {where}: {source.message[:200]}"


def most_specific(error):
    """The deepest sub-error of a jsonschema failure.

    An extension schema is a pile of anyOf/allOf, so the top-level error is
    reported at the root with the entire item inlined in its message ("{'type':
    'Feature', 'stac_version': ... } is not valid under any of the given
    schemas"). The sub-error underneath says what actually went wrong -- "'big'
    is not of type 'integer'" -- which is the line worth printing.
    """
    best = error
    for sub in getattr(error, "context", None) or []:
        candidate = most_specific(sub)
        if (len(candidate.absolute_path), -len(candidate.message)) > (
            len(best.absolute_path),
            -len(best.message),
        ):
            best = candidate
    return best


def sampled(project, item_paths, sample, seed):
    """Item stems to validate in one collection.

    Seeded per collection rather than drawn from one stream, so a collection
    samples the same items however many others are being checked alongside it
    -- the same rule audit_catalog.plan_project follows, and what makes a
    reported finding reproducible.
    """
    if sample <= 0 or not item_paths:
        return set()
    if sample >= len(item_paths):
        return {p.stem for p in item_paths}
    chosen = random.Random(f"{seed}:{project}").sample(item_paths, sample)
    return {p.stem for p in chosen}


# ---------------------------------------------------------------- collections


def check_collection(project, item_paths, checks, opts):
    """Every finding derivable from one collection directory.

    Returns (findings, notes, facts) -- facts being what the catalog-wide root,
    meta, schema and parquet checks need from this collection, so the whole tree
    is read once.
    """
    findings, notes = [], []
    coll_path = CATALOG_DIR / project / "collection.json"
    collection, problem = load(coll_path, "format" in checks)
    if problem:
        findings.append(dict(problem, collection=project))
    if collection is None:
        return findings, notes, None

    to_validate = (
        sampled(project, item_paths, opts.validate_sample, opts.seed)
        if "validate" in checks
        else set()
    )
    item_facts = {}
    records = {}
    for path in item_paths:
        item, problem = load(path, "format" in checks)
        if problem:
            findings.append(dict(problem, collection=project, item=path.stem))
        if item is None:
            continue
        item_findings, facts = check_item(path, project, item, checks)
        findings += item_findings
        if facts:
            item_facts[path.stem] = facts
        if path.stem in to_validate:
            import pystac

            findings += validate_object(
                item, pystac.STACObjectType.ITEM, collection=project, item=path.stem
            )
        if "parquet" in checks:
            records[path.stem] = parquet_record(item)

    if "links" in checks:
        findings += check_collection_links(project, collection, item_paths)
    if "summaries" in checks:
        findings += check_collection_summaries(project, collection, item_facts)
    if "validate" in checks:
        import pystac

        findings += validate_object(
            collection, pystac.STACObjectType.COLLECTION, collection=project
        )

    stale = KNOWN_NO_RASTER_BANDS & {
        i for i, f in item_facts.items() if not f["no_bands"]
    }
    for item_id in sorted(stale):
        notes.append(
            f"`{item_id}` now carries raster:bands -- drop it from "
            "KNOWN_NO_RASTER_BANDS in scripts/check_catalog.py"
        )
    extra_rels = sorted(
        {
            link.get("rel")
            for link in collection.get("links", [])
            if link.get("rel") not in ("root", "self", "parent", "item", "license")
        }
    )
    if extra_rels:
        notes.append(
            f"`{project}` carries {', '.join(extra_rels)} links from an older "
            "builder; a rebuild drops them (harmless)"
        )

    facts = {
        "n_items": len(item_facts),
        "bbox": collection.get("extent", {}).get("spatial", {}).get("bbox", [None])[0],
        "interval": collection.get("extent", {})
        .get("temporal", {})
        .get("interval", [[None, None]])[0],
        "signature": (
            collection.get("stac_version"),
            tuple(collection),
            tuple(collection.get("summaries", {})),
        ),
        "item_signatures": Counter(f["signature"] for f in item_facts.values()),
        "item_signature_example": {
            f["signature"]: item_id for item_id, f in item_facts.items()
        },
        "records": records,
    }
    return findings, notes, facts


def check_collection_links(project, collection, item_paths):
    """rel="item" links against the item files, in both directions."""
    out = []

    def add(msg):
        out.append(finding("links", msg, collection=project))

    if collection.get("id") != project:
        add(f"collection id is {collection.get('id')!r}, directory is {project!r}")
    if collection.get("type") != "Collection":
        add(f"type is {collection.get('type')!r}, not Collection")

    links = collection.get("links", [])
    rels = [link.get("rel") for link in links]
    for required in ("root", "license"):
        if required not in rels:
            add(f"no rel={required!r} link")

    hrefs = [link["href"] for link in links if link.get("rel") == "item"]
    on_disk = [p.stem for p in item_paths]

    duplicates = [h for h, n in Counter(hrefs).items() if n > 1]
    if duplicates:
        add(f"{len(duplicates)} duplicated item links, e.g. {duplicates[0]}")

    malformed = [h for h in hrefs if not re.fullmatch(r"\./[^/]+\.json", h)]
    if malformed:
        add(f"{len(malformed)} item links are not ./<id>.json, e.g. {malformed[0]!r}")

    linked = {h[2:-5] for h in hrefs if h.startswith("./") and h.endswith(".json")}
    missing = sorted(linked - set(on_disk))
    unlinked = sorted(set(on_disk) - linked)
    if missing:
        add(
            f"{len(missing)} items linked but not on disk: "
            f"{', '.join(missing[:5])}{' …' if len(missing) > 5 else ''}"
        )
    if unlinked:
        add(
            f"{len(unlinked)} items on disk but not linked: "
            f"{', '.join(unlinked[:5])}{' …' if len(unlinked) > 5 else ''}"
        )
    if not on_disk:
        add("no items at all")

    # rebuild order: create_stac_catalog() sorts items by id before add_items(),
    # so anything else means the next rebuild rewrites every link in the file
    # for no reason (sort_collection_links.py brought the catalog to this)
    if (
        not missing
        and not unlinked
        and hrefs != [f"./{i}.json" for i in sorted(on_disk)]
    ):
        add(
            "item links are not in sorted-by-id order, so a rebuild would reshuffle them"
        )
    return out


def check_collection_summaries(project, collection, item_facts):
    """Extent, proj:code and the WESM dates against the items themselves."""
    out = []

    def add(msg):
        out.append(finding("summaries", msg, collection=project))

    summaries = collection.get("summaries") or {}

    # STAC summary values are sets, not scalars. `validate` would also catch a
    # bare scalar, but only as a schema error 30 lines deep; this says what is
    # wrong and how to fix it. Ahead of the item-count guard below, because it
    # is a statement about the collection alone.
    unwrapped = sorted(
        k
        for k, v in summaries.items()
        if k.startswith(WESM_PREFIX) and (not isinstance(v, list) or len(v) != 1)
    )
    if unwrapped:
        add(
            f"{len(unwrapped)} wesm:* summaries are not one-element lists "
            f"({', '.join(unwrapped[:5])}) -- run `pixi run wrap-wesm-summaries`"
        )

    if not item_facts:
        return out

    bboxes = collection.get("extent", {}).get("spatial", {}).get("bbox") or []
    if len(bboxes) != 1:
        add(f"spatial extent has {len(bboxes)} bboxes, expected 1")
    else:
        item_bboxes = [f["bbox"] for f in item_facts.values() if f["bbox"]]
        union = [
            min(b[0] for b in item_bboxes),
            min(b[1] for b in item_bboxes),
            max(b[2] for b in item_bboxes),
            max(b[3] for b in item_bboxes),
        ]
        if not all(close(a, b) for a, b in zip(bboxes[0], union)):
            add(f"extent bbox {bboxes[0]} is not the union of its items {union}")

    intervals = collection.get("extent", {}).get("temporal", {}).get("interval") or []
    if len(intervals) != 1 or len(intervals[0]) != 2:
        add(f"temporal extent is {intervals!r}")
    else:
        try:
            wesm_start, wesm_end = wesm_interval(summaries)
        except Exception as e:  # noqa: BLE001 - a bad date is the finding
            add(
                f"wesm:collect_start/collect_end "
                f"({summaries.get('wesm:collect_start')!r}, "
                f"{summaries.get('wesm:collect_end')!r}) do not parse: {e}"
            )
        else:
            if list(intervals[0]) != [wesm_start, wesm_end]:
                add(
                    f"temporal extent {intervals[0]} does not match the snapshotted "
                    f"WESM dates [{wesm_start}, {wesm_end}]"
                )
            # every item's datetimes come from the same WESM row, and a partial
            # in-place metadata repair (issue #18) is exactly what splits them
            starts = {f["start"] for f in item_facts.values()}
            ends = {f["end"] for f in item_facts.values()}
            if starts != {wesm_start} or ends != {wesm_end}:
                add(
                    f"item datetimes {sorted(starts)[:3]}/{sorted(ends)[:3]} do not "
                    f"all match the WESM dates [{wesm_start}, {wesm_end}]"
                )

    # pystac's Summarizer walks the item links in order, so proj:code is
    # first-seen over the sorted items -- not sorted, and not a set
    seen = []
    for item_id in sorted(item_facts):
        code = item_facts[item_id]["code"]
        if code is not None and code not in seen:
            seen.append(code)
    if summaries.get("proj:code") != seen:
        add(f"proj:code summary is {summaries.get('proj:code')}, items give {seen}")
    return out


# -------------------------------------------------------------- catalog-wide


def check_root(projects):
    """catalog.json's children are exactly the collections on disk, plus meta."""
    out = []

    def add(msg):
        out.append(finding("root", msg))

    catalog, problem = load(ROOT_CATALOG, False)
    if problem:
        return [problem]
    if catalog.get("type") != "Catalog":
        add(f"{ROOT_CATALOG} type is {catalog.get('type')!r}, not Catalog")

    links = catalog.get("links", [])
    if [link["href"] for link in links if link.get("rel") == "root"] != [
        "./catalog.json"
    ]:
        add("no rel=root self link ./catalog.json")

    hrefs = [link["href"] for link in links if link.get("rel") == "child"]
    duplicates = [h for h, n in Counter(hrefs).items() if n > 1]
    if duplicates:
        add(f"{len(duplicates)} duplicated child links, e.g. {duplicates[0]}")

    if "./collection.json" not in hrefs:
        add("the All Cataloged meta collection is not linked as a child")
    for href in hrefs:
        if not (CATALOG_DIR / href[2:]).is_file():
            add(f"child link {href} points at nothing")

    linked = {h[2 : -len("/collection.json")] for h in hrefs if h.count("/") == 2}
    missing = sorted(linked - set(projects))
    unlinked = sorted(set(projects) - linked)
    if missing:
        add(f"{len(missing)} children with no directory: {', '.join(missing[:5])}")
    if unlinked:
        # the failure mode `pixi run update-root-catalog` exists to fix: a built
        # collection nothing links to is invisible to every catalog walker,
        # including catalog2geoparquet
        add(
            f"{len(unlinked)} collections not linked from the root catalog: "
            f"{', '.join(unlinked[:5])}{' …' if len(unlinked) > 5 else ''}"
        )
    return out


def check_meta(facts):
    """The All Cataloged extent is what update_root_catalog would write today."""
    out = []

    def add(msg):
        out.append(finding("meta", msg))

    meta, problem = load(META_COLLECTION, False)
    if problem:
        return [problem]

    projects = sorted(facts)
    bboxes = meta.get("extent", {}).get("spatial", {}).get("bbox") or []
    want = [facts[p]["bbox"] for p in projects]
    overall = [
        min(b[0] for b in want),
        min(b[1] for b in want),
        max(b[2] for b in want),
        max(b[3] for b in want),
    ]
    if len(bboxes) != len(want) + 1:
        add(
            f"extent has {len(bboxes)} bboxes, expected {len(want) + 1} "
            "(one overall, then one per collection) -- rerun `pixi run update-root-catalog`"
        )
    else:
        if not all(close(a, b) for a, b in zip(bboxes[0], overall)):
            add(
                f"overall bbox {bboxes[0]} is not the union of the collections {overall}"
            )
        stale = [p for p, b in zip(projects, bboxes[1:]) if b != facts[p]["bbox"]]
        if stale:
            add(
                f"{len(stale)} per-collection bboxes disagree with their collection: "
                f"{', '.join(stale[:5])}"
            )

    times = [t for p in projects for t in facts[p]["interval"]]
    interval = meta.get("extent", {}).get("temporal", {}).get("interval") or [[]]
    if list(interval[0]) != [min(times), max(times)]:
        add(
            f"temporal extent {interval[0]} is not the range across the collections "
            f"[{min(times)}, {max(times)}]"
        )
    return out


def check_schema(facts):
    """One item shape and one collection shape across the whole catalog.

    Anything that writes items has to keep all ~124k consistent, or the parquet
    build produces a mixed column schema. The majority shape is taken as the
    intended one and every other is reported against it, which is the same
    question a reviewer asks: what makes these few different?
    """
    out = []
    item_shapes = Counter()
    examples = {}
    for project, f in facts.items():
        item_shapes.update(f["item_signatures"])
        for signature, item_id in f["item_signature_example"].items():
            examples.setdefault(signature, (project, item_id))
    if item_shapes:
        (canonical, n), *rest = item_shapes.most_common()
        for signature, count in rest:
            project, item_id = examples[signature]
            out.append(
                finding(
                    "schema",
                    f"{count} items differ in shape from the other {n}: "
                    f"{shape_diff(canonical, signature)}",
                    collection=project,
                    item=item_id,
                )
            )

    coll_shapes = Counter(f["signature"] for f in facts.values())
    if coll_shapes:
        (canonical, n), *rest = coll_shapes.most_common()
        for signature, count in rest:
            project = next(p for p, f in facts.items() if f["signature"] == signature)
            out.append(
                finding(
                    "schema",
                    f"{count} collections differ in shape from the other {n}: "
                    f"{shape_diff(canonical, signature)}",
                    collection=project,
                )
            )
    return out


# ------------------------------------------------------------------- parquet
#
# catalog.parquet is the published artifact -- GDAL's GTI driver and SlideRule
# build from it, and stac-map renders it -- but it is gitignored and rebuilt in
# CI, so nothing in the repo has ever verified that it says the same thing as
# the items it was walked from. release.yml asserted the row count and stopped
# there, which cannot see a row whose href or footprint drifted.

# Fields carried through verbatim: an inequality here is a bug in the
# aggregation, not a rounding difference.
PARQUET_EXACT = (
    ("collection", "collection"),
    ("assets.elevation.href", "href"),
    ("assets.elevation.file:size", "file:size"),
    ("assets.elevation.file:checksum", "file:checksum"),
    ("proj:code", "proj:code"),
    ("proj:shape", "proj:shape"),
    ("start_datetime", "start_datetime"),
    ("end_datetime", "end_datetime"),
    ("datetime", "datetime"),
)


def parquet_record(item):
    """The fields catalog.parquet is expected to carry over from one item."""
    p = item.get("properties") or {}
    asset = (item.get("assets") or {}).get(create_static_stac.ASSET_NAME) or {}
    return {
        "collection": item.get("collection"),
        "href": asset.get("href"),
        "file:size": asset.get("file:size"),
        "file:checksum": asset.get("file:checksum"),
        "proj:code": p.get("proj:code"),
        "proj:shape": p.get("proj:shape"),
        "start_datetime": p.get("start_datetime"),
        "end_datetime": p.get("end_datetime"),
        "datetime": p.get("datetime"),
        "bbox": item.get("bbox"),
        "geometry": (item.get("geometry") or {}).get("coordinates"),
    }


def iso(value):
    """A parquet timestamp as the item spells it, or None."""
    if value is None:
        return None
    return value.isoformat().replace("+00:00", "Z")


def check_parquet(path, records, partial):
    """catalog.parquet against the item files, row for row.

    Coordinates are compared at the same 1 ULP as everywhere else, and they need
    it: rustac's JSON float parse and Python's disagree in the last bit on ~47%
    of coordinates (an item reading -119.49674045532203 lands in the parquet as
    -119.49674045532204). Everything in PARQUET_EXACT does compare exactly.

    `partial` means only some collections were selected, so the rows for the
    others are expected to be there and the both-directions id check is skipped.
    """
    import pyarrow.compute as pc
    import pyarrow.parquet as pq
    import shapely

    out = []

    def add(msg, item=None):
        out.append(finding("parquet", msg, item=item))

    table = pq.read_table(path)
    ids = table["id"].to_pylist()

    duplicates = [i for i, n in Counter(ids).items() if n > 1]
    if duplicates:
        add(f"{len(duplicates)} ids appear on more than one row, e.g. {duplicates[0]}")

    if not partial:
        if len(ids) != len(records):
            add(
                f"{len(ids):,} rows against {len(records):,} item files -- "
                "the parquet was built from a different catalog"
            )
        missing = sorted(set(records) - set(ids))
        extra = sorted(set(ids) - set(records))
        if missing:
            add(
                f"{len(missing)} items have no row: "
                f"{', '.join(missing[:5])}{' …' if len(missing) > 5 else ''}"
            )
        if extra:
            add(
                f"{len(extra)} rows have no item file: "
                f"{', '.join(extra[:5])}{' …' if len(extra) > 5 else ''}"
            )

    assets = table["assets"].combine_chunks()
    elevation = pc.struct_field(assets, [create_static_stac.ASSET_NAME])
    columns = {
        "collection": table["collection"].to_pylist(),
        "href": pc.struct_field(elevation, ["href"]).to_pylist(),
        "file:size": pc.struct_field(elevation, ["file:size"]).to_pylist(),
        "file:checksum": pc.struct_field(elevation, ["file:checksum"]).to_pylist(),
        "proj:code": table["proj:code"].to_pylist(),
        "proj:shape": table["proj:shape"].to_pylist(),
        "start_datetime": [iso(v) for v in table["start_datetime"].to_pylist()],
        "end_datetime": [iso(v) for v in table["end_datetime"].to_pylist()],
        "datetime": [iso(v) for v in table["datetime"].to_pylist()],
    }
    bbox = table["bbox"].combine_chunks()
    corners = [
        pc.struct_field(bbox, [name]).to_pylist()
        for name in ("xmin", "ymin", "xmax", "ymax")
    ]
    geometries = shapely.from_wkb(table["geometry"].to_pylist())

    disagree = Counter()
    example = {}
    for row, item_id in enumerate(ids):
        want = records.get(item_id)
        if want is None:
            continue  # already reported above, or out of a --project subset
        for label, key in PARQUET_EXACT:
            if columns[key][row] != want[key]:
                disagree[label] += 1
                example.setdefault(label, (item_id, columns[key][row], want[key]))
        got_bbox = [corner[row] for corner in corners]
        if want["bbox"] and not all(
            close(a, b) for a, b in zip(got_bbox, want["bbox"])
        ):
            disagree["bbox"] += 1
            example.setdefault("bbox", (item_id, got_bbox, want["bbox"]))
        if want["geometry"]:
            differs = ring_diff(geometries[row], want["geometry"])
            if differs:
                disagree["geometry"] += 1
                example.setdefault("geometry", (item_id, *differs))

    for label, n in disagree.most_common():
        item_id, got, expected = example[label]
        add(
            f"{n:,} rows disagree with their item on {label}, e.g. {got!r} "
            f"against {expected!r}",
            item=item_id,
        )
    return out


def ring_diff(geometry, coordinates):
    """(parquet point, item point) where they first differ beyond 1 ULP.

    None when the parquet geometry holds the item's coordinates.
    """
    import shapely

    got = shapely.get_coordinates(geometry).tolist()
    want = [list(c) for ring in coordinates for c in ring]
    if len(got) != len(want):
        return (f"{len(got)} coordinates", f"{len(want)} coordinates")
    for point, target in zip(got, want):
        if not all(close(a, b) for a, b in zip(point, target)):
            return (point, target)
    return None


def shape_diff(canonical, other):
    """Which part of a signature moved, in words."""
    parts = []
    for want, got in zip(canonical, other):
        if want == got:
            continue
        if isinstance(want, tuple) and isinstance(got, tuple):
            added = [k for k in got if k not in want]
            dropped = [k for k in want if k not in got]
            if added or dropped:
                bits = []
                if added:
                    bits.append(f"extra {added}")
                if dropped:
                    bits.append(f"missing {dropped}")
                parts.append(", ".join(bits))
            else:
                parts.append(f"same keys in a different order: {got} vs {want}")
        else:
            parts.append(f"{got!r} vs {want!r}")
    return "; ".join(parts) or "identical"


# ------------------------------------------------------------------ reporting


def plural(n, noun):
    return f"{n:,} {noun}{'' if n == 1 else 's'}"


def render_summary(report):
    """The whole report as markdown, for the job summary and --markdown."""
    findings, notes = report["findings"], report["notes"]
    out = ["# Catalog consistency\n"]
    counts = Counter(f["check"] for f in findings)

    if findings:
        out.append(
            f"**{plural(len(findings), 'finding')}** across "
            f"{plural(len(counts), 'check')}, over "
            f"{report['collections']:,} collections / {report['items']:,} items. "
            "Every one is derivable from the committed files alone — no tile was "
            "read, so these are our bugs, not USGS's.\n"
        )
    else:
        out.append(
            f"**Clean.** {report['collections']:,} collections / "
            f"{report['items']:,} items agree with themselves on every check "
            f"({', '.join(report['checks'])}).\n"
        )
    if report["scope"] != "the whole catalog":
        out.append(f"Scope: {report['scope']}.\n")

    if findings:
        out.append("\n## By check\n")
        out.append("| Check | Findings | What it means |")
        out.append("| --- | ---: | --- |")
        for name, description in CHECKS:
            if counts.get(name):
                out.append(f"| `{name}` | {counts[name]} | {description} |")

        out.append("\n## Findings\n")
        out.append("| Check | Collection | Item | Problem |")
        out.append("| --- | --- | --- | --- |")
        for f in findings[:200]:
            item = f"`{f['item']}`" if f["item"] else ""
            collection = f"`{f['collection']}`" if f["collection"] else ""
            # a message carrying a pipe would otherwise split the row
            message = f["message"].replace("|", "\\|")
            out.append(f"| `{f['check']}` | {collection} | {item} | {message} |")
        if len(findings) > 200:
            out.append(f"\n… and {len(findings) - 200} more, in the JSON report.")

    if notes:
        out.append(f"\n## Notes ({len(notes)})\n")
        out.append("Known and harmless; listed so they stay deliberate.\n")
        for note in notes[:50]:
            out.append(f"- {note}")
        if len(notes) > 50:
            out.append(f"\n… and {len(notes) - 50} more.")

    return "\n".join(out) + "\n"


def write_step_summary(report):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as f:
            f.write(render_summary(report))


def print_findings(findings):
    """Findings grouped by check, capped, in CHECKS order."""
    by_check = {}
    for f in findings:
        by_check.setdefault(f["check"], []).append(f)
    for name, _ in CHECKS:
        group = by_check.get(name)
        if not group:
            continue
        print(f"\n{name}: {len(group)} findings")
        for f in group[:EXAMPLES]:
            where = " ".join(x for x in (f["collection"], f["item"]) if x)
            print(f"  {where}: {f['message']}" if where else f"  {f['message']}")
        if len(group) > EXAMPLES:
            print(f"  … and {len(group) - EXAMPLES} more")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--project",
        action="append",
        metavar="PROJECT",
        help="check only this collection (repeatable); root/meta are skipped, "
        "since they are questions about the catalog as a whole",
    )
    ap.add_argument(
        "--check",
        action="append",
        choices=CHECK_NAMES,
        help="run only this check (repeatable). Default: "
        f"{', '.join(DEFAULT_CHECKS)} ({', '.join(sorted(ON_DEMAND_CHECKS))} "
        "needs --parquet)",
    )
    ap.add_argument(
        "--parquet",
        type=Path,
        metavar="PATH",
        help="also check this STAC-GeoParquet against the item files it was "
        "built from (row for row); enables the `parquet` check",
    )
    ap.add_argument(
        "--validate-sample",
        type=int,
        default=1,
        metavar="N",
        help="items per collection validated against the STAC JSON schemas "
        "(default 1; every collection, the root catalog and the meta collection "
        "are always validated). 0 skips items. ~11 ms each, and the `schema` "
        "check already proves one item shape catalog-wide",
    )
    ap.add_argument(
        "--seed",
        type=int,
        default=0,
        help="seed for the validation sample, so a finding can be reproduced",
    )
    ap.add_argument(
        "--untracked",
        action="store_true",
        help="check every collection directory on disk, not just git-tracked "
        "files (default is tracked only, so local experiments do not report)",
    )
    ap.add_argument(
        "--threads",
        type=int,
        default=THREADS,
        help=f"reader threads (default {THREADS})",
    )
    ap.add_argument("--json", type=Path, help="write the full report here")
    ap.add_argument(
        "--markdown",
        type=Path,
        metavar="PATH",
        help="write the summary here (the same rendering the job summary gets)",
    )
    ap.add_argument(
        "--exit-zero",
        action="store_true",
        help="report findings but exit 0 (default is exit 1 on any finding)",
    )
    args = ap.parse_args()

    if not CATALOG_DIR.is_dir():
        sys.exit("run from the repository root (catalog/ not found)")

    checks = set(args.check or DEFAULT_CHECKS)
    if args.parquet:
        if not args.parquet.is_file():
            sys.exit(f"no such parquet: {args.parquet}")
        checks.add("parquet")
    elif "parquet" in checks:
        sys.exit("--check parquet needs --parquet <path> to check against")

    items_by_project = tracked_paths(args.untracked)
    if args.project:
        missing = sorted(set(args.project) - set(items_by_project))
        if missing:
            sys.exit(f"not in catalog/: {', '.join(missing)}")
        items_by_project = {p: items_by_project[p] for p in args.project}
        # root and meta are statements about every collection at once; answering
        # them from a subset would report the other 933 as missing
        checks -= {"root", "meta"}

    scope = (
        "the whole catalog"
        if not args.project
        else f"{len(items_by_project)} named collection(s)"
    )
    n_items = sum(len(v) for v in items_by_project.values())
    print(
        f"checking {len(items_by_project)} collections / {n_items:,} items "
        f"({', '.join(sorted(checks))})",
        flush=True,
    )

    findings, notes, facts = [], [], {}
    opts = types.SimpleNamespace(
        validate_sample=args.validate_sample if "validate" in checks else 0,
        seed=args.seed,
    )
    # built before the pool so an unreachable schema is one note, not 934
    if "validate" in checks and stac_validator() is None:
        checks.discard("validate")
        opts.validate_sample = 0
        notes.append(
            f"schema validation skipped: the STAC schemas could not be fetched "
            f"({_validator_error}). Every other check is offline"
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.threads) as ex:
        results = ex.map(
            lambda p: check_collection(p, items_by_project[p], checks, opts),
            sorted(items_by_project),
        )
        for project, (found, noted, fact) in zip(sorted(items_by_project), results):
            findings += found
            notes += noted
            if fact:
                facts[project] = fact

    if "root" in checks:
        findings += check_root(set(items_by_project))
    if "meta" in checks and facts:
        findings += check_meta(facts)
    if "schema" in checks:
        findings += check_schema(facts)
    if "validate" in checks:
        import pystac

        for path, object_type in (
            (ROOT_CATALOG, pystac.STACObjectType.CATALOG),
            (META_COLLECTION, pystac.STACObjectType.COLLECTION),
        ):
            obj, problem = load(path, False)
            if obj is not None:
                findings += validate_object(obj, object_type, collection=path.name)
    if "parquet" in checks:
        records = {
            item_id: record
            for fact in facts.values()
            for item_id, record in fact["records"].items()
        }
        findings += check_parquet(args.parquet, records, partial=bool(args.project))

    order = {name: i for i, (name, _) in enumerate(CHECKS)}
    findings.sort(
        key=lambda f: (order[f["check"]], f["collection"] or "", f["item"] or "")
    )

    report = {
        "collections": len(items_by_project),
        "items": n_items,
        "checks": sorted(checks),
        "scope": scope,
        "findings": findings,
        "notes": sorted(notes),
    }
    print_findings(findings)
    print(f"\ncollections checked: {report['collections']}")
    print(f"items checked:       {report['items']:,}")
    print(f"findings:            {len(findings)}")
    print(f"notes:               {len(notes)} (known, not failures)")
    write_step_summary(report)
    if args.json:
        args.json.write_text(json.dumps(report, indent=2))
        print(f"report written to {args.json}")
    if args.markdown:
        args.markdown.write_text(render_summary(report))
        print(f"summary written to {args.markdown}")
    if findings and not args.exit_zero:
        sys.exit(1)


if __name__ == "__main__":
    main()
