# Outputs: the dataset contract

Every DataLoader run writes a **dataset**: a directory that describes
itself. Downstream programs should read `manifest.json` and `items.jsonl`
rather than parse filenames. Filenames are readable, but they are not the
contract.

```
<output.dir>/
  manifest.json                 dataset-level description (schema "dataloader-manifest", version 1.0)
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
  "schemaVersion": "1.0",
  "dataset": {
    "created": "2026-10-08T...", "updated": "...",
    "complete": true,                 // nothing pending or failed
    "dataloaderVersion": "1.0.0", "gitCommit": "..."
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
    "fill": "source fill (SR DN 0) is nodata in every encoding",
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
         "nodata": null,
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
     "bandSet": "bands", "bytes": 3365204, "sha256": "..."}
    // Sentinel-2 native files also carry "bandScaleOffset": {"red": [0.0001, -0.1], ...}
  ],
  "provenance": {                     // normalized, provider-independent
    "provider": "usgs_ard", "provider_item_id": "...", "upstream_product_id": "...",
    "acquisition_datetime": "...", "platform": "landsat-5", "product_family": "usgs_ard_sr",
    "collection": "landsat-c2ard-sr", "processing_baseline": null, "generation_time": "20210423",
    "extra": {"grid_horizontal": "03", "grid_vertical": "04", "scene_count": 2, "fill_percent": 85.9, ...},
    "source_metadata_ref": "metadata/usgs_ard/LT05_CU_003004_19900102_20210423_02_SR.json"
  },
  "sourceMetadata": "metadata/usgs_ard/LT05_CU_003004_19900102_20210423_02_SR.json",
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
| `failed` | Every attempt so far failed. `error` holds the last message and `attempts` the count. A run retries failures until `max_attempts` in total; a later run continues if attempts remain. |
| `pending` | Discovered but not acquired yet: the run was interrupted, or is still in progress. |
| `filtered` | Excluded by `filters.max_cloud_percent`. `reason` says why. Nothing is written. |

So `items.jsonl` answers "what exists for this request, and what did we
get?". Missing and failed observations are explicit, not just absent.

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
    refl = dn[:len(sr)].astype("float32") * sr[0]["scale"] + sr[0]["offset"]
    refl[dn[:len(sr)] == 0] = np.nan                       # nodata / fill
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
| `native` | provider integers (uint16 for Landsat C2 / Sentinel-2) | `0` | `value * scale + offset` (`bandSets`; per file for Sentinel-2) |

Indices are always float32 with `NaN` nodata. Source fill is nodata in
both encodings. With `pixel_cloud_mask: true`, masked pixels are nodata
too. The QA band is never masked.

## Compatibility promise

- The manifest carries `schemaVersion`. Fields may be **added** within 1.x.
  A change that removes or renames a field, or changes its meaning,
  bumps the major version, and `open_dataset` refuses a major version it
  doesn't know.
- Item `key`s and file paths of an existing dataset never change on
  re-runs. A re-run adds rows and updates `status`, `attempts` and
  `updated`.
- The field names follow STAC where an equivalent exists (`datetime`,
  `platform`, `eo:cloud_cover`, scale/offset/nodata), so exporting to STAC
  or GeoParquet later is a mechanical conversion.
