#!/usr/bin/env python
"""
Create STAC-GeoParquet for a Static STAC Catalog

https://github.com/stac-utils/rustac-py/discussions/67#discussioncomment-12381926

Usage:
python scripts/catalog2geoparquet.py catalog/ catalog.parquet
"""

import asyncio
import rustac
import sys


async def collection_to_stac_geoparquet(catalog_path, output_path=None):
    """
    Given path to catalog.json read all child items & save as STAC-geoparquet
    """
    catalog = await rustac.read(catalog_path)
    all_items = []
    async for _, _, items in rustac.walk(catalog):
        all_items.extend(items)

    if not output_path:
        output_path = catalog_path.replace(".json", ".parquet")

    # TODO: test different compressions (e.g. zstd)
    await rustac.write(output_path, all_items, format="parquet[snappy]")


if __name__ == "__main__":
    collection_path = sys.argv[1]
    output_path = sys.argv[2] if len(sys.argv) > 2 else None
    asyncio.run(collection_to_stac_geoparquet(collection_path, output_path))
