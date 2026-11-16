#!/usr/bin/env python
"""
Write the list of USGS 1 m 10 km grid cells that make up CONUS.

This is the denominator for the "% of CONUS" figure in release notes
(scripts/release_notes.py). It is committed as data/conus_10km_cells.csv so the
figure is stable from release to release and the release job needs no network
access for it; rerun this only to adopt a newer grid or boundary.

Sources:

* the USGS grid itself, published beside the 1 m projects:
  s3://prd-tnm/StagedProducts/Elevation/1m/FullExtentSpatialMetadata/10_km_cell_grid.gpkg
  One polygon per 10 km cell per UTM zone, `name` = the x<X>y<Y> in tile names
  (`slug` is not usable as a key: it carries suffixes such as "x29y395-3").
  Each zone's cells form a full rectangle -- 16,241 per CONUS zone, ocean,
  Canada and Mexico included -- so it has to be clipped to be a total.
* the Census 2024 cartographic boundary for the nation (1:5,000,000), which is
  clipped to the shoreline (the Great Lakes and Chesapeake Bay fall outside it):
  https://www2.census.gov/geo/tiger/GENZ2024/shp/cb_2024_us_nation_5m.zip

A cell is kept when it is in zones 10-19 and its center lies both inside CONUS
(the nation polygon clipped to the lower-48 bounding box, which drops Alaska,
Hawaii and the territories) and inside the cell's own 6-degree zone strip. The
last condition gives every point one cell, even though the per-zone rectangles
overlap their neighbours. Cell x/y are the tile's west and *north* edges in
units of 10 km (x29y395 spans E 290-300 km, N 3940-3950 km), which the USGS
polygons confirm.

Usage:
pixi run build-conus-grid                       # downloads both sources
pixi run build-conus-grid --grid 10_km_cell_grid.gpkg --boundary cb_2024_us_nation_5m.zip
"""

import argparse
import csv
import tempfile
import urllib.request
from pathlib import Path

import geopandas as gpd
import numpy as np
import shapely
from pyproj import Transformer

GRID_URL = (
    "https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/1m/"
    "FullExtentSpatialMetadata/10_km_cell_grid.gpkg"
)
BOUNDARY_URL = "https://www2.census.gov/geo/tiger/GENZ2024/shp/cb_2024_us_nation_5m.zip"
CONUS_ZONES = range(10, 20)
# lower-48 bounding box: keeps CONUS, drops Alaska, Hawaii, PR/USVI
CONUS_BBOX = (-125.5, 24.0, -66.5, 49.6)
DEFAULT_OUTPUT = Path(__file__).resolve().parent.parent / "data/conus_10km_cells.csv"


def cell_centers(zones, xs, ys):
    """Lon/lat (NAD83) of the centers of cells given by zone and x/y name parts."""
    zones, xs, ys = map(np.asarray, (zones, xs, ys))
    lon = np.empty(len(zones))
    lat = np.empty(len(zones))
    for z in np.unique(zones):
        s = zones == z
        to_ll = Transformer.from_crs(26900 + int(z), 4269, always_xy=True)
        lon[s], lat[s] = to_ll.transform(xs[s] * 1e4 + 5e3, ys[s] * 1e4 - 5e3)
    return lon, lat


def conus_cells(grid_path, boundary_path):
    grid = gpd.read_file(grid_path, columns=["name", "utm_zone"], ignore_geometry=True)
    grid = grid[grid.utm_zone.isin(CONUS_ZONES)]
    xy = grid.name.str.extract(r"^x(\d+)y(\d+)$").astype(int)
    zones = grid.utm_zone.to_numpy(int)
    lon, lat = cell_centers(zones, xy[0], xy[1])

    nation = gpd.read_file(boundary_path).to_crs(4269).geometry.union_all()
    conus = shapely.clip_by_rect(nation, *CONUS_BBOX)
    shapely.prepare(conus)

    west = -180 + 6 * (zones - 1)
    keep = (lon >= west) & (lon < west + 6) & shapely.contains_xy(conus, lon, lat)
    rows = sorted(
        zip(zones[keep], xy[0].to_numpy()[keep], xy[1].to_numpy()[keep]),
    )
    return [(int(z), f"x{x}y{y}") for z, x, y in rows]


def fetch(url, dest):
    print(f"downloading {url}")
    urllib.request.urlretrieve(url, dest)
    return dest


def main(args=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--grid", help=f"local copy of {GRID_URL}")
    p.add_argument("--boundary", help=f"local copy of {BOUNDARY_URL}")
    p.add_argument("-o", "--output", default=DEFAULT_OUTPUT, type=Path)
    a = p.parse_args(args)

    with tempfile.TemporaryDirectory() as tmp:
        grid = a.grid or fetch(GRID_URL, Path(tmp) / "10_km_cell_grid.gpkg")
        boundary = a.boundary or fetch(BOUNDARY_URL, Path(tmp) / "nation.zip")
        cells = conus_cells(grid, boundary)

    a.output.parent.mkdir(parents=True, exist_ok=True)
    with open(a.output, "w", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["utm_zone", "name"])
        w.writerows(cells)
    print(f"wrote {len(cells):,} CONUS cells to {a.output}")


if __name__ == "__main__":
    main()
