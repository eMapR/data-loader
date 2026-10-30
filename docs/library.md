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
| Imagery library | tile datasets + product specs + catalog | storage volume, e.g. `/vol/v1/Landsat-ARD/collection_2/...` | maintainers only (via DataLoader / `imagery-library`) |
| Catalog | GeoParquet index, generated from the tile datasets | `<root>/catalog/` | rebuilt by maintainers; never edited |
| Project configurations | AOIs and what each project needs | this repo, `aois/` | project owners |

The library code (`imagery_library/`) reads DataLoader's output contract
(manifest.json, items.jsonl) and builds DataLoader configs; it never calls
engine internals. Projects hold no imagery and no acquisition settings.

## Layout

Layout 2 (decisions and reasons: [library-architecture.md](library-architecture.md)).
One root per product family; on Iowa: `/vol/v1/Landsat-ARD`.

```
Landsat-ARD/
  README.md                                     the dataset's guide (library/landsat_ard/README.md in the repo)
  LIBRARY.yaml                                  name, maintainers, contact, layout_version 2, permissions
  collection_2/                                 product version
    PRODUCT.yaml                                the product spec (library/landsat_ard/collection_2/ in the repo)
    published_tiles.json                        tiles USGS publishes, per grid (ocean/foreign positions are not)
    conus/h003v004/                             grid dir / tile = one ordinary DataLoader dataset
      manifest.json  items.jsonl  request.yaml  metadata/  landsat/h003v004/<year>/*.tif
      LINEAGE.json                              only for tiles imported from an older archive
    alaska/AK_h016v005/                         Alaska and Hawaii ids keep their AK_/HI_ prefix
    hawaii/
  catalog/
    tiles.parquet -> current/tiles.parquet      stable names: GeoParquet, one row per tile, footprint in EPSG:4326
    observations.parquet -> current/...         one row per acquired observation
    catalog.json -> current/catalog.json        build summary (counts, problems, builder commit)
    current -> builds/20261026T185339877200Z/   atomic switch to the latest build
    builds/<id>/                                every build (the last 3, and any under an hour old)
  staging/<user>/                               maintainers' work in progress: new tiles, imports (never catalogued)
  admin/
    imports.jsonl                               every import (what, from where, by which code)
    acquisitions.jsonl                          every `imagery-library acquire` run (tile, outcome, time)
    verification.jsonl                          every verify (tile, time, ok, files, checksums)
    verified/<tile path>.json                   exactly which observations the last verify covered
    locks/usgs_m2m-<hash>.lock                  DataLoader's account lock (DATALOADER_LOCK_DIR points here)
    configs/, logs/                             generated per-tile DataLoader configs and their run logs
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

### Grids and missing tiles

USGS ARD has three regional grids (CONUS, Alaska, Hawaii), each numbered
from h000v000, so the same h/v is a different place on each; tile ids are
region-qualified (`AK_h016v005`; bare = CONUS) and every spelling resolves
to one canonical id and one directory (`CU_h003v004` -> `h003v004`).
Measured 2026-10-26 (`bench/ard_grid_overlap.py`,
`bench/ard_published_tiles.py`): within the h/v ranges USGS publishes, the
grids do not overlap on the ground (closest published CONUS and Alaska
tiles are 2.65° apart), so an AOI is never offered the same ground on two
grids. But only 547 of 726 CONUS, 218 of 364 Alaska and 14 of 15 Hawaii
grid positions have data (the rest are ocean or outside the US), and areas
such as central British Columbia lie on no grid. Queries therefore report
three things separately: tiles the library holds, tiles USGS publishes that
the library lacks (**missing**: ask a maintainer), and the share of the AOI
**outside the product** (nothing to acquire). The published list lives
with the product (`published_tiles.json`); re-run the bench script when
USGS adds tiles. Three Aleutian tiles cross ±180°; their footprints are
split into two polygons.

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
AOI intersects 2 library tile(s): conus/h003v003 (63.5%), conus/h004v003 (36.5%)
497 observations, 0.047 TB, 2000-06-05 .. 2005-09-25
AOI covered by library tiles: 100.0%
```

`imagery-library tiles ROOT` lists every tile with observation counts,
date range and both states.

### Tile and observation states

Every tile in a query result (and the catalog) has two states:

| acquisition | meaning |
|---|---|
| `complete` | everything USGS has published (as of `discovered_through`) is acquired |
| `incomplete` | some observations failed or are pending (`failed_example` says why). Older processing versions USGS has replaced are `superseded`, not failed, and do not make a tile incomplete |
| `updating` | a DataLoader run is writing the tile right now; newest observations appear after the next catalog build |

| verification | meaning |
|---|---|
| `verified` | the last checksum verify passed and covered every observation |
| `partially_verified` | it passed, but observations were added since (they are `verified = False`) |
| `failed` | the last verify found missing or corrupt files |
| `unverified` | never verified with checksums (or imagery not present on this storage) |

Each observation has `verified` (True only if the last passing checksum
verify checked that exact row version). `verified_only=True` (query file:
`verified_only: true`) returns only those, and says how many it left out. Each GeoTIFF's band
order, scale/offset, nodata and QA meaning are in its tile's
`manifest.json` (`bandSets`), as for any DataLoader dataset.

## Maintaining the library

```
imagery-library init /vol/v1/Landsat-ARD --name ... --description ... --maintainer ... \
    --readme library/landsat_ard/README.md [--group <maintainers' group>]
imagery-library add-product /vol/v1/Landsat-ARD library/landsat_ard/collection_2/PRODUCT.yaml

# a project needs an AOI: which tiles, which are already held?
imagery-library acquire-config ROOT --aoi aois/usfs_r10_tanana_river.geojson
# acquire the missing ones, one tile at a time, queued behind any other run on the account (run in tmux);
# each new tile is written to staging/<you>/, checksum-verified, then moved into collection_2/<grid>/
imagery-library acquire ROOT --aoi aois/usfs_r10_tanana_river.geojson
# a tile staged by a manual `data-loader run` of an acquire-config config: verify and move it into place
imagery-library promote ROOT --tile AK_h016v005
# updates (newly published observations) for held tiles
imagery-library acquire ROOT --tile AK_h016v005 --include-held

imagery-library verify ROOT [--tile T]      # DataLoader verify per tile (skips tiles being written)
imagery-library permissions ROOT [--fix]    # check / apply the permission policy (LIBRARY.yaml permissions:)
imagery-library backup-list ROOT --tier metadata|irreplaceable|imagery --out LIST   # see library-backup-policy.md
imagery-library catalog ROOT                # rebuild after runs / verifies
```

### Concurrency: what is guaranteed

- **No duplicate tiles.** A tile has exactly one directory (version /
  grid / canonical id) and at most one staging directory per maintainer; a
  tile another maintainer is staging is skipped. `acquire` and `acquire-config` list tiles
  already held instead of acquiring them, and the importer refuses a tile
  that exists with different content.
- **One writer per tile.** DataLoader holds an exclusive lock on the tile
  directory for the whole run; a second run on the same tile (from any
  maintainer or machine) is refused at once.
- **One acquisition per USGS account, enforced by DataLoader.** Every
  `data-loader run` holds a lock for its USGS account (DataLoader 1.2; a
  second run exits with code 6 or, with `--wait-for-account`, queues).
  `acquire` runs DataLoader with `DATALOADER_LOCK_DIR=<library>/admin/locks`
  and `--wait-for-account` (`--no-wait`: skip and report); maintainers put
  the same `DATALOADER_LOCK_DIR` in the env file that holds the account's
  credentials, so direct runs share the lock. Locks are `flock`; on NFSv4
  they are held on the server, so they work across machines.
- **Permissions are applied, not hoped for.** Every maintainer command
  brings what it wrote to the policy (read-only imagery, maintainers-only
  admin and staging); `imagery-library permissions ROOT` audits the whole
  library. Files that also have a name outside the library (a hard-link
  migration in progress) are left exactly as they are and counted as
  `shared`; they are sealed once the old names are retired.
- **Nothing half-finished is catalogued.** New tiles live in `staging/`
  until they are complete and checksum-verified; updates to held tiles
  show as `updating`.
- **Readers never see partial state.** DataLoader writes each GeoTIFF to a
  temp name, fsyncs, renames, and only then records it; the catalog is built
  into a new directory and `catalog/current` is switched with one atomic
  rename; a query resolves `current` once and reads only that build. Old
  builds are kept for at least an hour (the current one always).
- **Catalog builds during acquisitions are consistent.** One build at a
  time (`catalog/.build.lock`). A tile being written is read journal first,
  then items.jsonl, keeping the newer row per observation, so a snapshot
  DataLoader writes in between cannot drop rows; the tile is marked
  `updating`.
- **Verify skips tiles being written** and records exactly which
  observations it checked, so states never depend on clock comparisons.

Validated on real data 2026-10-26 (development report): a second acquire
request for the same tile was skipped (account busy), a direct
`data-loader run` on it was refused (tile locked), verify skipped it, and a
catalog build and query during the run showed it as `updating` with its
first observations `unverified`.

## Migrating existing archives

`imagery-library import ROOT ARCHIVE --mode hardlink` splits a
project-named, multi-tile archive into tile datasets:

- item rows are copied verbatim; each tile gets a manifest narrowed to that
  tile and a request whose `aoi.tiles` is that tile, so `data-loader run`
  resumes it without re-downloading anything;
- imagery and source metadata are hard-linked (same filesystem: no copy, no
  extra space) or copied (`--mode copy`, across filesystems);
  `--mode metadata` brings metadata only (for dry runs);
- each tile is assembled in the maintainer's `staging/<user>/` and renamed
  into place when complete; `LINEAGE.json` records the source archive, its
  original request and a fingerprint of its manifest/items;
- the source is only read: every listed file is stat-checked first,
  manifest/items are hashed before and after, and the importer refuses a
  tile whose product spec differs from the product or that is already in
  the library with different content. `--dry-run` writes nothing.

Phased plan for production: [library-migration-checklist.md](library-migration-checklist.md).
