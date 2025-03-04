#!/usr/bin/env python
"""
Keep top-level catalog up-to date with subcatalogs
"""

import pathlib
import pystac
from itertools import chain


def get_subcollections(base_path="./catalog"):
    base_path = pathlib.Path(base_path)
    return [pystac.read_file(path) for path in base_path.rglob("./*/collection.json")]


def update_root_catalog(base_path="./catalog"):
    path = f'{base_path}/catalog.json'
    cat = pystac.read_file(path)
    subcats = get_subcollections(base_path)
    existing = [subcat.id for subcat in list(cat.get_children())]
    new = [x for x in subcats if x.id not in existing]
    cat.add_children(new)
    cat.save_object(include_self_link=False, dest_href=path)
    print(f'Updated {path}')


def create_root_collection(base_path="./catalog"):
    ''' render all processed bboxes on a map'''
    output = f'{base_path}/collection.json'
    collections = get_subcollections(base_path)
    bboxes = [c.extent.spatial.bboxes[0] for c in collections]
    intervals = [c.extent.temporal.intervals[0] for c in collections]
    times = list(chain(*intervals))

    # https://github.com/radiantearth/stac-spec/blob/master/collection-spec/collection-spec.md#spatial-extent-object
    # 1st element needs to be overall bbox
    overall_bbox = [
        min([bbox[0] for bbox in bboxes]),
        min([bbox[1] for bbox in bboxes]),
        max([bbox[2] for bbox in bboxes]),
        max([bbox[3] for bbox in bboxes]),
    ]
    bboxes.insert(0, overall_bbox)

    spatial_extents = pystac.SpatialExtent(bboxes=bboxes)
    temporal_extent = pystac.TemporalExtent(
        intervals=[min(times), max(times)]
    )
    collection_extent = pystac.Extent(spatial=spatial_extents, temporal=temporal_extent)

    collection = pystac.Collection(
        id='All Cataloged',
        description="Meta collection of all 1m DEM STAC collections in this catalog.",
        providers=collections[0].providers,
        extent=collection_extent,
        license=collections[0].license,
    )
    collection.save_object(include_self_link=False, dest_href=output)
    print(f'Updated {output}')

if __name__ == "__main__":
    update_root_catalog()
    create_root_collection()
