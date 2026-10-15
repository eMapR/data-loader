# Outputs: the dataset contract

Every DataLoader run writes a **dataset**: a directory that describes
itself. Downstream programs should read `manifest.json` and `items.jsonl`
rather than parse filenames. Filenames are readable, but they are not the
contract.

```
<output.dir>/
  manifest.json                 dataset-level description (schema "dataloader-manifest", version 1.1)
  items.jsonl                   one JSON object per line: every acquisition (or composite) and its status
  request.yaml                  the resolved request that produced the dataset
  metadata/<provider>/<id>.json the provider's original metadata record for each source item, verbatim
  <sensor>/<grid>/<YYYY>/<date>_<item-id>_bands.tif      scene mode
  <sensor>/<grid>/<YYYY>/<date>_<item-id>_indices.tif    scene mode, if indices were requested
  <sensor>/<grid>/composites/<seasonYear>_<reduce>_bands.tif     annual_composite mode
  .state/                       run bookkeeping (journal, lock); not part of the contract
```

`<grid>` is the tile ID (`h003v004`) for tile requests and `aoi` for a
bbox.

## `manifest.json`

```jsonc
{
  "schema": "dataloader-manifest",
  "schemaVersion": "1.1",
  "dataset": {
    "created": "2026-10-08T...", "updated": "...",
    "complete": true,                 // nothing pending or failed
    "dataloaderVersion": "1.1.0", "gitCommit": "...", "gitDirty": false,   // software of the latest run
    "runs": [                         // every run into this directory, oldest first (1.1)
      {"started": "...", "finished": "...", "dataloaderVersion": "1.1.0", "gitCommit": "...",
       "gitDirty": false, "acquired": 812, "failed": 0}
      // "finished": null and no counts: the run was killed; "interrupted": true: stopped with Ctrl-C
    ],
    "upgrades": [{"at": "...", "from": "1.0", "to": "1.1", ...}]   // only if `data-loader upgrade` ran
  },
  "request": { ... },                 // the full resolved config (see request.yaml)
  "products": {
    "landsat": {"sensor": "landsat", "provider": "usgs_ard", "productFamily": "usgs_ard_sr",
                "temporalProduct": "scene", "processingVersionPolicy": "any"}
  },
  "processing": {
    "temporalMode": "scene", "reducer": null, "encoding": "native",
    "sceneCloudFilter": {"maxCloudPercent": 100.0, "note": "..."},
    "pixelCloudMask": {"applied": false, "note": "no cloud/shadow masking: every source pixel is kept; ..."},
    //  or {"applied": true, "rules": {"landsat": {"qaBand": "qa_pixel", "rule": "masked if any listed bit is set",
    //                                              "bits": {"1": "dilated_cloud", "2": "cirrus", "3": "cloud", "4": "cloud_shadow"}}}}
    "fill": "source fill is the file's nodata value in every encoding ...",
    "resampling": "none: read on the provider's native tile grid",
    "normalizationApplied": [],
    "providerProfile": {"landsat": {...}}   // provider facts: scale/offset, access path, native grid
  },
  "bandSets": {
    "landsat": {
      "bands": [
        {"index": 1, "name": "blue", "role": "reflectance", "dataType": "uint16",
         "scale": 2.75e-05, "offset": -0.2, "nodata": 0,
         "unit": "surface reflectance = value * scale + offset"},
        ...
        {"index": 7, "name": "qa_pixel", "role": "qa", "dataType": "uint16", "scale": 1.0, "offset": 0.0,
         "nodata": 0,                 // the file's nodata (GeoTIFF has one per file); see "QA bands" below
         "qa": {"name": "qa_pixel", "encoding": "bitmask", "reference": "USGS Landsat Collection 2 Level-2 QA_PIXEL",
                "bits": {"0": "fill", "1": "dilated_cloud", "2": "cirrus", "3": "cloud", "4": "cloud_shadow",
                         "5": "snow", "6": "clear", "7": "water", "8-9": "cloud_confidence", ...}}}
      ],
      "indices": []                   // float32 index bands, if requested
    }
  },
  "grids": {
    "landsat": {
      "h003v004": {"kind": "tile", "tile": {"system": "usgs_ard_conus", "id": "h003v004", "h": 3, "v": 4, ...},
                   "crsWkt": "...", "epsg": null, "resolutionM": 30.0,
                   "transform": [30, 0, -2115585, 0, -30, 2714805], "width": 5000, "height": 5000}
      // bbox requests: "aoi": {"kind": "aoi", "crs": "EPSG:32610", "aoi": {"upperLeft": [...], "lowerRight": [...]}, ...}
    }
  },
  "coverage": {
    "timeWindows": [{"seasonYear": 1990, "start": "1990-01-01", "end": "1990-12-31"}, ...],
    "openEnded": true, "discoveredThrough": "2026-10-08",
    "counts": {"acquired": 88931, "failed": 1, "pending": 0, "filtered": 0},
    "firstAcquired": "1990-01-02", "lastAcquired": "2026-10-05", "bytes": 4.6e12,
    "excludedProcessingVersions": {}
  },
  "items": "items.jsonl"
}
```

The band order in each file is the `bandSets` order. The same
information is also in each GeoTIFF: band descriptions are the band
names, the standard GDAL scale/offset band metadata is set, and dataset
tags carry `DATALOADER_ITEM_ID`, `DATALOADER_DATE`, `DATALOADER_SENSOR`,
`DATALOADER_PROVIDER` and `DATALOADER_GRID`.

## `items.jsonl`

One JSON object per line, one per acquisition per grid (or per composite
per grid), sorted by sensor, grid and date. A **scene** row:

```jsonc
{
  "key": "landsat/h003v004/LT05_CU_003004_19900102_20210423_02_SR",   // stable unique id
  "type": "scene",
  "status": "acquired",               // acquired | failed | pending | filtered
  "sensor": "landsat", "grid": "h003v004", "seasonYear": 1990,
  "date": "1990-01-02", "datetime": "1990-01-02T18:21:03.517000Z",
  "itemId": "LT05_CU_003004_19900102_20210423_02_SR",   // provider item id
  "platform": "landsat-5",
  "eo:cloud_cover": 78.6,             // catalog scene cloud cover, %
  "fillPercent": 85.9,                // catalog fill %, where the provider reports it (ARD)
  "validFraction": 0.1394,            // fraction of pixels that are not nodata in the first band
  "files": [
    {"path": "landsat/h003v004/1990/1990-01-02_LT05_CU_003004_19900102_20210423_02_SR_bands.tif",
     "bandSet": "bands", "bytes": 3365204, "sha256": "...",
     "bandScaleOffset": {"blue": [2.75e-05, -0.2], ..., "qa_pixel": [1.0, 0.0]}}   // authoritative, per band
  ],
  "provenance": {                     // normalized, provider-independent
    "provider": "usgs_ard", "provider_item_id": "...", "upstream_product_id": "...",
    "acquisition_datetime": "...", "platform": "landsat-5", "product_family": "usgs_ard_sr",
    "collection": "landsat-c2ard-sr", "processing_baseline": null, "generation_time": "20210423",
    "extra": {"grid_horizontal": "03", "grid_vertical": "04", "scene_count": 2, "fill_percent": 85.9, ...},
    "source_metadata_ref": "metadata/usgs_ard/LT05_CU_003004_19900102_20210423_02_SR.json"
  },
  "sourceMetadata": "metadata/usgs_ard/LT05_CU_003004_19900102_20210423_02_SR.json",
  "software": {"dataloaderVersion": "1.1.0", "gitCommit": "...", "gitDirty": false},   // what wrote this row
  "attempts": 1, "updated": "...",
  "timing": {"readS": 11.2, "totalS": 19.8}
}
```

A **composite** row has `type: "composite"`, `seasonYear`,
`windowStart`/`windowEnd`, `candidateItems` (every scene found for the
window), `sources` (each contributing scene with its provenance and
metadata snapshot), `failedSources`, and `method` (`local` or `provider`).

**Statuses:**

| Status | Meaning |
|---|---|
| `acquired` | Files written and checksummed. |
| `failed` | Every attempt so far failed. `error` holds the last message (URL query strings, which can carry signed tokens, are shown as `?<redacted>`) and `attempts` the count. A run retries failures until `max_attempts` in total; a later run continues if attempts remain. |
| `pending` | Discovered but not acquired yet: the run was interrupted, or is still in progress. |
| `filtered` | Excluded by `filters.max_cloud_percent`. `reason` says why. Nothing is written. |

So `items.jsonl` answers "what exists for this request, and what did we
get?". Missing and failed observations are explicit, not just absent.

**`software`** (1.1) records the DataLoader version and git commit
(`gitDirty`: the source had uncommitted changes; `null` = unknown) that
wrote each acquired or failed row. Rows of a dataset upgraded from 1.0
carry `"attributedBy"` when their software was inferred from the run
history given to `data-loader upgrade`, and have no `software` when that
history was not given.

**Sentinel-2 acquisition identity.** One Sentinel-2 overpass (datatake)
covers many MGRS tiles, and a catalog may list the same granule more than
once at different processing baselines. DataLoader treats (datatake, MGRS
tile) as one acquisition, keeps one version of each per
`processing_version_policy`, and lists the others under
`coverage.excludedProcessingVersions`. `provenance.extra.mgrs_tile` names
the tile; `provenance.upstream_product_id` is the full ESA product name,
which includes the processing time (two catalogs can hold different
processing runs of the same baseline, with slightly different pixels).

## Reading a dataset

```python
from data_loader import open_dataset
import numpy as np, rasterio

ds = open_dataset("/data/oregon_landsat_ard")
bands = ds.manifest["bandSets"]["landsat"]["bands"]        # names, order, dtype, scale/offset, nodata

for path, item, f in ds.files(grid="h003v004", start="2000-06-01", end="2000-09-30"):
    with rasterio.open(path) as src:
        dn = src.read()                                     # (bands, rows, cols), uint16 for native
    sr = [b for b in bands if b["role"] == "reflectance"]
    so = f["bandScaleOffset"]                               # per file: authoritative (varies for Sentinel-2)
    refl = np.stack([dn[i].astype("float32") * so[b["name"]][0] + so[b["name"]][1]
                     for i, b in enumerate(sr)])
    refl[dn[:len(sr)] == sr[0]["nodata"]] = np.nan         # nodata / fill
    qa = dn[-1]                                            # qa_pixel (if qa_band: true)
    cloudy = (qa & (1 << 3 | 1 << 4 | 1 << 1 | 1 << 2)) != 0   # cloud, shadow, dilated cloud, cirrus
    refl[:, cloudy] = np.nan
```

`ds.items(status=None)` yields every row, whatever its status.
`ds.status()` gives counts by status, grid and season year. `ds.verify()`
checks every file's size and sha256. From the shell, use `data-loader
status DIR` and `data-loader verify DIR`. All of these also see a run in
progress, because they read the journal in `.state/`.

Without the Python package, read `manifest.json` and `items.jsonl` with
any JSON library; the GeoTIFFs are standard.

## Encodings and nodata

| `output.encoding` | Bands | nodata | To reflectance |
|---|---|---|---|
| `float32` | float32 | `NaN` | already reflectance |
| `native` | provider integers (uint16 for Landsat C2 / Sentinel-2) | `0` | `value * scale + offset` with the file's `bandScaleOffset` |

Indices are always float32 with `NaN` nodata. Source fill is nodata in
both encodings. With `pixel_cloud_mask: true`, masked pixels are nodata
too. The QA band is never masked.

**Which scale/offset to use.** `files[].bandScaleOffset` is authoritative
for that file; the same values are in the GeoTIFF's GDAL band metadata.
The `bandSets` scale/offset is the provider's usual value and is right for
Landsat, but **not** for every Sentinel-2 file: ESA baseline 04.00+
(2022 onward) needs offset −0.1 where the catalog serves ESA's DN
unchanged (Planetary Computer, CDSE), while Earth Search has already
removed it. Always apply the per-file values. Datasets written by 1.0
recorded `bandScaleOffset` only for bands that differed from `bandSets`;
`data-loader upgrade` fills in the rest.

**QA bands and nodata.** A GeoTIFF has one nodata value per file, so the
QA band carries the file's nodata too (`0` for native, `NaN` for float32;
1.0 manifests said `null`). Don't use the nodata mask on QA bands: decide
fill and clouds from the QA meaning in `bandSets` (Landsat QA_PIXEL bit 0 =
fill; Sentinel-2 SCL class 0 = no data). Landsat surface reflectance can
also be DN 0 at a non-fill pixel (very dark water or shadow, clamped by
the source), which the nodata mask treats as missing; QA bit 0 tells the
two apart.

## Compatibility promise and upgrading

- The manifest carries `schemaVersion`. Fields may be **added** within 1.x.
  A change that removes or renames a field, or changes its meaning,
  bumps the major version, and `open_dataset` refuses a major version it
  doesn't know.
- A 1.0 dataset is read as is by 1.1 (`open_dataset` accepts any 1.x).
  Running 1.1 into it upgrades its manifest on the next catalog write. To
  upgrade without a run, and to fill in per-file scale/offset and the
  run history for rows written by 1.0, use `data-loader upgrade DIR`
  (metadata only; no imagery is read or rewritten except to spot-check
  with `--check-files N`). It takes the dataset lock, so it cannot run
  while a run is writing the directory; `--dry-run` reports what would
  change. `--run START=COMMIT` (repeatable) gives the software history:
  rows written after START are attributed to COMMIT.
- A DataLoader refuses to write into a dataset whose major version it
  doesn't know, instead of rewriting it as its own version.
- Item `key`s and file paths of an existing dataset never change on
  re-runs. A re-run adds rows and updates `status`, `attempts` and
  `updated`.
- The field names follow STAC where an equivalent exists (`datetime`,
  `platform`, `eo:cloud_cover`, scale/offset/nodata), so exporting to STAC
  or GeoParquet later is a mechanical conversion.
