# Study-area AOIs and their ARD tile acquisitions

Four study areas, confirmed 2026-10-20, acquired as **complete native USGS
ARD tiles** with the same product spec as the Oregon archive: uint16 DN +
QA_PIXEL, every observation 1990–present, no cloud filter, no pixel
masking, no resampling. Nothing is clipped to the AOIs; the AOIs only
decide which tiles are needed.

| File | What it is | Made by |
|---|---|---|
| `aois.yaml` | Hand-maintained spec: source boundary file, feature filter, expected count/area | by hand |
| `<id>.geojson` | One dissolved (Multi)Polygon per study area, EPSG:4326 | `bench/prepare_aois.py aois/aois.yaml` |
| `registry.json` | Source path + sha256 of every source sidecar, source CRS, area, validity checks | same |
| `ard_tile_plan.json` | Every ARD tile each AOI intersects (share of AOI, share of tile), observation counts, storage/time estimate, Oregon overlap | `bench/study_area_ard_plan.py` |
| `acquisitions/<id>.yaml` | DataLoader configs, one dataset per study area, only tiles no archive holds yet | from the plan |
| `usfs_r10_northern_se_alaska_valid_coverage.json` | QA_PIXEL-based valid-data coverage of HUC 190103 | `bench/ard_valid_coverage.py` |

The source boundaries live in
`prithvi-reference-assessment/data/raw/boundaries/` and are only read;
their checksums in `registry.json` show whether a source changed since.

## Study areas

| id | Boundary | Tiles |
|---|---|---|
| `nccn_olym_lpa01` | NCCN LPa01 10-mile-buffer study area, `PARK_CODE=OLYM`, 11,476 km² | h003v001, h002v001 + **h003v002 from the Oregon archive** |
| `glkn_slbe_landtrendr` | GLKN LandTrendr analysis area, `park=slbe`, 2,383 km² | h022v005 |
| `usfs_r6_columbia_mtns_wa` | EPA L3 ecoregion 15, Washington portion (BugNet R6), 21,422 km² | h006v002, h006v001, h005v001, h005v002 |
| `usfs_r10_northern_se_alaska` | WBD HUC6 190103 Northern Southeast Alaska, 28,388 km² | AK_h021v008, AK_h021v009, AK_h022v008, AK_h022v009, AK_h020v008, AK_h020v009 (Alaska ARD grid) |

Edge tiles holding only a few percent of an AOI are kept on purpose:
complete coverage over smaller downloads.

**Olympic depends on the Oregon archive.** h003v002 holds 8.3% of the
Olympic AOI (its southeast corner) and is already in
`examples/oregon_landsat_ard_archive.yaml`, so it is not acquired again.
Olympic's complete coverage is the `nccn_olym_lpa01` dataset **plus** the
Oregon archive's h003v002. Use the tile catalog below rather than
hard-coding both paths.

## Shared imagery: tiles, not study areas, own the files

- Imagery is stored once per tile, in a tile-archive dataset (Oregon, or
  the dataset of the study area that first needed the tile). Study areas
  are AOIs; they never get their own copy of a tile.
- A new study area: run `bench/study_area_ard_plan.py` with the existing
  archives' configs to see which tiles are new, and acquire only those.
  DataLoader treats a different tile list as a different dataset, so new
  tiles go into a new dataset rather than into an existing one.
- `bench/ard_tile_catalog.py <archive roots> --plan aois/ard_tile_plan.json
  --out <root>/ard_tile_catalog.json` reads every `manifest.json` under the
  roots and writes tile → dataset(s), with each dataset's product spec,
  plus each study area's tile → dataset resolution. It exits 1 if a study
  area needs a tile no dataset holds. Downstream pipelines read this
  catalog instead of tracking archive paths. Keys are always
  region-qualified (`CU_h003v002`, `AK_h021v008`).
- If this becomes a DataLoader feature later, the natural form is a
  read-only `open_tiles(catalog, tiles)` next to `open_dataset`, not a
  change to how archives are written.

## Run plan (not started)

Per the 2026-10-20 plan: 13 new tiles, ~45,700 observations, ~4.3 TB
stored, ~5.9 TB downloaded, ~4.3–5.5 days on one M2M account
(`ard_tile_plan.json`; costs scaled from the measured Oregon run).

1. Check space where the archives live (Oregon is under `/vol/v1/data`):
   `df -h /vol/v1`. Needed: ~4.3 TB plus headroom (~5 TB free to be
   comfortable); the Oregon archive's own growth from updates is extra.
2. Confirm no other run uses the same USGS M2M account.
3. One study area at a time, in tmux, e.g.:
   ```bash
   data-loader validate aois/acquisitions/usfs_r10_northern_se_alaska.yaml
   data-loader plan aois/acquisitions/usfs_r10_northern_se_alaska.yaml --output-dir /vol/v1/data/usfs_r10_northern_se_alaska_landsat_ard
   data-loader run  aois/acquisitions/usfs_r10_northern_se_alaska.yaml --output-dir /vol/v1/data/usfs_r10_northern_se_alaska_landsat_ard
   ```
4. Afterwards: `data-loader verify` each dataset, then rebuild the tile
   catalog.

The Alaska config needs a DataLoader with native Alaska ARD grid support
(region-qualified tile ids; see CHANGELOG "Unreleased").
