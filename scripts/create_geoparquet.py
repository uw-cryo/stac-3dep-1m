#!/usr/bin/env python
"""
Create STAC-GeoParquet for a Static STAC Collection

https://cloudnativegeo.org/blog/2024/08/introduction-to-stac-geoparquet/

Usage:
python scripts/create_geoparquet.py catalog/CO_WestCentral_2019_A19/collection.json CO_WestCentral_2019_A19.parquet
pixi run create-geoparquet catalog/CO_WestCentral_2019_A19/collection.json CO_WestCentral_2019_A19.parquet
"""

import asyncio
import stacrs
import pystac
import sys


async def collection_to_stac_geoparquet(collection_path, output_path=None):
    """
    Given path to local collection.json or catalog.json read all items & save as geoparquet
    """
    # TODO: add collection JSON to metadata
    # https://github.com/stac-utils/stacrs/discussions/67

    # TODO: compression
    # https://github.com/stac-utils/stacrs/issues/74
    c = pystac.read_file(collection_path)
    items = [i.to_dict() for i in c.get_items(recursive=True)]
    if not output_path:
        output_path = collection_path.replace(".json", ".parquet")
    # TODO: test different compressions (e.g. zstd)
    await stacrs.write(output_path, items, format="parquet[snappy]")


if __name__ == "__main__":
    collection_path = sys.argv[1]
    output_path = sys.argv[2] if len(sys.argv) > 2 else None
    asyncio.run(collection_to_stac_geoparquet(collection_path, output_path))
