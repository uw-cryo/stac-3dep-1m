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
import argparse
import time
import boto3
from botocore import UNSIGNED
from botocore.client import Config
s3 = boto3.client('s3', config=Config(signature_version=UNSIGNED))

# explicitly instantiate a client that always uses the local cache
client = S3Client(local_cache_dir="/tmp", no_sign_request=True)

# Can change to whatever you want >=GDAL 3.10.1
ASSET_NAME = "elevation"

## 1m folder to WESM metadata mapping for non-conforming naming schemes
# If commented out unsure for some reason or another
onemeter_folder_to_wesm = {
    "AR_BentonCoBAA_2015": "AR_Benton_Co_2015",
    "BLev8MI_31Co_Mason_2017": "MI_31Co_Mason_2017",
    "CA_Eastern_SanDiegoCo_2016": "CA_E_SanDiegoCo_2016",
    #"CA_SanDiegoCo_2016": "CA_SanDiego_2015_C17_1",  # not sure, different year?
    #'CO_MesaCo_QL2_UTM12_2016' : 'CO_MesaCo_QL2_UTM13_2015' # not sure, different UTM
    "Elwha_River_LiDAR_2014_MOD2": "WA_ElwhaRiver_2014",
    "IL_12_County_HenryCO_2009": "IL_12_County_HenryCo_2009",
    "IL_KankakeeCo_2014": "IL_5County_KankakeeCo_2014",
    "IL_McHenryCo_2008": "IL_Twelve_Counties_McHenry",
    "IL_StephensonCo_2009": "IL_12_County_Stephenson_Co_2009",
    #"KS_South_Central_AOI_2_Manhattan_2015": "KS_SCentral_L2_2015",  # not sure
    "KS_SthCentral_AOI4_2015": "KS_SCentral_L4_2015",  # not sure
    "LA_Atchafalaya2_2012": "LA_ATCHAFALAYA2_2013",
    #"MA_NE_CMGP_Sandy_Z19_2013": "MA_NE_CMGP_Sandy_Z18_2013",  # or 'MA_NE_CMGP_Sandy_Z19_A3_2015' ?!
    "MA_NE_CMGP_Snds_2013": "MA_NE_CMGP_Sandy_Z18_2013",
    "ME_SouthernArea_2012": "ME_SouthernAreas_2012",
    "MI_13Co_Emmett_2015": "MI_EmmetCo_2015",
    "MI_25County_AlleganCo_2015": "MI_AlleganCo_2015",
    #"MI_Montcalm_2016": "MI_Montcalm_2015",  # NOTE: why is 2015 in name if collection in 2018/10/24?
    "MO_BooneCo_2014": "MO_BooneCo_2015",
    "MO_SaintLouis_2017": "MO_StLouis_2017",
    "MS_NRCS_Lauderdale_2013": "MS_NRCS_LAUDERDALE_2013",
    "MS_TishomingoNorth_2016": "MS_Tishomingo_North_2016",
    "MS_TishomingoSouth_2016": "MS_Tishomingo_South_2016",
    #"NH_CT_RiverNorthL6_2015": "NH_CT_RiverNorthL6_P1_2015",  # or 'NH_CT_RiverNorthL6_P2_2015',
    "NH_CT_River_North_L6_p3_2015": "NH_CT_River_North_L6_P3_2015",
    "NY_Southwest_2_Co_2016": "NY_Southwest_2_CO_2016",
    "OR_Wallowa_2015": "OR_OLC_Wallowa_2015",
    "SD_NRCS_Fugro_B2_TL_2017": "SD_NRCS_Fugro_B2_2017",
    #"TN_NRCS_2011": "TN_NRCS_L2_2011_12",  # or 'TN_NRCS_L1_2011_12' ?,
    #"UT_Area1WasatchFault_2013": "UT_WasatchFault_L2_2013",
    "UT_MonroeMountain_2016": "UT_MonroeMoutain_2016",
    "UT_Wasatch_L3_2013": "UT_WasatchFault_L3_2013",
}


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
    """Add to extra_fields dictionary"""
    links = ["lpc_link", "sourcedem_link", "metadata_link"]
    # Ensure JSON-serializable
    wesm_properties = json.loads(series.drop(links).add_prefix("wesm:").to_json())
    # STAC-browser does not render this
    # collection.extra_fields = {'properties':wesm_properties}
    # WARNING: this creates *invalid* STAC, be default STAC-browser still renders it!
    for k, v in wesm_properties.items():
        collection.summaries.add(k, v)


# NOTE: TiTiler now rate limited :(
# https://github.com/developmentseed/titiler/discussions/1223
# Should deploy our own version anyways

# TODO: use aiohttp instead?
# How to code async functions in a synchronous script...
def create_stac_item(URL, DATETIME):
    ID = URL.split("/")[-1][:-4]
    PROJECT = URL.split("/")[-3]
    params = {
        "id": ID,
        "datetime": DATETIME,
        "collection": PROJECT,
        "asset_name": ASSET_NAME,
        "asset_roles": "data",
        "with_eo": "false",
        "url": URL,
    }
    #stacify = f"https://titiler.xyz/cog/stac"
    stacify = "https://xpohtuqdoyg4w7ze7loqenojje0earua.lambda-url.us-west-2.on.aws/cog/stac"

    # Test:
    # https://xpohtuqdoyg4w7ze7loqenojje0earua.lambda-url.us-west-2.on.aws/cog/stac?id=USGS_1M_16_x24y472_WI_Statewide_2019_A19&datetime=2019-04-08T00:00:00Z/2019-04-08T00:00:00Z&collection=WI_Statewide_2019_A19&asset_name=elevation&asset_roles=data&with_eo=false&url=https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/1m/Projects/WI_Statewide_2019_A19/TIFF/USGS_1M_16_x24y472_WI_Statewide_2019_A19.tif

    #print(f"Requesting STAC item for {ID}")
    r = requests.get(stacify, params=params)

    # Omit raster stats if it fails (or omit item entirely?)
    if r.status_code != 200:
        # Debug:
        print(r.url)
        print(r.status_code)
        print(r.text)
        if r.json().get("detail", "").startswith("Too many bins for data range"):
            params["with_raster"] = "false"
            r = requests.get(stacify, params=params)

    return r.text


def get_tif_list_s3(s3path):
    with open(client.CloudPath(s3path)) as f:
        lines = [x.strip("\n") for x in f.readlines()]
        tifs = [x for x in lines if x.endswith(".tif")]
    return tifs


def get_wesm_series(project, is_workunit=False):
    wesm_csv = client.CloudPath(
        "s3://prd-tnm/StagedProducts/Elevation/metadata/WESM.csv"
    )
    df = pd.read_csv(wesm_csv)

    # Fast track manually specified mappings
    if project in onemeter_folder_to_wesm:
        name = onemeter_folder_to_wesm[project]
        s = df[df.workunit == name].iloc[0]
    else:
        if is_workunit:
            if project not in df.workunit.values:
                alternatives = df[df.workunit.str.startswith(project)].workunit.to_list()
                raise ValueError(
                    f"Workunit '{project}' not found in WESM metadata, close matches: {alternatives}"
                )
            s = df[df.workunit == project].iloc[0]
        else:
            if project not in df.project.values:
                alternatives = df[df.project.str.startswith(project)].project.to_list()
                raise ValueError(
                    f"Project '{project}' not found in WESM metadata, close matches: {alternatives}"
                )
            s = df[df.project == project].iloc[0]

    if not s.onemeter_category == "Meets":
        warnings.warn(
            f"Project '{project}' onemeter_category is not 'Meets' but '{s.onemeter_category}'"
        )

    return s


def generate_stac_from_titiler(tiflist, DATETIME):
    # TODO: compare to running rio-stac locally.
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

    # Break tiflist into batches of no more than 100 URLs
    def batch_urls(urls, batch_size=100):
        for i in range(0, len(urls), batch_size):
            yield urls[i:i + batch_size]

    # waiting on quota increase... from 10 to 2000 :)
    results = []
    for batch in batch_urls(tiflist, batch_size=10):
        task_group = asyncio.gather(
            *[create_stac_item_async(url, DATETIME) for url in batch]
        )
        batch_results = loop.run_until_complete(task_group)
        results.extend(batch_results)

    # NOTE: this results in {"Reason":"ConcurrentInvocationLimitExceeded","Type":"User","message":"Rate Exceeded."}
    # task_group = asyncio.gather(
    #     *[create_stac_item_async(url, DATETIME) for url in tiflist]
    # )
    # # All the results as a list
    # results = loop.run_until_complete(task_group)
    loop.close()

    return results


# TODO: can probably speed this up a lot just by taking min/max of bboxes
def get_collection_bbox(results):
    gf = gpd.GeoDataFrame.from_features([json.loads(x) for x in results])
    return gf.total_bounds.tolist()


def list_tiffs_in_project(project, bucket="prd-tnm"):
    project_prefix = f"StagedProducts/Elevation/1m/Projects/{project}/TIFF"
    response = s3.list_objects_v2(Bucket=bucket, Prefix=project_prefix)
    tiff_files = [
        obj['Key'] for obj in response.get('Contents', [])
        if obj['Key'].endswith('.tif')
    ]

    return [f"https://{bucket}.s3.amazonaws.com/{key}" for key in tiff_files]


def create_stac_catalog(project, is_workunit=False):
    """Create a STAC catalog from a 3DEP 1m project"""
    s = get_wesm_series(project, is_workunit=is_workunit)

    # NOTE: not all folders have 0_file_download_links.txt
    # or 0_file_download_links.txt has errors
    #project_inventory = f"s3://prd-tnm/StagedProducts/Elevation/1m/Projects/{project}/0_file_download_links.txt"
    #tiflist = get_tif_list_s3(project_inventory)

    # Instead list TIFF/ folder directly
    tiflist = list_tiffs_in_project(project)
    if len(tiflist) == 0:
        raise ValueError(f"No tiffs found for project '{project}'")

    print(f"creating STAC Items from {len(tiflist)} tifs...")
    # Same datatime range for all tifs in a project
    # Formatted for TITILER 2019-08-21T00:00:00Z/2019-09-19T00:00:00Z'
    DATETIME = get_titiler_datetime(s)

    # ASYNC
    results = generate_stac_from_titiler(tiflist, DATETIME)

    # SYNC
    # results = []
    # for url in tiflist:
    #     results.append(create_stac_item(url, DATETIME))
    # #     time.sleep(0.1)

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
    # Debug: do loop in serial to see where we're failing (or use function...)
    # items = []
    # for x in results:
    #     print(x)
    #     item = pystac.read_dict(json.loads(x))
    #     print(item.id)
    #     items.append(item)
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
    # Thumbnail asset?
    collection = pystac.Collection(
        id=project,
        description="USGS 3DEP 1m tiles",
        providers=[USGS_PROVIDER, TACOLAB_PROVIDER],
        extent=collection_extent,
        license="PDDL-1.0",
    )

    license = pystac.Link(
        title="USGS 3DEP License",
        rel="license",
        target="https://www.usgs.gov/faqs/what-are-terms-uselicensing-map-services-and-data-national-maps",
    )
    collection.add_links([license])

    collection.add_items(items)

    # Automatic summaries of Item propertes (e.g. proj:code)
    summaries = pystac.summaries.Summarizer().summarize(
        collection.get_items(recursive=True)
    )
    collection.summaries.update(summaries)

    # Add WESM metadata to collection summaries
    add_wesm_metadata_to_collection(collection, s)

    # collection.validate()

    strategy = pystac.layout.TemplateLayoutStrategy(
        item_template=""
    )  # flat layout (no subfolders per item)
    collection.normalize_hrefs(
        f"./catalog/{project}", strategy=strategy
    )  # sets local path
    collection.save(catalog_type=pystac.CatalogType.SELF_CONTAINED)

    # Create a subcatalog
    # TODO: add keywords (since static browsing allows filtering by 'title, description, keywords')!
    # catalog = pystac.Catalog(id=project, description="USGS 3DEP 1m tiles")
    # catalog.add_child(collection)
    # strategy = pystac.layout.TemplateLayoutStrategy(
    #     item_template=""
    # )  # flat layout (no subfolders per item)
    # catalog.normalize_hrefs(
    #     f"./catalog/{project}", strategy=strategy
    # )  # sets local path
    ## Validate
    # catalog.validate_all()
    ## Turn absolute hrefs into relative ones
    # catalog.save(catalog_type=pystac.CatalogType.SELF_CONTAINED)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Generate a STAC catalog from a 3DEP 1m project or workunit."
    )
    parser.add_argument(
        "-p", "--project", type=str, help="Project name (e.g. CO_WestCentral_2019_A19)"
    )
    parser.add_argument(
        "-w",
        "--workunit",
        type=str,
        help="Workunit name (e.g. TX_RedRiver_3Area_B2_2018)",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        default=False,
        help="Overwrite existing project folder if it exists",
    )
    args = parser.parse_args()

    if not args.project and not args.workunit:
        parser.error("Either --project or --workunit must be specified.")
    elif args.project and args.workunit:
        parser.error("Only one of --project or --workunit can be specified.")
    elif args.project:
        project = args.project
        is_workunit = False
    else:
        project = args.workunit
        is_workunit = True

    stac_path = Path(f"./catalog/{project}")
    if stac_path.exists():
        if args.overwrite:
            print(f"Overwriting existing project folder '{project}'.")
            create_stac_catalog(project, is_workunit)
        else:
            print(f"Output folder '{project}' already exists, skipping.")
    else:
        create_stac_catalog(project, is_workunit)
