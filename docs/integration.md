# Building a pipeline on DataLoader output

For developers of downstream pipelines (compositing, LandTrendr, BugNet,
BULC-D, model training): how to find imagery in a DataLoader dataset,
interpret bands and QA, apply scale/offset, deal with acquisitions that
are missing or failed, and read an archive while it is still downloading.
The field-by-field reference is [outputs](outputs.md); this page is the
how-to.

Rule of thumb: **read `manifest.json` and `items.jsonl`, never parse
filenames or directory names.** Everything below is available from those
two files (and is mirrored in each GeoTIFF's own metadata).

## 1. Find the imagery

```python
from data_loader import open_dataset

ds = open_dataset("/vol/v1/data/oregon_landsat_ard")
for path, item, f in ds.files(grid="h003v004", start="2000-06-01", end="2000-09-30"):
    print(item["date"], item["platform"], item["itemId"], path)
```

- `ds.files()` yields `(absolute path, item row, file entry)` for
  **acquired** files only. Filters: `sensor`, `grid` (tile id, or `aoi` for
  a bbox dataset), `start`/`end` (inclusive dates), `kind` (`scene` or
  `composite`), `band_set` (`bands` (default), `indices`, or `None` for
  both).
- `ds.items(status=None)` yields every row whatever its status; useful
  for coverage reports.
- Each call re-reads `items.jsonl` (about 5 s and 0.7 GB for the 89k-row
  Oregon archive), so read once and keep the rows if you query
  repeatedly: `rows = list(ds.items())`.
- Without Python: `items.jsonl` is one JSON object per line; select rows
  with `"status": "acquired"` and read `files[].path` relative to the
  dataset directory.

## 2. Bands and their order

`manifest["bandSets"][sensor]["bands"]` lists the bands of every `bands`
file in order, with `name`, `role` (`reflectance` or `qa`), `dataType` and
`nodata`. The GeoTIFF band descriptions carry the same names, so you can
also select by name:

```python
import rasterio

with rasterio.open(path) as src:
    idx = {name: i + 1 for i, name in enumerate(src.descriptions)}
    nir = src.read(idx["nir"])
    qa = src.read(idx["qa_pixel"])
```

Canonical names are the same for every provider and sensor: `blue`,
`green`, `red`, `nir`, `swir1`, `swir2` (then the QA band if
`qa_band: true`). For Landsat they map to TM/ETM+ bands 1, 2, 3, 4, 5, 7
and OLI bands 2–7; DataLoader picks the right source file per platform.

## 3. Scale and offset

Native (`uint16`) files store the source integers. Convert with the
**per-file** values, which are authoritative:

```python
import numpy as np

so = f["bandScaleOffset"]                 # {"blue": [scale, offset], ...}
nd = ds.manifest["bandSets"]["landsat"]["bands"][0]["nodata"]   # 0 for native
refl = nir.astype("float32") * so["nir"][0] + so["nir"][1]
refl[nir == nd] = np.nan
```

- Landsat C2 (ARD and Level-2): `scale 2.75e-05`, `offset -0.2`, for every
  file.
- Sentinel-2 L2A: `scale 1e-4`; offset `-0.1` for ESA processing baseline
  04.00 and later when the catalog serves ESA's numbers unchanged
  (Planetary Computer, CDSE), `0` before 2022 and on Earth Search, which
  has already removed it. That is why you must use the per-file value.
- The same scale/offset is set in each GeoTIFF's GDAL band metadata
  (`src.scales`, `src.offsets`), so GDAL-based tools apply it correctly.
- `float32` datasets already hold reflectance (`bandScaleOffset` is
  `[1, 0]`); nodata is `NaN`.

Datasets written by DataLoader 1.0 recorded `bandScaleOffset` only where it
differed from `bandSets`; run `data-loader upgrade DIR` once to fill it in
(see [outputs](outputs.md#compatibility-promise-and-upgrading)).

## 4. Clouds, shadows, fill: the QA band

Archives keep every observation and every pixel, clouds included
(`processing.pixelCloudMask.applied: false`). Masking is your pipeline's
decision, made from the QA band. Its meaning is in the manifest
(`bandSets[sensor].bands[-1].qa`).

**Landsat `qa_pixel`** (USGS C2 QA_PIXEL bitmask):

| Bit | Meaning |
|---|---|
| 0 | fill |
| 1 | dilated cloud |
| 2 | cirrus (OLI) |
| 3 | cloud |
| 4 | cloud shadow |
| 5 | snow |
| 6 | clear |
| 7 | water |
| 8–9, 10–11, 12–13, 14–15 | cloud, cloud-shadow, snow/ice, cirrus confidence (0 none, 1 low, 2 medium, 3 high) |

```python
fill = (qa & 1) != 0
cloudy = (qa & (1 << 1 | 1 << 2 | 1 << 3 | 1 << 4)) != 0   # what pixel_cloud_mask: true removes
usable = ~fill & ~cloudy
```

**Sentinel-2 `scl`** (classes): 0 no data, 1 saturated/defective, 2 dark
features, 3 cloud shadow, 4 vegetation, 5 not vegetated, 6 water,
7 unclassified, 8/9 cloud medium/high probability, 10 thin cirrus,
11 snow.

Two traps:

- **Don't use the nodata mask on the QA band.** A GeoTIFF has one nodata
  value per file, so the QA band carries it too (`0` in native files). Find
  fill with QA bit 0 (Landsat) or class 0 (Sentinel-2).
- **Reflectance DN 0 is not always fill.** USGS clamps very dark surface
  reflectance (deep water, shadow) to 0, which the nodata value also
  marks. In one winter Landsat 9 scene of the Oregon archive, about 1% of
  the tile's pixels were not fill but had blue = 0. If dark targets
  matter, decide fill from QA bit 0, not from DN 0.

## 5. Grids and the GeoTIFF layout

- **Tile archives** (`aoi.tiles`): every file of a tile is on the same
  5000 × 5000 px, 30 m grid: the USGS CONUS ARD Albers Equal-Area grid
  (WGS 84 datum, so it has no EPSG code; use `grids[sensor][tile].crsWkt`
  and `.transform`). Files of one tile stack pixel for pixel; no
  resampling has been done.
- **Bbox datasets**: one grid, labelled `aoi`, described in
  `grids[sensor]["aoi"]` (CRS, transform, size).
- Files are tiled GeoTIFFs, 256 × 256 internal blocks, deflate with a
  horizontal predictor, BigTIFF if needed, no overviews; they are not
  COGs. For Zarr or Dask, chunk in multiples of 256 px to read whole blocks.
- One file per acquisition: the reflectance bands followed by the QA band.
  A Landsat ARD file is about 93 MB.

## 6. Missing, failed and partial acquisitions

Every acquisition the catalog lists for the request has a row, whatever
happened to it:

| `status` | What to do |
|---|---|
| `acquired` | Use it. |
| `pending` | Not acquired yet (run in progress or interrupted). Will appear later. |
| `failed` | Every attempt failed so far (`error`, `attempts`). A later run may still get it; treat it as missing for now and report it. |
| `filtered` | Excluded on purpose by `filters.max_cloud_percent` (`reason`). Archives use 100, so none. |

Also per acquired row:

- `validFraction`: share of pixels that are not nodata in the first band.
  ARD tiles include **slivers**, observations from a neighbouring path
  that only clip the tile edge (`fillPercent` near 100,
  `validFraction` near 0). They are real observations; skip them if your
  method needs a minimum coverage.
- `eo:cloud_cover`: the catalog's scene-level cloud percentage, for quick
  pre-selection; use QA for per-pixel decisions.
- `manifest.dataset.complete` is `true` only when nothing is pending or
  failed. `manifest.coverage.counts` gives the totals as of the last
  catalog write; `ds.status()` gives them live.

A dataset can contain **two acquired rows for one acquisition** if the
provider later re-releases it under a new id (a USGS reprocessing changes
the processing date inside the ARD id). Today DataLoader keeps both; if
your method needs one per date, group by `sensor`, `grid`, `platform` and
`datetime`, and keep the row with the latest
`provenance.generation_time`.

## 7. Reading an archive that is still downloading

You can read while a run is writing:

- Files are written under a temporary name (`*.tif.tmp`) and renamed when
  complete; a `.tif` that exists is complete. Ignore `*.tmp`.
- A row becomes `acquired` only after its file is complete and
  checksummed. `open_dataset` (and `data-loader status`) merge
  `items.jsonl` with the run's journal (`.state/journal.jsonl`), so they
  see acquisitions within seconds.
- `items.jsonl` and `manifest.json` themselves are rewritten every 200
  acquisitions or 10 minutes, atomically. A reader that reads them alone
  (without the journal) sees a consistent but slightly older state.
- At the moment the catalog is rewritten, a reader can very rarely miss
  the most recent rows; they reappear on the next read. Treat a listing as
  "at least what was there".
- Practical pattern for a pipeline that processes tiles as they finish:
  `ds.status()["byGrid"]["landsat/h003v004"]` gives a tile's counts by
  status; process a tile once it has no `pending`. A run works through
  one tile at a time (including its retries), so tiles complete in order.
- Never write into the dataset directory. Write your products elsewhere
  and record the dataset path, `manifest.dataset.updated` and the
  `itemId`s you used.

## 8. Provenance you can cite

- `item.provenance.upstream_product_id`: the agency's product id (for
  Sentinel-2 the full ESA product name, including processing time).
- `item.provenance.processing_baseline` (Sentinel-2), `generation_time`,
  `collection`, `platform`.
- `item.sourceMetadata`: path to the provider's complete original
  metadata record (STAC item) for that acquisition, kept verbatim.
- `item.software`: DataLoader version and git commit that wrote the row;
  `manifest.dataset.runs`: every run into the dataset.
- `files[].sha256`: checksum of each file; `data-loader verify DIR`
  re-checks them.

## 9. The Oregon Landsat ARD archive

`/vol/v1/data/oregon_landsat_ard` on the eMapR server
(`examples/oregon_landsat_ard_archive.yaml`):

- 23 ARD tiles touching Oregon, 1990 to present (open-ended: re-running the
  same command adds new acquisitions), Landsat 4, 5, 7, 8 and 9.
- Native encoding: uint16 `blue green red nir swir1 swir2 qa_pixel`,
  no masking, no resampling.
- About 88,900 acquisitions, about 8.3 TB.
- Status: `data-loader status /vol/v1/data/oregon_landsat_ard`.

## 10. Running DataLoader from your own code

Prefer a config file plus the command line (`data-loader run CONFIG`) so
the request is recorded. From Python:

```python
from data_loader import load_config, run, open_dataset

summary = run(load_config("my_request.yaml"))   # resumable; same as `data-loader run`
ds = open_dataset(summary.output_dir)
```

`run` raises `ConfigError` for an invalid request, `DatasetError` if the
output directory holds a different dataset or is locked by another run,
and `RunStopped` (exit code 5 on the command line) if it stopped itself
because of a disk, credential or prolonged source problem; re-running
resumes in every case.
