# data-loader

A dynamic, config-driven imagery data loader for geoprocessing: describe
what you want in a YAML/JSON file (AOI, sensor(s), date range, data
source, annual composites vs. every individual scene, raw bands and/or
derived indices) and run it. It writes plain GeoTIFFs + a `manifest.json`
to an output directory — readable by any downstream program, not just
Python.

Originally extracted from LT-rust's `python/fetch_nbr.py` (a single
Planetary-Computer-only, NBR-only fetch function); generalized into a
pluggable multi-provider, multi-index, multi-mode loader.

## Install

```bash
pip install -r requirements.txt
# only if you'll use provider: gee —
pip install earthengine-api
```

## Use

```bash
python -m data_loader --config examples/annual_composite.yaml
python -m data_loader --config examples/scene_mode.yaml
```

Or in-process:

```python
from data_loader import load_config, run

result = run(load_config("examples/annual_composite.yaml"))
# result["landsat"]["composites"][2020]["indices"]["nbr"]  -> (H, W) float32 array
```

## Config

```yaml
aoi:
  upper_left: [-123.855, 45.896]   # [lon, lat], EPSG:4326
  lower_right: [-123.835, 45.882]  # [lon, lat], EPSG:4326

provider: planetary_computer     # planetary_computer | aws_earth_search | gee | usgs_ard | usgs_m2m | glad_ard

sensors:
  - name: landsat                # landsat | sentinel2
    resolution_m: 30             # optional, default per sensor if omitted
  - name: sentinel2
    resolution_m: 10

date_range:
  start: "2018-01-01"            # ISO date, day precision
  end: "2024-12-31"
  season_start: "06-01"          # MM-DD, optional — in-season window
  season_end: "09-15"            #   applied within every year in [start, end]

filters:
  max_cloud_percent: 60          # scene-level metadata filter
  pixel_cloud_mask: true         # per-pixel QA/SCL masking (bad pixels -> NaN)

temporal_mode: annual_composite  # annual_composite | scene
reduce: median                   # median | mean — only used for annual_composite

output:
  bands: [blue, green, red, nir, swir1, swir2]   # raw bands to write (omit = none)
  indices: [nbr, ndvi]                            # derived indices to write (omit = none)
  dir: output/my_aoi
  target_epsg: null              # auto-picks a UTM zone from the bbox centroid if null

provider_options: {}             # e.g. {gee_project: "your-ee-project"} for provider: gee
                                  # or {usgs_username, usgs_token} for provider: usgs_m2m
                                  # (env vars preferred over putting these in the config file)
```

Requesting an index that isn't in `output.bands` still works — the loader
fetches whatever raw bands the index needs internally, it just doesn't
write them as a separate file unless you also list them in `output.bands`.

### Providers

| provider | auth | notes |
|---|---|---|
| `planetary_computer` | none | Microsoft's free STAC API + signed COG reads |
| `aws_earth_search` | none | Element84's Earth Search STAC API over AWS Open Data |
| `gee` | Earth Engine account + project | ad hoc `getDownloadURL` fetch, no batch/Drive step — see `data_loader/providers/gee.py`. Good for exploratory AOIs; capped around 48MB per request. |
| `usgs_ard` | EROS account + M2M token | Landsat Collection 2 U.S. ARD surface reflectance, read per band directly from USGS on the fixed national Albers tile grid. Landsat only, CONUS/AK/HI only. M2M allows one request at a time per account, which caps concurrency — see the report. |
| `usgs_m2m` | EROS account + M2M token | Scene-based Landsat Collection 2 (no Sentinel-2). Heavier than the STAC providers — each scene is a full bundle download + local extraction. M2M auth verified live; the bundle read path has not yet been exercised end-to-end. |
| `glad_ard` | none | GLAD (UMD) 16-day normalized Landsat ARD tiles from the public S3 mirror, 2020–present. Landsat only. |

Credentials are read from environment variables (see `.env.example`;
copy it to `.env`, which is git-ignored) rather than from config files.

### Output layout

Raw bands and indices are written to separate files (different scaling —
reflectance vs. unitless), one file per sensor per year (composite mode)
or per scene (scene mode):

```
output.dir/
  manifest.json
  landsat/
    bands_2018.tif       # present only if output.bands is non-empty
    indices_2018.tif     # present only if output.indices is non-empty
  sentinel2/
    bands_2018.tif
    indices_2018.tif
```

In `scene` mode, `_2018.tif` becomes `_<date>_<scene-id>.tif` per scene
instead of one file per year. Every file is float32 with `NaN` as nodata
(no scale-factor/int16 trick — this loader targets general geoprocessing,
not the storage-constrained on-device use case GeoTimeSeriesApp3 uses
Cloud-Optimized GeoTIFFs for). `manifest.json` records each file's
provider, sensor, band names, CRS, transform, and (for composites) the
reducer used — the contract any downstream program reads to know what's
on disk without guessing filenames.

## Notes / known limitations

- AOI is bbox only for now — no polygon/route-corridor support (see
  GeoTimeSeriesApp3's `gee_export/export_timeseries.py` for that, a
  separate, unrelated pipeline this loader doesn't touch).
- `target_epsg` auto-picks a UTM zone from the bbox centroid if left
  unset — fine for small AOIs, pick one explicitly for anything crossing
  a zone boundary.
- Cloud masking: Landsat via `QA_PIXEL` bits 1–4 (dilated cloud, cirrus,
  cloud, cloud shadow); Sentinel-2 via Scene Classification (`SCL`)
  values 3/8/9/10 (cloud shadow, cloud med/high probability, thin
  cirrus). No terrain-shadow or snow masking.
- The `gee` provider's `season_start`/`season_end` window is computed
  per calendar year and doesn't handle a window that wraps the year
  boundary (e.g. `season_start: "11-01"`, `season_end: "02-28"`) in its
  fast composite path — falls back to the generic per-scene path in that
  case, which does handle wrap-around.
- Scene-level reads can run concurrently via `workers:` in the config
  (default 1). Gains flatten past ~2–4 workers (network/provider bound);
  `usgs_ard` is further limited by M2M's one-request-at-a-time rule.
- Local compositing holds every contributing scene for a composite in
  memory at once — watch memory on large AOIs / long periods.

## Findings and benchmarks

DataLoader is also where we record what we've measured about the
providers — speed, scaling, reliability, and whether two sources produce
scientifically equivalent products.

- [`docs/DATALOADER_DEVELOPMENT_REPORT.md`](docs/DATALOADER_DEVELOPMENT_REPORT.md) —
  the full development and scientific report: architecture, correctness
  bugs found and fixed, provenance, performance findings, open questions.
- [`docs/benchmarks/README.md`](docs/benchmarks/README.md) — methodology and
  headline results for each benchmark, and how to re-run it.
- [`docs/figures/`](docs/figures/) — figures generated from benchmark results.

## Repository layout

```
data_loader/          the loader: config, engine, masking, indices, manifest
  providers/          one module per imagery source (shared Provider interface)
examples/             example YAML configs
tests/                unit tests (pytest; network-free)
bench/                benchmark harnesses and analysis scripts
  results/            raw benchmark outputs + imagery — git-ignored, can be 10s of GB
docs/
  DATALOADER_DEVELOPMENT_REPORT.md
  benchmarks/         methodology + results summaries
    data/             compact summary JSON extracted from bench/results (tracked)
  figures/            generated figures we want to keep (tracked)
```

Raw benchmark outputs and generated imagery stay out of Git; only compact
summaries and figures, produced by scripts in `bench/`, are committed.
