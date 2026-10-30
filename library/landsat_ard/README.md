# Landsat-ARD: USGS Landsat Collection 2 ARD surface reflectance

A shared research archive of every Landsat 4-9 observation that USGS
publishes on its U.S. Analysis Ready Data (ARD) tile grids, kept exactly as
USGS distributes it. It is maintained by eMapR (Oregon State University)
for eMapR and other researchers, and belongs to no single project.

Use it for time series, change detection, compositing, training data, or
any work that needs Landsat surface reflectance over the U.S. without
downloading it yourself. **Researchers read; maintainers write.** Never
edit, move or delete files here. Copy what you need, or read it in place.

## What is in it

| | |
|---|---|
| Product | USGS Landsat Collection 2 ARD surface reflectance (`landsat-c2ard-sr`) |
| Sensors | Landsat 4-5 TM, 7 ETM+, 8-9 OLI |
| Period | 1990 to the latest update (see `catalog/catalog.json`) |
| Bands | blue, green, red, nir, swir1, swir2 (uint16 DN) + QA_PIXEL (bitmask), one GeoTIFF per observation |
| Reflectance | `DN * 2.75e-05 - 0.2`, recorded per file in the catalog and manifests. DN 0 = no data |
| Processing | None. No cloud filter, no pixel masking, no resampling, no reprojection. Cloud handling is yours: QA_PIXEL is in every file |
| Integrity | Each source file is SHA-512 checked against the USGS record before it is written. Each written file has a SHA-256 in the catalog |

## Layout

```
Landsat-ARD/
  README.md                 this file
  LIBRARY.yaml              machine-readable description (maintainers, permissions)
  collection_2/             USGS Landsat Collection 2 (product spec: collection_2/PRODUCT.yaml)
    conus/                  CONUS ARD grid
      h003v004/             one tile = one dataset (see "Inside a tile")
    alaska/                 Alaska ARD grid
      AK_h021v008/
    hawaii/                 Hawaii ARD grid
      HI_h001v001/
  catalog/
    tiles.parquet           one row per tile: footprint, counts, dates, state
    observations.parquet    one row per observation: date, platform, cloud %, file path, checksum
    catalog.json            summary of the current build
  staging/                  maintainers' work in progress (not part of the dataset)
  admin/                    maintainers' logs and verification records
```

### Why collection, then grid, then tile

- **Collection** (`collection_2`). USGS reprocesses Landsat as a whole
  from time to time. Collection 1 and Collection 2 values are not
  interchangeable, so a future Collection 3 gets its own folder next to
  this one instead of being mixed in. The folder also fixes what was
  acquired (bands, encoding, no masking, start year). Any change to that
  definition is a new folder, never an edit.
- **Native grid** (`conus`, `alaska`, `hawaii`). The three ARD regions
  are separate map projections. Each numbers its tiles from `h000v000`,
  so the same h/v means a different place on each grid.
- **Tile**. ARD is distributed per 150 x 150 km tile. A tile folder is
  self-contained: its imagery, its full record of where each file came
  from, and what is still missing. A whole tile can be checked, updated or
  copied on its own.
- **Not by project.** Imagery is stored once, by what it is. Projects find
  it by area and date through the catalog, so two projects never hold two
  copies of the same tile.

### The three ARD grids

All three are Albers Equal-Area, WGS84, 30 m pixels, 5000 x 5000-pixel
tiles. `h` counts west to east and `v` north to south from each grid's own
origin.

| Folder | Tile ids | Projection (PROJ) | Grid positions | With data |
|---|---|---|---|---|
| `conus` | `h003v004` (bare = CONUS) | `+proj=aea +lat_0=23 +lon_0=-96 +lat_1=29.5 +lat_2=45.5` | h 0-32, v 0-21 | 547 of 726 |
| `alaska` | `AK_h021v008` | `+proj=aea +lat_0=50 +lon_0=-154 +lat_1=55 +lat_2=65` | h 0-25, v 0-13 | 218 of 364 |
| `hawaii` | `HI_h001v001` | `+proj=aea +lat_0=3 +lon_0=-157 +lat_1=8 +lat_2=18` | h 0-4, v 0-2 | 14 of 15 |

Don't mosaic tiles from different grids without reprojecting. Alaska and
Hawaii tile ids always keep their `AK_`/`HI_` prefix, including in folder
names, so a tile copied elsewhere can't be mistaken for a CONUS tile
(`h021v008` without a prefix is in Illinois). The grids don't overlap on
the ground. Grid positions over ocean or outside the U.S. have no data.
Three Aleutian tiles cross 180°.

### Inside a tile

Each tile folder is a standard DataLoader dataset (manifest schema
`dataloader-manifest` 1.x, see DataLoader's `docs/outputs.md`):

```
collection_2/conus/h003v004/
  manifest.json             what the tile holds: grid, product, counts, software, every run
  items.jsonl               one line per observation: status, files, checksums, USGS ids, provenance
  request.yaml              the exact DataLoader request this tile is acquired with
  LINEAGE.json              only for tiles imported from an older project archive
  landsat/h003v004/<year>/<date>_<USGS product id>_bands.tif
  metadata/usgs_ard/<USGS product id>.json      the USGS STAC record, verbatim
```

Example file:
`landsat/h003v004/1990/1990-08-10_LT05_CU_003004_19900810_20210423_02_SR_bands.tif`
(date, then the USGS product id: sensor, region, h/v, acquisition date,
processing date, collection).

## Finding imagery

Use the catalog. You never need to know tile ids or project names.

```python
from imagery_library import open_library

lib = open_library("/vol/v1/Landsat-ARD")
r = lib.query("my_aoi.geojson", start="2000-06-01", end="2020-09-30",
              platforms=["landsat-5", "landsat-7"], months=[6, 7, 8, 9])
print(r.summary())    # tiles, counts, coverage, anything missing
r.observations        # table: date, platform, cloud %, valid fraction, path, sha256
r.paths()             # GeoTIFF paths to read
```

From the shell:
`imagery-library query /vol/v1/Landsat-ARD my_query.yaml --out files.csv`
(see DataLoader's `docs/library.md`). Any tool that reads GeoParquet
(GeoPandas, QGIS, DuckDB, R `sfarrow`) can open `catalog/tiles.parquet` and
`catalog/observations.parquet` directly. To read both from one consistent
build, open them through `catalog/current/`.

The query reports three things separately:
- tiles the archive holds;
- tiles USGS publishes that the archive lacks (**missing**: ask a maintainer);
- the share of your area with no ARD at all (ocean, outside the U.S.).

Each tile also has two states:
- **acquisition:** `complete`, `incomplete` (some observations failed or are pending) or `updating` (being added to now);
- **verification:** `verified`, `partially_verified` (new observations since the last check), `failed` or `unverified`.

Use `verified_only=True` to read only checksum-verified files.

## How imagery is added and checked (maintainers)

1. **Acquire.** `imagery-library acquire` runs DataLoader for each needed
   tile, one at a time per USGS account. The settings always come from
   `collection_2/PRODUCT.yaml`, never from a project.
2. **Stage.** A new tile is written to `staging/<maintainer>/` first, and
   only moved into `collection_2/<grid>/` when every observation is
   acquired and every file passes a checksum check. An interrupted
   download resumes in staging. Nothing half-finished appears in the
   catalog.
3. **Update.** Tiles already here are updated in place to add newly
   published observations. Nothing already acquired is downloaded again.
   Every file is written under a temporary name and renamed when complete,
   so readers never see a partial file.
4. **Verify.** `imagery-library verify` re-reads every file against its
   recorded SHA-256. Each tile records which observations its last check
   covered.
5. **Catalog.** `imagery-library catalog` rebuilds the catalog from the
   tiles' own records. It reads no imagery and can be rebuilt at any time.

## Versions, provenance and updates

- **Product definition:** `collection_2/PRODUCT.yaml`. Every tile must
  match it, and the catalog leaves out any tile that doesn't.
- **USGS reprocessing:** each observation carries the USGS product id and
  processing date. When USGS replaces an observation with a newer
  processing version, the new one is acquired. The old row is marked
  `superseded`, and its file is kept, because USGS stops distributing it.
- **Per observation** (`items.jsonl`): the verbatim USGS STAC record,
  SHA-512 checks of the source files, the SHA-256 of the written file,
  the DataLoader version and git commit that wrote it, and every failed
  attempt.
- **Per tile** (`manifest.json`): every run (when, which software), what
  was discovered and through which date. Imported tiles also record which
  project archive they came from (`LINEAGE.json`).
- **Maintainer actions** (imports, acquisitions, verifications) are logged
  in `admin/`.

## Who maintains it

| Role | Who |
|---|---|
| Maintainers (acquire, verify, catalog) | clarype (eMapR, OSU). **TODO (D6): other maintainers** |
| Contact for requests and problems | **TODO (D6): contact address** |
| Storage | `/vol/v1` on Iowa (eMapR). Planned move to shared research storage (Thomas) |
| Software | DataLoader and `imagery-library`, https://github.com/eMapR/data-loader |

To request a tile or report a problem, contact a maintainer. The design
and its reasons are written down in DataLoader's
`docs/library-architecture.md`.
