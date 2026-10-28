# Changelog

## 1.1.0 (2026-10-28)

Stabilization after the October 2026 audit
([docs/development/AUDIT_2026-10.md](docs/development/AUDIT_2026-10.md)),
plus native Alaska and Hawaii ARD grids.
Manifest schema 1.1 is additive; 1.0 datasets are read and resumed as is.
Config changes: see *Provider options and credentials* and *Product
consistency* below.

### Native Alaska and Hawaii ARD grids
- **Native Alaska and Hawaii ARD grids.** `aoi.tiles` now takes
  region-qualified USGS ARD tile ids: `AK_h021v008` (Alaska grid),
  `HI_h001v001` (Hawaii), `CU_h003v004` (CONUS). Each region has its own
  Albers projection and origin; tiles are read on their native grid like
  CONUS tiles. One request can mix regions.
- **An Alaska tile id could silently fetch a CONUS tile.** Every region
  numbers its tiles from `h000v000`, but tile searches always queried the
  CONUS grid: `h021v008` meant to be near Juneau validated and downloaded
  northern Illinois. Searches now query the tile's own region, discovered
  items must match the tile's region as well as its h/v, and an item from
  another region is refused when its grid is read.
- A bare tile id still means CONUS, and CONUS tiles are still recorded
  bare (`CU_h003v004` is recorded as `h003v004`), so existing datasets'
  keys, paths, manifests and request identity are unchanged and resume
  as is. Manifest additions only: `grids.<tile>.tile.system` can be
  `usgs_ard_alaska`/`usgs_ard_hawaii`, and `tile.id` is region-qualified
  outside CONUS.
- `data_loader.tiles.UsgsArdGrid` replaces `UsgsArdConusGrid` (kept as an
  alias); `parse()` now returns `(region, h, v)`.
- The same tile listed twice (e.g. `h003v004` and `CU_h003v004`) is now a
  config error.

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
- **QA band nodata in the manifest** is now the file's nodata (`0` native,
  `NaN` float32), matching the GeoTIFF; 1.0 said `null`.
- Error messages no longer show `?<redacted><redacted>`.

### Provider options and credentials
- `usgs_ard` and `usgs_m2m` reject unknown `provider_options` (they used
  to ignore typos) and no longer accept `usgs_username`/`usgs_token` there:
  USGS credentials come only from `USGS_M2M_USERNAME`/`USGS_M2M_TOKEN`.
  **Configs that put them in `provider_options` must move them to the
  environment.**

### Product consistency
- A sensor without `product_family` resolves to the provider's product only
  if that is an agency source product (`usgs_ard_sr`, `usgs_c2_l2`,
  `esa_s2_l2a`) delivered as scenes. Derived products (`glad_ard`'s
  normalized 16-day composites, GEE's `esa_s2_l2a_harmonized`) must be
  named explicitly; before, switching `provider:` could substitute them
  silently. **Configs using `glad_ard`, or `gee` for Sentinel-2, must add
  `product_family`.**
- The recorded request (manifest `request`, `request.yaml`) names each
  sensor's resolved `product_family`. Datasets recorded with
  `product_family: null` resume with it pinned or unpinned.
- `processing_version_policy: pinned:...` is checked by `validate`/`plan`:
  a provider/sensor with no processing versions (every Landsat source) is
  a config error. Planetary Computer and Earth Search Landsat used to
  accept and ignore it.
- All examples pin `product_family`; the docs recommend it for every
  pipeline config.

### Output contract: manifest schema 1.1 (additive)
- `files[].bandScaleOffset` is written for **every** band of every
  `bands` file (1.0 wrote it only when it differed from `bandSets`) and is
  the authoritative scale/offset. The docs' reading example now uses it;
  the old example applied the band-set offset, which is 0.1 off for
  Planetary Computer Sentinel-2 from 2022 on.
- `software` (DataLoader version, git commit, dirty flag) on every
  acquired/failed row; `dataset.runs` lists every run into the directory;
  `dataset.gitDirty`.
- `data-loader upgrade DIR [--run START=COMMIT ...] [--dry-run]
  [--check-files N]` brings a 1.0 dataset's metadata to 1.1 without
  touching imagery. 1.0 datasets stay readable and resumable as they are.
- A writer refuses a dataset of an unknown major schema version instead of
  rewriting it.

### Reliability
- **Circuit breaker** (`max_consecutive_failures`, default 20, `0` = off):
  when that many acquisitions fail their first attempt in a row, the run
  treats the source as down: those attempts are not counted against
  `max_attempts`, and it pauses 5, 15, 30, then 60 minutes before
  continuing. Still failing after that, it stops with **exit code 5** and
  resumes on the next run. Before, a multi-hour USGS outage could use up
  `max_attempts` for every remaining acquisition within hours.
- **USGS ARD downloads are verified** against the SHA-512 `file:checksum`
  in each STAC item; a mismatching or truncated file is downloaded again,
  and a connection dropped mid-file (`ChunkedEncodingError`) is retried
  like other transport errors. Rows record it as `sourceIntegrity`.
  M2M HTML maintenance pages are retried instead of failing the
  acquisition; a rejected M2M token (`AUTH_*`) stops the run.
- Signed URLs and per-tile locks are dropped once a tile is read, instead
  of accumulating for the whole run. A failed acquisition's future is
  released as soon as it is recorded. Progress lines report memory
  (`rss`, `peak`).
- Failures that waiting can't fix stop the run at once (exit code 5),
  without counting the attempt: output disk full, quota exceeded or
  read-only, and provider `SystemicError`s such as rejected credentials.
  `dataset.runs[]` records why a run stopped.
- manifest.json, items.jsonl, GeoTIFFs and the run journal are flushed to
  disk (fsync) before they replace the previous version, so a power loss
  can't leave an empty `items.jsonl`. Metadata snapshots are written
  atomically, and one cut short by a crash is rewritten.

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
