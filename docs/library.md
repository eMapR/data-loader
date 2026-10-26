# The imagery library

A shared, read-only store of satellite imagery for eMapR and other OSU
researchers, organised by **what the data is** (product, version, grid,
tile) rather than by the project that first paid for it. Researchers find
imagery with an AOI and a date range; they never need tile ids or project
names. Maintainers acquire and update it with DataLoader.

Status: **proof of concept** (`imagery_library/`, branch `imagery-library`).
Production archives have not been migrated; see
[the migration plan](#migrating-existing-archives) and
[storage requirements](library-storage-requirements.md).

## Four separate parts

| Part | What | Where | Who changes it |
|---|---|---|---|
| Acquisition engine | DataLoader (`data_loader/`): downloads, verifies sources, writes one dataset per tile | this repo | developers |
| Imagery library | tile datasets + product specs + catalog | storage volume, e.g. `<root>/products/...` | maintainers only (via DataLoader / `imagery-library`) |
| Catalog | GeoParquet index, generated from the tile datasets | `<root>/catalog/current/` | rebuilt by maintainers; never edited |
| Project configurations | AOIs and what each project needs | this repo, `aois/` | project owners |

The library code (`imagery_library/`) reads DataLoader's output contract
(manifest.json, items.jsonl) and builds DataLoader configs; it never calls
engine internals. Projects hold no imagery and no acquisition settings.

## Layout

```
<root>/
  LIBRARY.yaml                                  name, maintainers, contact, layout_version
  products/
    landsat_ard_sr/c2/                          product / version
      PRODUCT.yaml                              the product spec (copy of library/products/... in the repo)
      usgs_ard_conus/h003v004/                  grid system / tile = one ordinary DataLoader dataset
        manifest.json  items.jsonl  request.yaml  metadata/  landsat/h003v004/<year>/*.tif
        LINEAGE.json                            only for tiles imported from an older archive
      usgs_ard_alaska/AK_h016v005/
  catalog/
    current -> 20261026T185339877200Z/          atomic switch to the latest build
    20261026T185339877200Z/tiles.parquet        GeoParquet: one row per tile, footprint in EPSG:4326
                          observations.parquet  one row per acquired observation
                          catalog.json          build summary (counts, problems, builder commit)
  admin/
    imports.jsonl                               every import (what, from where, by which code)
    verification.jsonl                          every verify (tile, time, ok, files, checksums)
```

A tile directory is exactly what `data-loader run` writes for a config
whose `aoi.tiles` is that one tile, so DataLoader resumes, updates and
verifies it with no library code involved.

### Products and versions

A product is defined once in `PRODUCT.yaml`: the DataLoader request
every tile of it is acquired with (everything but `aoi` and `output.dir`).
Each tile's spec hash (DataLoader's request identity minus the tile list)
must match the product's; the catalog leaves out any tile that doesn't,
and the importer refuses one. Changing anything that is part of a dataset's
identity (bands, encoding, masking, cloud filter, start of the time range,
product family) is a **new product version** in a new directory, never an
edit. Extending the end of the time range is not a change: re-running a
tile adds newly published observations.

USGS reprocessing versions of individual observations are tracked per
observation (`processing_date`, `upstream_product_id` in the catalog;
full provenance in items.jsonl), not as product versions.

Adding another product (e.g. Sentinel-2 L2A) is a new `PRODUCT.yaml` with
its own grid system (e.g. `mgrs`). Footprints come from each manifest's
own grid record, so the catalog and queries work unchanged; only naming
*missing* tiles needs a small grid helper in `imagery_library/grids.py`
(and DataLoader needs tile-mode support for that provider).

## Finding imagery (researchers)

Python:

```python
from imagery_library import open_library

lib = open_library("/path/to/library")
r = lib.query("my_aoi.geojson", start="2000-06-01", end="2020-09-30",
              platforms=["landsat-5", "landsat-7"], months=[6, 7, 8, 9])
print(r.summary())
r.tiles          # GeoDataFrame: tiles the AOI touches, aoi_share, completeness, verification
r.observations   # DataFrame: date, platform, cloud %, valid fraction, processing date, path, sha256
r.paths()        # absolute GeoTIFF paths
r.missing        # tiles the AOI needs that the library does not hold yet
```

The AOI may be any vector file with a CRS, a GeoDataFrame, a lon/lat
shapely geometry, or a `(west, south, east, north)` bbox. Other filters:
`product`, `version`, `max_cloud_percent`, `min_valid_fraction`.

Command line, with a query file (strict keys, like DataLoader configs):

```yaml
# gorge_summers.yaml
aoi: gorge.geojson
start: 2000-06-01
end: 2005-09-30
months: [6, 7, 8, 9]
platforms: [landsat-5, landsat-7]
```

```
$ imagery-library query /path/to/library gorge_summers.yaml --out files.csv
AOI intersects 2 library tile(s): usgs_ard_conus/h003v003 (63.5%), usgs_ard_conus/h004v003 (36.5%)
497 observations, 0.047 TB, 2000-06-05 .. 2005-09-25
AOI covered by library tiles: 100.0%
```

`imagery-library tiles ROOT` lists every tile with observation counts,
date range, completeness and last verification. Each GeoTIFF's band
order, scale/offset, nodata and QA meaning are in its tile's
`manifest.json` (`bandSets`), as for any DataLoader dataset.

## Maintaining the library

```
imagery-library init ROOT --name ... --description ... --maintainer ...
imagery-library add-product ROOT library/products/landsat_ard_sr/c2/PRODUCT.yaml

# a project needs an AOI: which tiles, which are already held?
imagery-library acquire-config ROOT --aoi aois/usfs_r10_tanana_river.geojson --out-dir cfgs/
# -> one DataLoader config per tile the library lacks (held tiles are listed, not re-acquired)
data-loader run cfgs/landsat_ard_sr_c2_usgs_ard_alaska_AK_h016v005.yaml   # one tile at a time, in tmux

# updates: re-run a held tile's config (--include-held writes those too)
imagery-library verify ROOT [--tile T]      # DataLoader verify per tile, logged to admin/verification.jsonl
imagery-library catalog ROOT                # rebuild after runs / verifies
```

Rules: one DataLoader run per USGS M2M account at a time; one writer per
tile (DataLoader's lock enforces it); rebuild the catalog after each run.

### Concurrent access

Researchers read through `catalog/current`, which switches atomically to
a complete new build, and DataLoader writes each GeoTIFF to a temp file
renamed into place only after fsync. A researcher therefore never sees a
partial file or a half-written catalog; a tile being updated simply shows
its new observations after the next catalog build. The catalog records
`run_in_progress` per tile.

## Migrating existing archives

`imagery-library import ROOT ARCHIVE --mode hardlink` splits a
project-named, multi-tile archive into tile datasets:

- item rows are copied verbatim; each tile gets a manifest narrowed to that
  tile and a request whose `aoi.tiles` is that tile, so `data-loader run`
  resumes it without re-downloading anything;
- imagery and source metadata are hard-linked (same filesystem: no copy, no
  extra space) or copied (`--mode copy`, across filesystems);
  `--mode metadata` brings metadata only (for dry runs);
- each tile is assembled in a hidden staging directory and renamed into
  place when complete; `LINEAGE.json` records the source archive, its
  original request and a fingerprint of its manifest/items;
- the source is only read: every listed file is stat-checked first,
  manifest/items are hashed before and after, and the importer refuses a
  tile whose product spec differs from the product or that is already in
  the library with different content. `--dry-run` writes nothing.

Phased plan for production: see the proposal and
`docs/library-storage-requirements.md`. Nothing is migrated until the
storage location and permissions are agreed.
