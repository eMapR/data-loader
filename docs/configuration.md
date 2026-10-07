# Configuration

A DataLoader run is described by one YAML (or JSON) file. Every key is
listed below. Validation is strict: an unknown or misspelled key is an
error, and the message suggests the closest valid key. A typo can't
silently fall back to a default. Check a config without touching the
network with:

```bash
data-loader validate my_config.yaml
```

## Complete reference

```yaml
version: 1                       # required; the config schema version

provider: usgs_ard               # required: usgs_ard | planetary_computer | aws_earth_search | gee | glad_ard | usgs_m2m
sensors:                         # required: one or more of landsat, sentinel2
  - name: landsat
    product_family: usgs_ard_sr  # optional: fail instead of silently accepting a different product
    processing_version_policy: any   # optional: any | latest | allow_mixed | pinned:<baseline> (Sentinel-2)
# shorthand: sensors: [landsat]

aoi:                             # required: a bbox OR tiles
  upper_left: [-122.43, 44.29]   #   [lon, lat], EPSG:4326
  lower_right: [-122.34, 44.23]
  # tiles: [h003v004, h003v005]  #   provider tile ids (usgs_ard: CONUS ARD grid)

time:                            # required: years OR dates
  start_year: 1990
  end_year: 2025                 #   optional; omitted = through today (open-ended)
  # start_date: 2023-06-01       #   exact, day precision
  # end_date: 2023-09-30         #   optional; omitted = through today
  season:                        #   optional; omitted = whole year
    start: "06-01"               #     MM-DD
    end: "09-30"                 #     MM-DD; may be earlier than start (wraps New Year)

temporal_mode: scene             # required: scene | annual_composite
reduce: median                   # annual_composite only: median | mean

filters:
  max_cloud_percent: 100         # scene-level cloud filter, 0-100; 100 = keep everything (default)
  pixel_cloud_mask: false        # true = cloud/shadow/cirrus pixels -> nodata (default false)

grid:
  crs: auto                      # auto | "EPSG:<code>" | native (tiles only); default auto (native for tiles)
  resolution_m: 30               # default: the sensor's native size (Landsat 30, Sentinel-2 10)

output:
  dir: output/my_dataset         # required; overridable with --output-dir
  encoding: float32              # float32 (reflectance, NaN nodata) | native (source integers)
  bands: [blue, green, red, nir, swir1, swir2]
  indices: [nbr, ndvi]           # nbr, ndvi, tcb, tcg, tcw (float32 only)
  qa_band: false                 # true = keep the raw QA band (QA_PIXEL / SCL) as the last band

workers: 4                       # acquisitions in flight at once (default 1)
max_attempts: 3                  # per acquisition, across re-runs (default 3)
provider_options: {}             # provider-specific extras, e.g. {gee_project: my-project}
```

## `provider` and `sensors`

`provider` chooses where the data comes from. See [providers](providers.md)
for what each one serves. Each sensor resolves to a **product family**
(e.g. `usgs_ard_sr` for USGS Landsat ARD, `usgs_c2_l2` for Landsat
Collection 2 Level-2 scenes, `esa_s2_l2a` for Sentinel-2 L2A). Set
`product_family` to pin it: if the provider supplies something else, the
run fails before downloading anything.

`processing_version_policy` matters only where one acquisition can exist
in more than one ESA processing version. Today that's Sentinel-2 on
Earth Search, which lists both originals and reprocessed copies.

| Value | Behavior |
|---|---|
| `any` / `latest` | Keep the newest baseline per acquisition. |
| `pinned:05.00` | Keep only that baseline. |
| `allow_mixed` | Keep every version. |

Versions that were left out are listed in the manifest
(`coverage.excludedProcessingVersions`).

## `aoi`: bbox or tiles

**Bbox:** named corners in longitude/latitude (EPSG:4326):
`upper_left: [west, north]` and `lower_right: [east, south]`. Named corners
avoid the usual west/south/east/north ordering mix-ups. The bbox is read
onto one grid (`grid.crs`, default a UTM zone picked from the AOI center)
and labeled `aoi` in the output.

**Tiles:** `tiles: [h003v004, ...]` requests whole tiles of the provider's
own tile grid. Only `usgs_ard` has one: the CONUS ARD grid, with tiles of
5000×5000 px at 30 m in Albers Equal-Area, `h` 0–32 west to east and `v`
0–21 north to south. A tile request:

- reads every observation of the tile, including "slivers" from
  neighboring satellite paths that only clip the tile's edge;
- stays on the tile's native pixel grid, with no reprojection or
  resampling (`grid.crs` is `native`);
- writes each tile under its own ID: `landsat/h003v004/...`.

USGS's [ARD tile grid page](https://www.usgs.gov/landsat-missions/landsat-us-analysis-ready-data)
has maps of the grid. Oregon's 23 tiles are listed in
[`examples/oregon_landsat_ard_archive.yaml`](../examples/oregon_landsat_ard_archive.yaml).

Vector AOIs (GeoJSON polygons) are planned but not supported yet.

## `time`: years, dates and seasons

Give **years** (`start_year`, optional `end_year`) or **dates**
(`start_date`, optional `end_date`, as `YYYY-MM-DD`). Both ends are
inclusive: `start_year: 2000, end_year: 2020` covers 2000-01-01 through
2020-12-31.

**Open-ended:** leave out `end_year`/`end_date` and the request runs through
today. Running it again later adds imagery that has appeared since, which
is how an archive stays current. An explicit end in the future is also
clipped to today.

**Seasons:** `season: {start: MM-DD, end: MM-DD}` restricts every year to
that window. The request becomes a list of **time windows**, one per year,
and each is identified by its **season year**: *the year the season
starts in*.

| Request | Windows (season year: dates) |
|---|---|
| `start_year: 2020, end_year: 2021` | 2020: Jan 1–Dec 31 2020; 2021: Jan 1–Dec 31 2021 |
| same + `season: {start: "06-01", end: "09-30"}` | 2020: Jun 1–Sep 30 2020; 2021: Jun 1–Sep 30 2021 |
| `start_year: 2000, end_year: 2002, season: {start: "11-01", end: "02-28"}` | 2000: Nov 1 2000–Feb 28 2001; 2001: Nov 1 2001–Feb 28 2002; 2002: Nov 1 2002–Feb 28 2003 |
| `start_date: 2021-01-15, end_date: 2021-12-15`, same winter season | 2020: Jan 15–Feb 28 2021 (clipped); 2021: Nov 1–Dec 15 2021 (clipped) |

Notes:

- A wrapped season (end before start) crosses New Year and belongs to the
  year it starts in. `end_year` therefore names the last season, which may
  end in the following calendar year.
- `"02-29"` means the last day of February, so it works in non-leap years.
- With dates, windows are clipped to `start_date`/`end_date`.
- The windows are recorded in the manifest (`coverage.timeWindows`). In
  `items.jsonl`, scene rows carry their `seasonYear`, and composite rows
  carry `seasonYear` plus `windowStart`/`windowEnd`.

## `temporal_mode`

- **`scene`**: one output per acquisition (per tile, for tile AOIs). This is
  the mode for archives and time-series work.
- **`annual_composite`**: one composite per time window (per year or per
  season), reduced per pixel with `reduce: median` or `mean`. Masking is
  configurable as usual, and each composite's row lists every
  contributing scene. If a window is still open (the current season), it
  is rebuilt when a later run finds new scenes for it.

## `filters`

- **`max_cloud_percent`** (default 100): skips whole scenes whose
  catalog cloud cover is above this value. Skipped scenes still appear in
  `items.jsonl`, with `status: filtered` and the reason. Scenes with unknown
  cloud cover are kept.
- **`pixel_cloud_mask`** (default **false**): when true, pixels the
  provider's QA band flags as cloud, cloud shadow, cirrus or dilated cloud
  are set to nodata. Landsat uses QA_PIXEL bits 1–4; Sentinel-2 uses SCL
  classes 3, 8, 9 and 10. The exact rules are written into the manifest
  (`processing.pixelCloudMask`). The default keeps every source pixel.
  Pair that with `output.qa_band: true` so downstream code can decide for
  itself. Source *fill* (no data at all) is always nodata.

## `grid`

| `crs` | Meaning |
|---|---|
| `auto` (default for a bbox) | A UTM zone chosen from the AOI's center. Fine for small AOIs; choose explicitly for anything spanning zones. |
| `"EPSG:<code>"` | That CRS, e.g. `EPSG:5070` (CONUS Albers, the ARD projection). |
| `native` (default and required for tiles) | The provider's own tile grid, unchanged. |

`resolution_m` sets the pixel size for `auto` and EPSG grids. When reprojecting,
reflectance is resampled bilinearly and QA by nearest neighbor.

## `output`

- **`dir`**: where the dataset goes. Override it per run with `--output-dir`.
  One directory holds one dataset. Re-running the same request resumes or
  updates it. A *different* request into the same directory is refused;
  only the end of the time range, `workers`, `max_attempts` and
  `provider_options` may change between runs.
- **`encoding`**:
  - `float32`: surface reflectance with `NaN` nodata, ready to use.
    Indices are always float32.
  - `native`: the provider's original integers. For Landsat C2 and
    Sentinel-2 L2A that's uint16 DN, with `0` = nodata. Reflectance is
    `value * scale + offset`, with scale and offset in the manifest and in
    each GeoTIFF's band metadata. It's lossless and half the size of
    float32, and it's the right choice for archives. Requires
    `temporal_mode: scene` and no `indices`. Supported by `usgs_ard`,
    `planetary_computer` and `aws_earth_search`.
- **`bands`**: any of `blue, green, red, nir, swir1, swir2`, written in the
  order listed. Each provider maps these to its own band numbers.
- **`indices`**: any of `nbr, ndvi, tcb, tcg, tcw`, written to a separate
  `_indices.tif`. The bands an index needs are read automatically, even if
  they aren't in `bands`.
- **`qa_band`**: appends the provider's raw QA band to each bands file:
  Landsat `qa_pixel` (bitmask) or Sentinel-2 `scl` (classes). Its meaning
  is spelled out in the manifest. Scene mode only.

## Run settings

- **`workers`**: acquisitions processed at once (threads). Gains flatten
  past about 4. For `usgs_ard`, M2M allows one API call at a time per
  account; DataLoader queues those calls internally, while downloads still
  run in parallel.
- **`max_attempts`**: how many times one acquisition is tried, counted
  across re-runs. After that it stays `failed` in `items.jsonl`. Raise it
  and re-run to try again.
- **`provider_options`**: extra provider arguments, such as
  `{gee_project: ...}`. Only the option *names* are recorded in the
  manifest, never the values. Credentials belong in environment
  variables, not here.

## Migrating from pre-1.0 configs

Configs from before DataLoader 1.0 fail with a message pointing here. To
convert one:

| Before | Now |
|---|---|
| *(none)* | add `version: 1` |
| `date_range: {start, end, season_start, season_end}` | `time: {start_date, end_date, season: {start, end}}`, or `start_year`/`end_year` |
| `sensors: [{name: landsat, resolution_m: 30}]` | `sensors: [landsat]` + `grid: {resolution_m: 30}` |
| `output.target_epsg: EPSG:32610` | `grid: {crs: "EPSG:32610"}` |
| `temporal_mode` defaulted to `annual_composite` | `temporal_mode` is required |
| `filters` defaulted to `max_cloud_percent: 60`, `pixel_cloud_mask: true` | defaults are now 100 / false. Set them explicitly to keep the old behavior. |
| `output.write_files: false` (in-memory results) | removed: `run()` always writes a dataset; read it with `open_dataset()` |
| `python -m data_loader --config X` | `data-loader run X` |

The output layout changed too. Instead of `manifest.json` with a `files`
list and `landsat/bands_2020.tif`, there's now a `dataloader-manifest`
1.0 `manifest.json`, an `items.jsonl`, and files under
`<sensor>/<grid>/...`. See [outputs](outputs.md).
