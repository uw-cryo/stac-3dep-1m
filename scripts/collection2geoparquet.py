#!/usr/bin/env python
"""
Create STAC-GeoParquet for a Static STAC Collection

https://cloudnativegeo.org/blog/2024/08/introduction-to-stac-geoparquet/

Usage:
python scripts/collection2geoparquet.py catalog/CO_WestCentral_2019_A19/collection.json CO_WestCentral_2019_A19.parquet
pixi run collection2geoparquet ./catalog/CO_WestCentral_2019_A19/collection.json ./CO_WestCentral_2019_A19.parquet
"""

import argparse
import asyncio
from pathlib import Path
import rustac
import pystac


async def collection_to_stac_geoparquet(
    collection_path, output_path=None, prune_links=False
):
    """
    Given path to local collection.json or catalog.json read all items & save as geoparquet
    """
    collection_path = str(Path(collection_path).resolve())
    c = pystac.read_file(collection_path)

    items = [i.to_dict() for i in c.get_items(recursive=True)]
    if not output_path:
        output_path = collection_path.replace(".json", ".parquet")
    output_path = str(Path(output_path).resolve())

    # Use geoparquet_writer to embed collection-level metadata in the parquet file
    async with rustac.geoparquet_writer(items, output_path) as w:
        if isinstance(c, pystac.Collection):
            collection_dict = c.to_dict()
            if prune_links:
                collection_dict.pop("links", None)
            w.add_collection(collection_dict)


def parse_args(args=None):
    parser = argparse.ArgumentParser(
        description="Create STAC-GeoParquet for a Static STAC Collection"
    )
    parser.add_argument(
        "collection_path",
        help="Path to a STAC collection.json or catalog.json",
    )
    parser.add_argument(
        "output_path",
        nargs="?",
        default=None,
        help="Output parquet file path (default: derived from collection_path)",
    )
    parser.add_argument(
        "--prune-links",
        action="store_true",
        help="Remove 'links' from collection metadata before writing to parquet",
    )
    return parser.parse_args(args)


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(
        collection_to_stac_geoparquet(
            args.collection_path, args.output_path, args.prune_links
        )
    )
