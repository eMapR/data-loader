# Changelog

## 1.1.0 (unreleased)

### Fixed
- **Sentinel-2 scenes dropped on Planetary Computer.** Processing-version
  de-duplication keyed each acquisition by datatake + MGRS tile, but read
  the tile only from `grid:code`/`mgrs:grid_square`, which Planetary
  Computer items don't have (`s2:mgrs_tile`). Every tile of one overpass
  fell into one group: for an AOI spanning two tiles, half the scenes were
  dropped and listed as excluded processing versions, or the run failed on
  an unresolvable tie. The tile now comes from `s2:mgrs_tile`, `grid:code`,
  the split `mgrs:*` fields or the product id; an item whose tile or
  datatake can't be determined is never grouped with others. Earth Search
  was not affected (it has `grid:code`). Provenance `extra.mgrs_tile` is
  recorded for every Sentinel-2 item.

## 1.0.0 (2026-10-07)

First stable release for use by other eMapR pipelines. The config schema
(`version: 1`) and the output manifest (`dataloader-manifest` 1.0) are the
stable interfaces from here on.

### Breaking changes
- **New config format** (`version: 1`). `date_range` became `time`, which
  takes years or dates plus a `season`. `target_epsg` and per-sensor
  `resolution_m` moved to `grid`, and `temporal_mode` is now required.
  Unknown keys are errors. See
  [Migrating from pre-1.0 configs](docs/configuration.md#migrating-from-pre-10-configs).
- **New defaults that keep source data:** `filters.max_cloud_percent: 100`
  and `filters.pixel_cloud_mask: false` (previously 60 and true). Set
  them explicitly for the old behavior.
- **New output layout and manifest:** `manifest.json` is now a versioned
  dataset description, `items.jsonl` holds one row per acquisition, and
  files live under `<sensor>/<grid>/<year>/`. See [outputs](docs/outputs.md).
- **New command line:** `data-loader validate|plan|run|status|verify`
  replaces `python -m data_loader --config`.
- **`run()` returns a `RunSummary`, not arrays.** Read results with
  `open_dataset()`. Removed: `output.write_files`, `fetch_annual_nbr`,
  `pixel_index`, `example.py`.

### Added
- **USGS ARD tile archives as a first-class workflow:** `aoi.tiles`
  (CONUS ARD grid), whole-tile discovery including edge slivers,
  native-grid reads with no resampling, and `output.encoding: native`
  (uint16 DN with scale/offset) with `output.qa_band` (raw QA_PIXEL or SCL).
- **Resumable, updatable datasets:** per-acquisition journal, atomic
  writes, a per-directory lock, retry passes up to `max_attempts` (per
  tile, before the next tile starts),
  open-ended time ranges, and protection against mixing different
  requests in one directory.
- **Time windows:** `start_year`/`end_year` (whole years, inclusive) or
  exact dates, with seasons that may wrap New Year (labeled by the year
  they start in).
- Scene cloud-filter exclusions are listed as `status: filtered`, and
  masking rules and QA band meanings are written into the manifest.
- sha256 for every file, `data-loader verify`, and a `pyproject.toml`
  (`pip install -e .`).
- Faster GeoTIFF writes: multi-threaded deflate.

### Fixed
- **Sentinel-2 L2A reflectance offset** for processing baseline ≥ 04.00
  (2022 onward). Planetary Computer data needed offset −0.1 and got 0, so
  it came out about 0.1 too high. Earth Search pixels already have it
  removed, and DataLoader leaves them alone.
- A scene cloud filter of 100 no longer drops scenes with 100% (or
  unknown) cloud cover.
- An end year now means the whole year: pre-1.0 `end: 2020` meant
  2020-01-01.
- Error messages in logs and `items.jsonl` no longer contain signed
  download URLs' query strings (USGS `requestSignature`, Planetary
  Computer SAS tokens); they read `...TIF?<redacted>`.

## 0.x (2026-09 to 2026-10)

Development versions: multi-provider loader, provider benchmarks, the USGS
ARD investigation, and the h003v004 tile-history and Oregon-scale
measurements. See the
[development report](docs/development/DATALOADER_DEVELOPMENT_REPORT.md). The
last pre-1.0 commit is tagged `pre-1.0`.
