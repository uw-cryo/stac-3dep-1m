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
# File format: one row per zone and grid row y, with the row's x values as
# space-separated runs -- "10,402,60-76" is cells x60y402 ... x76y402 in zone 10.


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


def encode_runs(cells):
    """One row per (zone, grid row): the x values as runs, "60-76 80".

    One line per cell is 78k lines; CONUS is mostly contiguous along a row, so
    runs bring it to ~1.9k lines (26 KB) and the diff stays readable.
    """
    by_row = {}
    for zone, name in cells:
        x, y = map(int, name[1:].split("y"))
        by_row.setdefault((zone, y), []).append(x)
    for (zone, y), xs in sorted(by_row.items()):
        xs.sort()
        runs, start = [], xs[0]
        for prev, x in zip(xs, xs[1:] + [None]):
            if x != prev + 1:
                runs.append(f"{start}-{prev}" if prev > start else str(start))
                start = x
        yield zone, y, " ".join(runs)


def read_cells(path=DEFAULT_OUTPUT):
    """(zones, xs, ys) arrays of the cells in a file written by encode_runs()."""
    zones, xs, ys = [], [], []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            for run in row["x"].split():
                lo, _, hi = run.partition("-")
                for x in range(int(lo), int(hi or lo) + 1):
                    zones.append(int(row["utm_zone"]))
                    xs.append(x)
                    ys.append(int(row["y"]))
    return np.array(zones), np.array(xs), np.array(ys)


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
        w.writerow(["utm_zone", "y", "x"])
        w.writerows(encode_runs(cells))
    print(f"wrote {len(cells):,} CONUS cells to {a.output}")


if __name__ == "__main__":
    main()
