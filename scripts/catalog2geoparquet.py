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
from pathlib import Path


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

    # zstd to match the v0.3 release assets
    await rustac.write(output_path, all_items, format="parquet[zstd]")


if __name__ == "__main__":
    # rustac resolves relative child hrefs against the input path, so it must
    # be absolute (a relative path resolves them against the filesystem root)
    collection_path = str(Path(sys.argv[1]).resolve())
    output_path = sys.argv[2] if len(sys.argv) > 2 else None
    asyncio.run(collection_to_stac_geoparquet(collection_path, output_path))
