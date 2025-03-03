#!/usr/bin/env python
"""
Create STAC-GeoParquet for a STATIC Stac Collection
"""

import asyncio
import stacrs
import pystac
import sys


async def collection_to_stac_geoparquet(path):
    """
    Given path to local collection.json or catalog.json read all items & save as geoparquet
    """
    # TODO: add collection JSON to metadata
    # https://github.com/stac-utils/stacrs/discussions/67
    c = pystac.read_file(path)
    items = [i.to_dict() for i in c.get_items(recursive=True)]
    await stacrs.write(path.replace(".json", ".geoparquet"), items)


if __name__ == "__main__":
    collection_path = sys.argv[1]
    asyncio.run(collection_to_stac_geoparquet(collection_path))
