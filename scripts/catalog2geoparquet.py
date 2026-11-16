#!/usr/bin/env python
"""
Create STAC-GeoParquet for a Static STAC Catalog

https://github.com/stac-utils/rustac-py/discussions/67#discussioncomment-12381926

Every collection that items in the file belong to is embedded in the parquet
footer under the `stac-geoparquet` key's `collections` map (stac-geoparquet
1.1, rustac-py #208), so a reader can list collections -- with titles, extents
and wesm:* summaries -- without a second request to the static catalog.

Usage:
python scripts/catalog2geoparquet.py catalog/catalog.json catalog.parquet
"""

import asyncio
import json
import sys
from pathlib import Path

import rustac


def embedded_collection(path):
    """A collection.json as it is embedded in the parquet footer.

    `links` is dropped: a collection's links are one rel="item" per tile
    (thousands for the large ones) and relative hrefs that mean nothing once the
    collection is outside the catalog tree. Without them all ~950 collections
    are ~1.6 MB of footer; with them, tens of MB read on every open.

    Read with the stdlib rather than taken from rustac.walk so the floats in
    the extent are the ones committed: rustac's JSON parse differs from
    Python's in the last bit on some values (see check_catalog.check_parquet).
    """
    with open(path) as f:
        collection = json.load(f)
    collection.pop("links", None)
    return collection


async def catalog_to_stac_geoparquet(catalog_path, output_path=None):
    """
    Given path to catalog.json read all child items & save as STAC-geoparquet
    """
    catalog = await rustac.read(catalog_path)
    all_items = []
    async for _, _, items in rustac.walk(catalog):
        all_items.extend(items)

    if not output_path:
        output_path = catalog_path.replace(".json", ".parquet")

    # Only collections some item names: the root's "All Cataloged" meta
    # collection has no items and describes none of the rows. The layout is
    # flat (catalog/<id>/collection.json), which update_root_catalog and
    # check_catalog rely on too.
    catalog_dir = Path(catalog_path).parent
    ids = sorted({item["collection"] for item in all_items})
    missing = [i for i in ids if not (catalog_dir / i / "collection.json").is_file()]
    if missing:
        sys.exit(f"items name collections with no collection.json: {missing[:5]}")

    # zstd is the writer's default, matching the v0.3 release assets
    async with rustac.geoparquet_writer(all_items, output_path) as w:
        for collection_id in ids:
            w.add_collection(
                embedded_collection(catalog_dir / collection_id / "collection.json")
            )
    print(f"{len(all_items):,} items, {len(ids)} collections -> {output_path}")


if __name__ == "__main__":
    # rustac resolves relative child hrefs against the input path, so it must
    # be absolute (a relative path resolves them against the filesystem root)
    catalog_path = str(Path(sys.argv[1]).resolve())
    output_path = sys.argv[2] if len(sys.argv) > 2 else None
    asyncio.run(catalog_to_stac_geoparquet(catalog_path, output_path))
