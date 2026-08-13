# Upstream write-up: statistics fail on constant-valued float32 rasters

**Status:** not filed. This is a draft for a maintainer of this repo to file manually.

**Where to file:** the root cause belongs in [rio-tiler](https://github.com/cogeotiff/rio-tiler)
(it computes the histogram). There is a smaller, separate request for
[titiler](https://github.com/developmentseed/titiler) — see "Two asks" below.

Everything here reproduces against the **public** `https://titiler.xyz`, so no private
deployment is needed to verify it.

---

## Summary

`/cog/statistics` and `/cog/stac` return **500** for a valid COG whose valid pixels are
all (nearly) the same value:

```
{"detail":"Too many bins for data range. Cannot create 10 finite-sized bins."}
```

The tile is fine — it is a small, tiled, overviewed COG with a properly declared
`nodata`. The failure is `numpy.histogram` taking its bin dtype from a `float32` array:
when the data range is one float32 ULP wide, ten bin edges collapse onto each other and
numpy refuses. The identical data histograms without complaint in `float64`.

This is not a size or timeout problem. The file below is 3.7 MB.

## Reproduce

### 1. numpy alone — the actual mechanism

```python
import numpy as np

lo, hi = -0.8499999642372131, -0.8499999046325684   # two adjacent float32 values
a = np.array([lo, hi], dtype="float32")

print(float(a[1]) - float(a[0]))          # 5.960464477539063e-08
print(np.spacing(np.float32(0.85)))       # 5.9604645e-08   -> the gap is exactly 1 ULP

np.histogram(a, bins=10)                  # ValueError: Too many bins for data range.
                                          #             Cannot create 10 finite-sized bins.
np.histogram(a.astype("float64"), bins=10)  # -> (array([1, 0, 0, 0, 0, 0, 0, 0, 0, 1]), ...)
```

### 2. Through titiler (public instance)

A real USGS 3DEP tile — an entirely flat stretch of water, every valid pixel ≈ -0.85 m:

```bash
URL='https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/1m/Projects/DE_Statewide_B23/TIFF/USGS_1M_18_x47y433_DE_Statewide_B23.tif'

curl -s "https://titiler.xyz/cog/statistics?url=$URL"
# 500 {"detail":"Too many bins for data range. Cannot create 10 finite-sized bins."}

curl -s "https://titiler.xyz/cog/stac?url=$URL&asset_name=elevation&with_eo=false"
# 500 {"detail":"Too many bins for data range. Cannot create 3 finite-sized bins."}
```

Note the two endpoints report different bin counts (10 vs 3) for the same raster.

### 3. It is only the histogram

Asking for a single bin succeeds, and the statistics themselves are perfectly
well-defined:

```bash
curl -s "https://titiler.xyz/cog/statistics?url=$URL&histogram_bins=1"
# 200 {"b1":{"min":-0.8499999642372131,"max":-0.8499999046325684,
#            "mean":-0.849999904653309,"count":31612.0, ...}}
```

`histogram_range=-1,0` also succeeds. So nothing about the raster prevents statistics
being computed — only the default binning does.

## The raster

Read locally with rasterio, for completeness:

```
dtype      : float32
nodata     : -999999.0            (declared)
shape      : (10012, 10012)       overviews: [2, 4, 8, 16, 32]
COG        : valid (rio-cogeo reports COG: true, no errors)
size       : 3.7 MB
masked min : -0.84999996   masked max: -0.8499999    <- one ULP apart
```

## Versions tested

| | titiler | rasterio | GDAL |
| --- | --- | --- | --- |
| `titiler.xyz` | **2.2.1** | 1.5.0 | 3.12.1 |
| our deployment | **0.22.4** | 1.4.3 | 3.9.3 |

Both fail. The newer stack picks a different default bin count but hits the same wall,
so this is not fixed by upgrading.

## Impact

We maintain a static STAC catalog of ~124k USGS 3DEP 1 m tiles, built by calling
`/cog/stac` once per tile. 9 tiles fail this way — every one a flat water surface
(Delaware coast, Michigan lakes, Chesapeake Bay), where the whole tile is a single
elevation value.

The practical damage is larger than "no histogram", because the natural client-side
fallback is to retry with `with_raster=false`, and that is all-or-nothing: it drops
`data_type`, `nodata`, `scale`, `offset` and `unit` along with the statistics, none of
which needed a histogram. So one un-binnable histogram costs the asset its entire
`raster:bands` block.

## Two asks

1. **rio-tiler** — compute the histogram in `float64` (or fall back to fewer bins when
   the range cannot be split). The data is not pathological; only the `float32` bin
   dtype is. This is the fix that matters.

2. **titiler** — plumb `histogram_bins` / `histogram_range` through `/cog/stac` the way
   `/cog/statistics` already does. Today a caller who hits this on `/cog/stac` has no
   parameter to reach for, even though the same raster succeeds on `/cog/statistics`
   with `histogram_bins=1`. Useful independently of (1).

## Our workaround, for context

We do not re-derive what titiler will not return: `preserve_raster_bands()` in
`scripts/create_static_stac.py` carries the previously committed `raster:bands` forward
when a rebuild comes back without one, and only when the tile's bytes are unchanged
(`file:size` + `file:checksum`), since statistics describe pixels.
