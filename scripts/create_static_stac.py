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
import base64
import concurrent.futures
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

# max_pool_connections has to keep up with the HEAD fan-out in file_metadata()
s3 = boto3.client(
    "s3", config=Config(signature_version=UNSIGNED, max_pool_connections=64)
)

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
    "CA_SanDiegoCo_2016": "CA_SanDiego_2015_C17_1",
    "CA_Sonoma_A1_2013": "CA_Sonoma_2013",
    "CO_MesaCo_QL2_UTM12_2016": "CO_MesaCo_QL2_2015",
    "Elwha_River_LiDAR_2014_MOD2": "WA_ElwhaRiver_2014",
    "IL_12_County_HenryCO_2009": "IL_12_County_HenryCo_2009",
    "IL_KankakeeCo_2014": "IL_5County_KankakeeCo_2014",
    "IL_McHenryCo_2008": "IL_Twelve_Counties_McHenry",
    "IL_StephensonCo_2009": "IL_12_County_Stephenson_Co_2009",
    "KS_South_Central_AOI_2_Manhattan_2015": "KS_SCentral_L2_2015",
    "KS_SthCentral_AOI4_2015": "KS_SCentral_L4_2015",
    "LA_Atchafalaya2_2012": "LA_ATCHAFALAYA2_2013",
    "MA_NE_CMGP_Sandy_Z19_2013": "MA_NE_CMGP_Sandy_Z18_2013",  # note not sure about Z18 vs Z19...
    "MA_NE_CMGP_Snds_2013": "MA_NE_CMGP_Sandy_Z18_2013",
    "ME_SouthernArea_2012": "ME_SouthernAreas_2012",
    "MI_13Co_Emmett_2015": "MI_EmmetCo_2015",
    "MI_25County_AlleganCo_2015": "MI_AlleganCo_2015",
    "MI_Montcalm_2016": "MI_Montcalm_2015",  # note dates all over the place (2016,2015, 20181024)
    "MO_BooneCo_2014": "MO_BooneCo_2015",
    "MO_SaintLouis_2017": "MO_StLouis_2017",
    "MS_NRCS_Lauderdale_2013": "MS_NRCS_LAUDERDALE_2013",
    "MS_TishomingoNorth_2016": "MS_Tishomingo_North_2016",
    "MS_TishomingoSouth_2016": "MS_Tishomingo_South_2016",
    "NH_CT_RiverNorthL6_2015": "NH_CT_RiverNorthL6_P2_2015",  # or 'NH_CT_RiverNorthL6_P1_2015'?
    "NH_CT_River_North_L6_p3_2015": "NH_CT_River_North_L6_P3_2015",
    "NY_Southwest_2_Co_2016": "NY_Southwest_2_CO_2016",
    "OR_Wallowa_2015": "OR_OLC_Wallowa_2015",
    "SD_NRCS_Fugro_B2_TL_2017": "SD_NRCS_Fugro_B2_2017",
    # "TN_NRCS_2011": but "TN_NRCS_L2_2011_12" or 'TN_NRCS_L1_2011_12' also present?... L2 matches TIFF XML rngdates so I'll use that
    "TN_NRCS_2011": "TN_NRCS_L2_2011_12",
    "UT_Area1WasatchFault_2013": "UT_Wasatch_L4_2013",  # note: unsure, use most recent sourcedem update...
    "UT_MonroeMountain_2016": "UT_MonroeMoutain_2016",
    "UT_Wasatch_L3_2013": "UT_WasatchFault_L3_2013",
    # PROBLEM: 3 different workunits B1, B2, B3.... should use merge of 3 but code is currently setup to just pick one...
    "VA_FEMA-NRCS_SouthCentral_2017_D17": "VA_South_Central_B2_2017",
}
# XML metadata https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/1m/Projects/VA_FEMA-NRCS_SouthCentral_2017_D17/metadata/USGS_1M_17_x35y422_VA_FEMA-NRCS_SouthCentral_2017_D17.xml
# <begdate>20170414</begdate>
# <enddate>20180524</enddate>
# VA_FEMA-NRCS_SouthCentral_2017_D17
#                      workunit collect_start collect_end
# 1703  VA_South_Central_B2_2017    2017/04/14  2017/12/06
# 1704  VA_South_Central_B3_2017    2017/04/14  2017/12/07
# 1836  VA_South_Central_B1_2017    2017/11/16  2018/05/24


def get_titiler_datetime(series):
    start = pd.to_datetime(series.collect_start).isoformat() + "Z"
    end = pd.to_datetime(series.collect_end).isoformat() + "Z"
    datestr = f"{start}/{end}"
    # print(datestr)
    return datestr


# def add_wesm_metadata_to_properties(item, series):
#     '''add all WESM metadata with wesm: prefix to STAC properties inplace'''
#     item.properties.update(series.add_prefix('wesm:').to_dict())


def wesm_summary_fields(series):
    """The wesm:-prefixed collection summaries for one WESM row (JSON-safe).

    Each value is a *one-element list*, not the scalar it describes. A STAC
    summary value has to be a JSON Schema, a set, or a Range object -- scalars
    are not allowed, and this block used to make every collection fail
    validation ("218210 is not valid under any of the given schemas"). Wrapping
    each value makes it a legal one-value set, and STAC Browser still renders
    it, which is the whole reason the metadata lives in summaries rather than in
    extra_fields (see the Mar 2025 "render wesm metadata on collection page"
    commit). Use wesm_value() to read one back.

    Kept separate from add_wesm_metadata_to_collection so refresh_catalog.py can
    compare a live WESM row field-for-field against the summaries snapshotted in
    an existing collection.json (issue #18) -- both sides have to come out of the
    same serialization to be comparable.
    """
    links = ["lpc_link", "sourcedem_link", "metadata_link"]
    # Ensure JSON-serializable
    fields = json.loads(series.drop(links).add_prefix("wesm:").to_json())
    return {k: [v] for k, v in fields.items()}


def wesm_value(summaries, key):
    """One wesm:* summary as a scalar, tolerating the pre-2026 unwrapped form."""
    value = summaries.get(key)
    if isinstance(value, list):
        return value[0] if len(value) == 1 else value
    return value


def add_wesm_metadata_to_collection(collection, series):
    """Add to extra_fields dictionary"""
    # STAC-browser does not render this
    # collection.extra_fields = {'properties':wesm_properties}
    # WARNING: this creates *invalid* STAC, be default STAC-browser still renders it!
    for k, v in wesm_summary_fields(series).items():
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
    # stacify = f"https://titiler.xyz/cog/stac"
    stacify = (
        "https://xpohtuqdoyg4w7ze7loqenojje0earua.lambda-url.us-west-2.on.aws/cog/stac"
    )

    # Test:
    # https://xpohtuqdoyg4w7ze7loqenojje0earua.lambda-url.us-west-2.on.aws/cog/stac?id=USGS_1M_16_x24y472_WI_Statewide_2019_A19&datetime=2019-04-08T00:00:00Z/2019-04-08T00:00:00Z&collection=WI_Statewide_2019_A19&asset_name=elevation&asset_roles=data&with_eo=false&url=https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/1m/Projects/WI_Statewide_2019_A19/TIFF/USGS_1M_16_x24y472_WI_Statewide_2019_A19.tif

    # print(f"Requesting STAC item for {ID}")
    # timeout so one wedged connection can't stall an unattended run's batch,
    # with a short bounded retry on connection errors/timeouts
    TIMEOUT = (10, 120)  # (connect, read) seconds
    for attempt in range(3):
        try:
            r = requests.get(stacify, params=params, timeout=TIMEOUT)
            break
        except requests.RequestException as e:
            print(f"{ID}: {e} (attempt {attempt + 1})")
            time.sleep(2**attempt)
    else:
        raise RuntimeError(f"titiler request failed repeatedly for {URL}")

    # Omit raster stats if it fails (or omit item entirely?)
    if r.status_code != 200:
        # Debug:
        print(URL)
        print(r.url)
        print(r.status_code)
        print(r.text)

        # NOTE: match on r.text, not r.json(). A 502 body is plain text
        # ("Internal Server Error"), so r.json() raised JSONDecodeError here.
        if r.status_code == 500 and "Too many bins for data range" in r.text:
            params["with_raster"] = "false"
            r = requests.get(stacify, params=params, timeout=TIMEOUT)
        else:  # internal server errors seem to be 502?
            # just retry after a moment
            time.sleep(1)
            r = requests.get(stacify, params=params, timeout=TIMEOUT)

        # Statistics are what titiler actually chokes on: it asks GDAL for a
        # decimated read (max_size=1024), which is a few hundred KB off a COG
        # overview but a full-resolution pass over a re-staged non-COG -- e.g.
        # TX_WestTexas_2018_D19's two uncompressed, un-overviewed, 148 MB
        # tiles, which 502 every time. Drop the stats before giving up.
        if r.status_code != 200 and params.get("with_raster") != "false":
            print(f"{ID}: retrying without raster statistics")
            params["with_raster"] = "false"
            r = requests.get(stacify, params=params, timeout=TIMEOUT)

        # Never return an unvalidated body: it used to surface ~1000 tiles
        # later as an opaque JSONDecodeError from json.loads("Internal Server
        # Error"), taking the whole collection build down with it.
        if r.status_code != 200:
            raise RuntimeError(
                f"titiler HTTP {r.status_code} for {URL}: {r.text[:200]!r}"
            )

    return r.text


def get_tif_list_s3(s3path):
    with open(client.CloudPath(s3path)) as f:
        lines = [x.strip("\n") for x in f.readlines()]
        tifs = [x for x in lines if x.endswith(".tif")]
    return tifs


def collapse_workunit_rows(rows):
    """Collapse one or more matched WESM rows to a single series.

    Many projects span multiple WESM workunits (currently 214 of the 909
    cataloged collections) whose collect ranges disagree; taking .iloc[0]
    made the project's dates depend on WESM.csv row order (e.g.
    NV_USFSR4_D23 shipped with workunit _2's dates, but today's CSV orders
    _1 first). Instead use the project-level temporal extent:
    min(collect_start) / max(collect_end) across all matched rows, with the
    remaining fields from a deterministic representative row (first by
    workunit name). Single-row matches are returned unchanged.
    """
    rows = rows.sort_values("workunit")
    s = rows.iloc[0].copy()
    if len(rows) > 1:
        start = pd.to_datetime(rows.collect_start, errors="coerce").min()
        end = pd.to_datetime(rows.collect_end, errors="coerce").max()
        if pd.isna(end):  # no usable collect_end anywhere: fall back to start
            end = start
        if pd.notna(start):
            s.collect_start = start.strftime("%Y/%m/%d")
        if pd.notna(end):
            s.collect_end = end.strftime("%Y/%m/%d")
    return s


def load_wesm():
    """The USGS WESM metadata table (locally cached by cloudpathlib)."""
    wesm_csv = client.CloudPath(
        "s3://prd-tnm/StagedProducts/Elevation/metadata/WESM.csv"
    )
    return pd.read_csv(wesm_csv)


def get_wesm_series(project, is_workunit=False, df=None, warn=True):
    """Single collapsed WESM row for a project or workunit name.

    Pass an already-loaded `df` to look up many names without re-reading the CSV,
    and warn=False to look them up quietly (refresh_catalog.py checks all ~900
    cataloged collections on every run).
    """
    if df is None:
        df = load_wesm()

    # Fast track manually specified mappings
    if project in onemeter_folder_to_wesm:
        name = onemeter_folder_to_wesm[project]
        # NOTE: maybe issue here in cases of updated processing? check for source_dem update?
        # .e.g. TN_NRCS_L2_2011_12
        rows = df[df.workunit == name]
        if rows.empty:
            raise ValueError(
                f"Workunit '{name}' mapped from folder '{project}' not found in WESM metadata"
            )
        s = collapse_workunit_rows(rows)
    else:
        if is_workunit:
            if project not in df.workunit.values:
                alternatives = df[
                    df.workunit.str.startswith(project)
                ].workunit.to_list()
                raise ValueError(
                    f"Workunit '{project}' not found in WESM metadata, close matches: {alternatives}"
                )
            s = collapse_workunit_rows(df[df.workunit == project])
        else:
            if project not in df.project.values:
                alternatives = df[df.project.str.startswith(project)].project.to_list()
                raise ValueError(
                    f"Project '{project}' not found in WESM metadata, close matches: {alternatives}"
                )
            s = collapse_workunit_rows(df[df.project == project])

    if warn and not s.onemeter_category == "Meets":
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

    # Break tiflist into batches of no more than 200 URLs
    # waiting on quota increase... from 10 to 1000 :)
    def batch_urls(urls, batch_size=100):
        for i in range(0, len(urls), batch_size):
            yield urls[i : i + batch_size]

    results = []
    for batch in batch_urls(tiflist):
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


PROJ_EXT_V2 = "https://stac-extensions.github.io/projection/v2.0.0/schema.json"
PROJ_EXT_V1_PREFIX = "https://stac-extensions.github.io/projection/v1."


def normalize_projection(item_dict):
    """Normalize projection extension to v2.0.0 (proj:code).

    The existing catalog (~123k items) uniformly uses projection v2.0.0 with
    proj:code, but depending on the deployed titiler version the STAC endpoint
    may return v1.1.0 with proj:epsg. Keep the catalog (and the geoparquet
    column schema) uniform regardless of which titiler is answering.
    """
    exts = item_dict.get("stac_extensions", [])
    item_dict["stac_extensions"] = [
        PROJ_EXT_V2 if e.startswith(PROJ_EXT_V1_PREFIX) else e for e in exts
    ]
    props = item_dict.get("properties", {})
    epsg = props.pop("proj:epsg", None)
    if epsg is not None and "proj:code" not in props:
        props["proj:code"] = f"EPSG:{epsg}"
    return item_dict


# TODO: can probably speed this up a lot just by taking min/max of bboxes
def get_collection_bbox(item_dicts):
    gf = gpd.GeoDataFrame.from_features(item_dicts)
    return gf.total_bounds.tolist()


def list_tiffs_in_project(project, bucket="prd-tnm"):
    # NOTE: must paginate! A single list_objects_v2 call returns at most 1000
    # keys, which silently truncated projects with >1000 tiles (e.g. OR_SouthEast_D22)
    project_prefix = f"StagedProducts/Elevation/1m/Projects/{project}/TIFF"
    paginator = s3.get_paginator("list_objects_v2")
    tiff_files = []
    for page in paginator.paginate(Bucket=bucket, Prefix=project_prefix):
        tiff_files += [
            obj["Key"]
            for obj in page.get("Contents", [])
            if obj["Key"].endswith(".tif")
        ]

    return [f"https://{bucket}.s3.amazonaws.com/{key}" for key in tiff_files]


# ------------------------------------------------------- file metadata (#17)
# refresh_catalog.py diffs *tile sets*, so a tile reprocessed in place -- same S3
# key, new pixels -- is invisible: no add, no remove (issue #17;
# USGS_1M_12_x29y395_AZ_AubreyCherry_2020_D20 went 10012x10012 -> 1234x1549 and
# was only caught because its project independently lost 5 tiles). Recording the
# STAC file extension's file:size + file:checksum per asset gives the catalog its
# own drift baseline, and lets a consumer tell whether its copy is current.
#
# The checksum is S3's CRC64NVME, not a hash we compute: AWS stores it as object
# metadata for every object in prd-tnm (verified: 124407/124407), so re-checking
# it every refresh is a HEAD, not a 30 TB read. Two properties make it the right
# signal where the alternatives are not:
#   * it is derived from the bytes, so a re-staged tile that happens to keep its
#     byte count still changes it. file:size alone cannot see that, and 12.6% of
#     the catalog (15622 tiles) is stored uncompressed, where the byte count is
#     fixed by proj:shape x dtype and *cannot* move when pixels change.
#   * ChecksumType is FULL_OBJECT, i.e. over the whole object regardless of how
#     it was chunked on upload -- unlike ETag, which is a hash of part hashes for
#     65% of these tiles and so changes on a plain re-copy that touched no pixels.
#     LastModified is useless here for the same reason: all 124407 objects carry a
#     2026 stamp from a bulk prefix rewrite (issue #6).
FILE_EXT = "https://stac-extensions.github.io/file/v2.1.0/schema.json"

# multihash prefix for a CRC64NVME digest: multicodec crc64-nvme is 0x0165, whose
# unsigned-varint encoding is e5 02, followed by the 8-byte digest length. The
# file extension wants the whole thing hex-encoded lowercase, so a checksum is
# always "e50208" + 16 hex digits.
CRC64NVME_MULTIHASH_PREFIX = "e50208"


def crc64nvme_multihash(b64_digest):
    """S3's base64 ChecksumCRC64NVME -> hex multihash for file:checksum."""
    return CRC64NVME_MULTIHASH_PREFIX + base64.b64decode(b64_digest).hex()


def file_metadata(urls, threads=32):
    """{item_id: {"file:size": int, "file:checksum": str}} for tif URLs.

    One HEAD per tile (~1100/s at 32-64 threads, so ~2 min for the whole
    catalog). file:checksum is omitted -- not faked -- if S3 has no CRC64NVME
    for an object; every object in prd-tnm has one today, so a gap is worth
    seeing in the PR body rather than papering over.
    """

    def head(url):
        key = url.split(".amazonaws.com/", 1)[-1]
        resp = s3.head_object(Bucket="prd-tnm", Key=key, ChecksumMode="ENABLED")
        meta = {"file:size": resp["ContentLength"]}
        digest = resp.get("ChecksumCRC64NVME")
        if digest:
            meta["file:checksum"] = crc64nvme_multihash(digest)
        return meta

    urls = list(urls)
    with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as ex:
        metas = list(ex.map(head, urls))
    return {url.split("/")[-1][:-4]: meta for url, meta in zip(urls, metas)}


# pystac.Asset.to_dict() emits href/type/title/description, then extra_fields in
# insertion order, then roles -- so file:* has to land after the other extra
# fields but *before* roles, or a backfilled item and a rebuilt one differ by key
# order alone and every project churns on its next rebuild.
_ASSET_LEADING = ("href", "type", "title", "description")
FILE_FIELDS = ("file:size", "file:checksum")


def add_file_metadata(item_dict, meta):
    """Stamp file:size/file:checksum onto an item's asset(s), in place.

    Applied identically by the builder and by backfill_file_metadata.py, so a
    backfilled item is byte-identical to a freshly built one.
    """
    if not meta:
        return item_dict
    exts = item_dict.setdefault("stac_extensions", [])
    if FILE_EXT not in exts:
        # the catalog's extension lists are uniformly sorted; keep them that way
        item_dict["stac_extensions"] = sorted(exts + [FILE_EXT])
    assets = item_dict.get("assets", {})
    for name, asset in assets.items():
        rebuilt = {k: asset[k] for k in _ASSET_LEADING if k in asset}
        rebuilt.update(
            {
                k: v
                for k, v in asset.items()
                # drop any previously recorded file:* so a stale checksum can
                # never survive alongside a fresh size
                if k not in _ASSET_LEADING and k not in ("roles", *FILE_FIELDS)
            }
        )
        rebuilt.update(meta)
        if "roles" in asset:
            rebuilt["roles"] = asset["roles"]
        assets[name] = rebuilt
    return item_dict


# pystac picks its JSON serializer by what happens to be installed: orjson if it
# can import it, the stdlib otherwise. The two disagree on float repr -- orjson
# writes 0.00009924415650406505 and -3.4028230607370965e38 where the stdlib
# writes 9.924415650406505e-05 and -3.4028230607370965e+38 -- so the on-disk
# format of an item depended on the environment that built it, and the catalog
# ended up holding both spellings (~18.4k items stdlib, 32 orjson).
#
# Removing orjson is not an option: stac-geoparquet depends on it, so it is
# always in the environment. Pinning the serializer here is the better fix
# anyway, because it makes the output independent of what is installed rather
# than merely consistent with today's solve. The stdlib is the one to pin to --
# 125309 of the 125341 committed files already match it, so normalizing costs 32
# files instead of 18435.
class StdlibStacIO(pystac.stac_io.DefaultStacIO):
    """StacIO that always serializes with the stdlib, never orjson."""

    def json_dumps(self, json_dict, *args, **kwargs):
        return json.dumps(json_dict, indent=2)


pystac.StacIO.set_default(StdlibStacIO)


def dump_stac_json(d):
    """Serialize a catalog dict exactly as pystac's save() now writes it."""
    return json.dumps(d, indent=2)


def item_file_metadata(item_dict):
    """The file:size/file:checksum recorded on an item's elevation asset."""
    asset = item_dict.get("assets", {}).get(ASSET_NAME, {})
    return {k: asset[k] for k in FILE_FIELDS if k in asset}


def reusable_items(project, datetime_str, full=False, live_meta=None):
    """{item_id: item dict} for items already in catalog/<project>.

    A rebuild is triggered by the tile *set* changing, but re-deriving all ~1000
    existing items from titiler to pick up 22 new ones is both slow and fragile:
    one tile titiler cannot answer for kills the whole collection (issue: two of
    TX_WestTexas_2018_D19's tiles were re-staged as 148 MB non-COGs and 502 on
    every statistics request). Everything titiler derives from a tile -- geometry,
    projection, raster stats -- is a function of that tile alone, so an unchanged
    tile's item can be reused as is. Only the datetimes come from WESM, and those
    are restamped here so an incremental rebuild ends up where a full one would.

    full=True re-derives everything from titiler instead (after a titiler
    upgrade, or to pick up tiles whose pixels were re-staged in place).

    live_meta ({item_id: file_metadata}) makes reuse content-aware: an item whose
    recorded file:size/file:checksum disagrees with what S3 holds now describes
    bytes that no longer exist, so it is *not* reused and falls through to
    titiler (issue #17). Items with nothing recorded yet are reused as before --
    backfill_file_metadata.py establishes the baseline without a titiler round
    trip, and everything built after that carries one.
    """
    if full:
        return {}
    start, end = datetime_str.split("/")
    items = {}
    for path in sorted(Path(f"./catalog/{project}").glob("*.json")):
        if path.name == "collection.json":
            continue
        try:
            item = json.loads(path.read_text())
        except Exception:
            continue  # unreadable -> let titiler rebuild it
        if live_meta is not None:
            have = item_file_metadata(item)
            want = live_meta.get(path.stem, {})
            # compare only the fields the item actually carries, so a
            # not-yet-backfilled item is not treated as drifted
            if have and any(have[k] != want.get(k) for k in have):
                continue  # re-staged in place -> re-derive from titiler
        item["properties"]["start_datetime"] = start
        item["properties"]["end_datetime"] = end
        # drop the on-disk root/collection/parent links: they are relative
        # ("./collection.json") and unresolvable until the item has an href, and
        # collection.add_items() + normalize_hrefs() rewrite them regardless
        item["links"] = []
        items[path.stem] = item
    return items


def preserve_raster_bands(item_dicts, project, live_meta):
    """Carry forward raster:bands that a rebuild would otherwise drop.

    titiler answers a tile whose valid pixels span less than one float32 ULP --
    a perfectly flat water surface -- with 500 "Too many bins for data range",
    because numpy takes the bin dtype from the array and 10 float32 bins across
    a one-ULP range collapse to duplicate edges. (The same data histograms fine
    in float64, and the failure is not size: the smallest offender here is
    3.7 MB. It is not fixed by upgrading either -- titiler 2.2.1 fails the same
    tile with "Cannot create 3 finite-sized bins".)

    create_stac_item's fallback then retries with with_raster=false, which is
    all or nothing: it drops data_type, nodata, scale, offset and unit along
    with the statistics, none of which needed a histogram. The rebuilt item is
    strictly worse than the committed one, and audit_catalog.py cannot see it,
    because a fresh derivation of that tile returns nothing either -- both
    sides agree and it reports clean.

    Statistics describe pixels, so the committed block is only carried forward
    when the tile's bytes are unchanged. If the tile was re-staged, the old
    numbers are no longer about it and are dropped with a warning rather than
    silently attached to different data.
    """
    restored = stale = 0
    for d in item_dicts:
        asset = d.get("assets", {}).get(ASSET_NAME, {})
        if not asset or asset.get("raster:bands"):
            continue
        path = Path(f"./catalog/{project}/{d['id']}.json")
        if not path.is_file():
            continue
        try:
            old_asset = json.loads(path.read_text())["assets"][ASSET_NAME]
        except Exception:
            continue
        if not old_asset.get("raster:bands"):
            continue
        have = {k: old_asset[k] for k in FILE_FIELDS if k in old_asset}
        want = live_meta.get(d["id"], {})
        if have and any(have[k] != want.get(k) for k in have):
            stale += 1
            continue
        asset["raster:bands"] = old_asset["raster:bands"]
        restored += 1
    if restored:
        warnings.warn(
            f"{restored} item(s) kept their committed raster:bands: titiler "
            "returned none this time (see preserve_raster_bands)"
        )
    if stale:
        warnings.warn(
            f"{stale} item(s) lost raster:bands: titiler returned none and the "
            "tile's bytes changed, so the committed statistics cannot be reused"
        )
    return item_dicts


def create_stac_catalog(project, is_workunit=False, full=False):
    """Create a STAC catalog from a 3DEP 1m project"""
    s = get_wesm_series(project, is_workunit=is_workunit)

    # NOTE: not all folders have 0_file_download_links.txt
    # or 0_file_download_links.txt has errors
    # project_inventory = f"s3://prd-tnm/StagedProducts/Elevation/1m/Projects/{project}/0_file_download_links.txt"
    # tiflist = get_tif_list_s3(project_inventory)

    # Instead list TIFF/ folder directly
    tiflist = list_tiffs_in_project(project)
    if len(tiflist) == 0:
        raise ValueError(f"No tiffs found for project '{project}'")

    # Same datatime range for all tifs in a project
    # Formatted for TITILER 2019-08-21T00:00:00Z/2019-09-19T00:00:00Z'
    DATETIME = get_titiler_datetime(s)

    # size + checksum for every live tile: the baseline stamped onto the items
    # below, and the yardstick reusable_items() checks the existing ones against
    live_meta = file_metadata(tiflist)

    reused = reusable_items(project, DATETIME, full=full, live_meta=live_meta)
    todo = [url for url in tiflist if url.split("/")[-1][:-4] not in reused]
    # tiles that vanished upstream are simply not in tiflist, so they drop out
    reused = {
        i: d for i, d in reused.items() if i in {u.split("/")[-1][:-4] for u in tiflist}
    }
    print(
        f"creating STAC Items from {len(tiflist)} tifs "
        f"({len(todo)} from titiler, {len(reused)} reused)..."
    )

    # ASYNC
    results = generate_stac_from_titiler(todo, DATETIME) if todo else []

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
    # normalize_projection() on the titiler responses only: reused items came
    # out of the catalog, which is already uniformly projection v2.0.0
    item_dicts = [normalize_projection(json.loads(x)) for x in results]
    item_dicts += list(reused.values())
    # before add_file_metadata, so a carried-forward raster:bands lands in the
    # same key position a titiler-derived one would (ahead of file:* and roles)
    preserve_raster_bands(item_dicts, project, live_meta)
    # stamp both paths: a titiler item has no file metadata at all, and a reused
    # one may predate the backfill
    for d in item_dicts:
        add_file_metadata(d, live_meta.get(d["id"], {}))
    n_nochecksum = sum(
        1 for d in item_dicts if "file:checksum" not in d["assets"].get(ASSET_NAME, {})
    )
    if n_nochecksum:
        warnings.warn(f"{n_nochecksum} tiles have no CRC64NVME checksum in S3")
    items = [pystac.read_dict(d) for d in item_dicts]

    # THis will be 'collection' level metadata, not in each item
    # [add_wesm_metadata_to_properties(item, s) for item in items]
    collection_bbox = get_collection_bbox(item_dicts)

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

    # sorted by id, because link order is add_items() order and nothing
    # downstream re-sorts it: pystac takes no ordering argument on add_items(),
    # normalize_hrefs() or save(). Unsorted, the order is
    # [items from titiler] + [items reused from disk], so it shifts with the
    # ratio of rebuilt to reused tiles and a rebuild that changed three items
    # rewrites every link in the collection. The Summarizer below walks the
    # same links, so this also fixes list-valued summaries (proj:code) flipping
    # order for the same reason.
    collection.add_items(sorted(items, key=lambda i: i.id))

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
        help="Overwrite existing project folder if it exists "
        "(items already present are reused; only new tiles hit titiler)",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        default=False,
        help="With --overwrite, re-derive every item from titiler instead of "
        "reusing existing ones (after a titiler upgrade, or to pick up tiles "
        "whose pixels were re-staged in place)",
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
            create_stac_catalog(project, is_workunit, full=args.full)
        else:
            print(f"Output folder '{project}' already exists, skipping.")
    else:
        create_stac_catalog(project, is_workunit, full=args.full)
