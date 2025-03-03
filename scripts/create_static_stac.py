#!/usr/bin/env python
"""
Use TiTiler to generate a STAC Item Collection from Tiffs!

    We call titiler to actually generate the STAC items from reading the tiffs
    and then add WESM metadata to a STAC collection for a given project.

    The static catalog layout looks like this:

    CO_WestCentral_2019_A19
    ├── CO_WestCentral_2019_A19
    │   ├── USGS_1M_12_x76y426_CO_WestCentral_2019_A19.json
    │   ├── USGS_1M_12_x76y427_CO_WestCentral_2019_A19.json
    │   ├── USGS_1M_12_x76y428_CO_WestCentral_2019_A19.json
    │   └── collection.json
    └── catalog.json

    Catalog is self-contained with relative paths, so you can move it to different folders

Usage: python create_static_stac.py CO_WestCentral_2019_A19
"""

import sys
import asyncio
import json
import requests
import geopandas as gpd
import pandas as pd
from cloudpathlib import S3Client
from pystac import Provider, ProviderRole
import pystac
from pathlib import Path
import warnings

# explicitly instantiate a client that always uses the local cache
client = S3Client(local_cache_dir="/tmp", no_sign_request=True)

# Can change to whatever you want >=GDAL 3.10.1
ASSET_NAME = "elevation"


def get_titiler_datetime(series):
    start = pd.to_datetime(series.collect_start).isoformat() + "Z"
    end = pd.to_datetime(series.collect_end).isoformat() + "Z"
    datestr = f"{start}/{end}"
    # print(datestr)
    return datestr


# def add_wesm_metadata_to_properties(item, series):
#     '''add all WESM metadata with wesm: prefix to STAC properties inplace'''
#     item.properties.update(series.add_prefix('wesm:').to_dict())


def add_wesm_metadata_to_collection(collection, series):
    """Hijack "summaries" for WESM metadata rather than summaries of each item property"""
    links = ["lpc_link", "sourcedem_link", "metadata_link"]
    # not ideal: displays as a big block
    # collection.summaries.add('wesm', series.drop(links).to_dict())
    # NOTE: sure how to do this in one go, so loop and add each key
    # collection.summaries.update(series.drop(links).to_dict())
    for k, v in series.drop(links).to_dict().items():
        collection.summaries.add(k, str(v))


# TODO: use aiohttp instead?
# How to code async functions in a synchronous script...
def create_stac_item(URL, DATETIME):
    ID = URL.split("/")[-1][:-4]
    PROJECT = URL.split("/")[-3]
    stacify = f"https://titiler.xyz/cog/stac?id={ID}&datetime={DATETIME}&collection={PROJECT}&asset_name={ASSET_NAME}&asset_roles=data&with_eo=false&url={URL}"
    r = requests.get(stacify)
    # outpath = f'{outdir}/{ID}.json'
    # with open(outpath, 'w') as f:
    #    f.write(r.text)
    # return outpath
    return r.text


def get_tif_list_s3(s3path):
    with open(client.CloudPath(s3path)) as f:
        lines = [x.strip("\n") for x in f.readlines()]
        tifs = [x for x in lines if x.endswith(".tif")]
    return tifs


def get_wesm_series(project):
    wesm_csv = client.CloudPath(
        "s3://prd-tnm/StagedProducts/Elevation/metadata/WESM.csv"
    )
    df = pd.read_csv(wesm_csv)
    if project not in df.project.values:
        alternatives = df[df.project.str.startswith(project)].project.to_list()
        raise ValueError(
            f"Project '{project}' not found in WESM metadata, close matches: {alternatives}"
        )

    s = df[df.project == project].iloc[0]

    if not s.onemeter_category == 'Meets':
        warnings.warn(
            f"Project '{project}' onemeter_category is not 'Meets' but '{s.onemeter_category}'"
        )

    return s


def generate_stac_from_titiler(tiflist, DATETIME):
    # Neat: How to wrap a synchronous function to run asynchronously!
    # https://www.youtube.com/watch?v=p8tnmEdeOU0
    async def create_stac_item_async(url, datetime):
        response = await asyncio.to_thread(create_stac_item, url, datetime)
        return response

    # In jupyter there is already an asyncio event loop so can just run this!
    # extracted_metadata = await asyncio.gather(*[create_stac_item_async(url) for url in metadata_urls])
    # In script. Is there a better way to do this??
    # loop = asyncio.get_event_loop()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    task_group = asyncio.gather(
        *[create_stac_item_async(url, DATETIME) for url in tiflist]
    )
    # All the results as a list
    results = loop.run_until_complete(task_group)
    loop.close()
    return results


# TODO: can probably speed this up a lot just by taking min/max of bboxes
def get_collection_bbox(results):
    gf = gpd.GeoDataFrame.from_features([json.loads(x) for x in results])
    return gf.total_bounds.tolist()


def create_stac_catalog(project):
    """Create a STAC catalog from a 3DEP 1m project"""
    s = get_wesm_series(project)
    project_inventory = f"s3://prd-tnm/StagedProducts/Elevation/1m/Projects/{project}/0_file_download_links.txt"
    tifs = get_tif_list_s3(project_inventory)

    tiflist = tifs  # (for testing)

    print(f"creating STAC Items from {len(tiflist)} tifs...")
    # Same datatime range for all tifs in a project
    # Formatted for TITILER 2019-08-21T00:00:00Z/2019-09-19T00:00:00Z'
    DATETIME = get_titiler_datetime(s)
    results = generate_stac_from_titiler(tiflist, DATETIME)

    USGS_PROVIDER = Provider(
        name="USGS",
        description="USGS 3D Elevation Program",
        roles=[
            ProviderRole.PRODUCER,
            ProviderRole.PROCESSOR,
            ProviderRole.LICENSOR,
            ProviderRole.HOST,
        ],
        url="https://www.usgs.gov/3d-elevation-program",
    )

    TACOLAB_PROVIDER = Provider(
        name="UW-CRYO",
        description="UW Terrain Analysis and Cryosphere Observation Lab",
        roles=[
            ProviderRole.PROCESSOR,
        ],
        url="https://uw-cryo.github.io",
    )

    # Pystac.Items
    items = [pystac.read_dict(json.loads(x)) for x in results]

    # THis will be 'collection' level metadata, not in each item
    # [add_wesm_metadata_to_properties(item, s) for item in items]
    collection_bbox = get_collection_bbox(results)

    # extent either from metadata or geodataframe
    # Probably fastest to get it from WESM GPKG from fid lookup
    spatial_extent = pystac.SpatialExtent(bboxes=[collection_bbox])
    # should be python datetimes: AttributeError: 'str' object has no attribute 'tzinfo'
    temporal_extent = pystac.TemporalExtent(
        intervals=[(pd.to_datetime(s.collect_start), pd.to_datetime(s.collect_end))]
    )
    collection_extent = pystac.Extent(spatial=spatial_extent, temporal=temporal_extent)
    # TODO: summaries?
    # Thumbnail asset?
    collection = pystac.Collection(
        id=project,
        description="USGS 3DEP 1m tiles",
        providers=[USGS_PROVIDER, TACOLAB_PROVIDER],
        extent=collection_extent,
        # Seems either PDDL think this is correct https://resources.data.gov/open-licenses/ ?
        # https://github.com/radiantearth/stac-spec/blob/ec002bb93dbfa47976822def8f11b2861775b662/commons/common-metadata.md#licensing
        # license="CC0-1.0",
        # Or providers=[USGS_PROVIDER],
        # https://github.com/stactools-packages/threedep/blob/1948fd475cc73731703ecb9ed9f7f8d46066336c/src/stactools/threedep/commands.py#L18
        license="PDDL-1.0",
    )
    # Add WESM links to collection (assets?)
    # ? OPR (Original Product Resolution)
    sourcedem = pystac.Link(
        title="Original Product",
        rel="sourcedem",
        target=s.sourcedem_link,
    )
    lpcdem = pystac.Link(
        title="Lidar Point Cloud",
        rel="lpc",
        target=s.lpc_link,
    )
    metadata = pystac.Link(
        title="Metadata",
        rel="metadata",
        target=s.metadata_link,
    )
    # 3DEP License https://registry.opendata.aws/usgs-lidar/
    # https://resources.data.gov/open-licenses/
    # NOTE: add link to corresponding EPT on AWS?
    license = pystac.Link(
        title="USGS 3DEP License",
        rel="license",
        target="https://www.usgs.gov/faqs/what-are-terms-uselicensing-map-services-and-data-national-maps",
    )
    collection.add_links([sourcedem, lpcdem, metadata, license])

    collection.add_items(items)

    # Automatic summaries of Item propertes (e.g. proj:code)
    summaries = pystac.summaries.Summarizer().summarize(
        collection.get_items(recursive=True)
    )
    collection.summaries.update(summaries)

    # Add WESM metadata to collection summaries
    #add_wesm_metadata_to_collection(collection, s)

    # collection.validate()

    # Add to static catalog and save locally for stac-browser
    # TODO: add keywords (since static browsing allows filtering by 'title, description, keywords')!
    catalog = pystac.Catalog(id=project, description="USGS 3DEP 1m tiles")
    catalog.add_child(collection)
    strategy = pystac.layout.TemplateLayoutStrategy(
        item_template=""
    )  # flat layout (no subfolders per item)
    catalog.normalize_hrefs(
        f"./catalog/{project}", strategy=strategy
    )  # sets local path
    catalog.save(catalog_type=pystac.CatalogType.SELF_CONTAINED)


if __name__ == "__main__":
    project = sys.argv[1]  #'CO_WestCentral_2019_A19'
    project_path = Path(f"./catalog/{project}")
    if project_path.exists():
        print(f"Project folder '{project}' already exists.")
    else:
        create_stac_catalog(project)
