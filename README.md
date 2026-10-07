# DataLoader

DataLoader is the imagery-acquisition front end for eMapR pipelines
(LandTrendr, BULC-D, BugNet, foundation-model work, ...). A pipeline
describes the imagery it needs in a small YAML config: provider, sensor,
area, years/season, bands. DataLoader then finds it, downloads it, and
writes plain GeoTIFFs plus a **versioned, machine-readable manifest**
recording what was acquired, where it came from, and what was done to it.
Downstream code reads the manifest and never needs provider-specific code
or filename parsing.

```
config.yaml ──► DataLoader ──► provider (USGS ARD, Planetary Computer, Earth Search, ...)
                    │
                    └──► <output.dir>/  GeoTIFFs + manifest.json + items.jsonl + source metadata
                                              │
                                              └──► your pipeline
```

- **One tool for small pulls and big archives.** A one-month AOI and a
  35-year, 23-tile Landsat archive use the same command and produce the same
  output layout. The difference is only in the config.
- **Resumable and updatable.** If a run is interrupted, run the same command
  again and it picks up where it stopped. Leave the end year open and
  re-running later adds the new imagery.
- **Provider-independent.** Change `provider:` to switch sources. Before
  downloading, DataLoader checks that the provider supplies the scientific
  product you asked for.
- **Nothing hidden.** Cloud masking is off unless you ask for it. QA bands can be
  kept, source metadata is saved verbatim, and failures are recorded rather
  than silently dropped.

## Install

Python 3.9 or newer. Everything installs from pip; GDAL comes bundled with
the `rasterio` wheel.

```bash
git clone https://github.com/eMapR/data-loader.git
cd data-loader
python3 -m venv .venv && source .venv/bin/activate
pip install -e .            # add '.[gee]' for Google Earth Engine, '.[dev]' for tests
data-loader --version
```

## Five-minute example

[`examples/quickstart.yaml`](examples/quickstart.yaml) pulls every Landsat scene over a small Oregon AOI
for July 2023 from Microsoft Planetary Computer. It needs no account.

```yaml
version: 1
provider: planetary_computer
sensors: [landsat]
aoi:
  upper_left: [-122.4327, 44.2860]     # [lon, lat]
  lower_right: [-122.3426, 44.2315]
time:
  start_date: 2023-07-01
  end_date: 2023-07-31
temporal_mode: scene                   # or annual_composite
output:
  dir: output/quickstart
  bands: [red, nir]
  indices: [ndvi]
  qa_band: true
```

```bash
data-loader validate examples/quickstart.yaml   # checks the config, no network
data-loader run examples/quickstart.yaml        # ~1 minute; prints the manifest path
data-loader status output/quickstart
```

```
output/quickstart/
  manifest.json            what this dataset is: request, product, bands, grids, processing, counts
  items.jsonl              one line per acquisition: date, sensor, source ID, status, files, checksums, provenance
  request.yaml             the exact request that produced it
  metadata/<provider>/     each source item's original metadata record, verbatim
  landsat/aoi/2023/2023-07-04_LE07_L2SP_045029_20230704_02_T1_bands.tif
  landsat/aoi/2023/2023-07-04_LE07_L2SP_045029_20230704_02_T1_indices.tif
  ...
```

Reading it from Python:

```python
from data_loader import open_dataset
import rasterio

ds = open_dataset("output/quickstart")
print(ds.manifest["bandSets"]["landsat"]["bands"])      # band order, dtype, scale/offset, nodata, QA meaning
for path, item, f in ds.files(start="2023-07-10"):
    with rasterio.open(path) as src:
        red = src.read(1)
    print(item["date"], item["itemId"], item["eo:cloud_cover"])
```

Adapt the example: edit the corners, dates, and bands, and point
`output.dir` (or `--output-dir`) somewhere new.

## Choosing a provider

| Provider | Data | Account | Use it for |
|---|---|---|---|
| `usgs_ard` | Landsat 4–9 C2 **U.S. ARD** SR, straight from USGS | USGS EROS + M2M token | **Preferred for Landsat time series and archives.** USGS is the authoritative source, it covers Landsat 4–9, and every observation sits on a fixed 5000×5000 px Albers tile grid. |
| `planetary_computer` | Landsat 5–9 C2 L2 and Sentinel-2 L2A | none | Quick AOI pulls and Sentinel-2 with no setup. |
| `aws_earth_search` | Sentinel-2 L2A (free); Landsat (AWS Requester Pays) | none for Sentinel-2 | Sentinel-2. Its Landsat bucket bills an AWS account. |
| `gee` | Landsat C2 L2, Sentinel-2 SR (harmonized) | Earth Engine project | Exploratory small AOIs. Requests are capped at ~48 MB each. |
| `glad_ard` | GLAD 16-day Landsat ARD, 2020+ | none | GLAD's normalized 16-day composites specifically. |
| `usgs_m2m` | Landsat C2 L2 scene bundles | USGS EROS + M2M token | Experimental: the bundle download path has not been verified end to end. |

The full comparison, with caveats such as DataLoader's Planetary Computer
Landsat path covering Landsat 5–9 only and how Sentinel-2 processing versions are handled, is
in [docs/providers.md](docs/providers.md). The USGS account setup is in
[docs/getting-started.md](docs/getting-started.md#usgs-eros-account-and-m2m-token-usgs_ard-usgs_m2m).

## Archives: whole tiles, full history

For a persistent archive, request whole USGS ARD tiles. DataLoader then
stores each observation exactly as USGS distributes it: uint16 DN with
QA_PIXEL, no cloud masking, and no resampling. Downstream pipelines make
their own cloud, shadow and snow decisions.

```yaml
aoi:
  tiles: [h003v004, h003v005]
time:
  start_year: 1990                     # no end_year: through today, updatable
temporal_mode: scene
output:
  encoding: native
  bands: [blue, green, red, nir, swir1, swir2]
  qa_band: true
```

See [`examples/usgs_ard_tile_archive.yaml`](examples/usgs_ard_tile_archive.yaml) and eMapR's Oregon archive
[`examples/oregon_landsat_ard_archive.yaml`](examples/oregon_landsat_ard_archive.yaml), along with
[docs/large-jobs.md](docs/large-jobs.md) for runtime, storage, tmux, resume and updates. The
Oregon archive is about 89,000 observations, takes 2–4 weeks on one USGS
account, and needs roughly 4–5 TB.

## Documentation

| | |
|---|---|
| [Getting started](docs/getting-started.md) | Install, credentials for each provider, first runs |
| [Configuration](docs/configuration.md) | Every config key; years, seasons and dates; AOIs and tiles; migrating old configs |
| [Providers](docs/providers.md) | What each provider serves, accounts and costs, when to choose which |
| [Outputs](docs/outputs.md) | Directory layout, `manifest.json` and `items.jsonl` field by field, reading data, masking with QA |
| [Large jobs](docs/large-jobs.md) | Archives, runtime and storage, resume, updates, failures |
| [Troubleshooting](docs/troubleshooting.md) | Common errors and fixes |
| [Development](docs/development/README.md) | Architecture, adding a provider, tests, benchmarks and development history |

Example configs live in [`examples/`](examples/). Run the tests with
`pytest` (they need no network or credentials).

## Status

Version 1.0. The config schema (`version: 1`) and the output manifest
(`dataloader-manifest` 1.0) are the stable interfaces; see
[CHANGELOG.md](CHANGELOG.md). Planned next: vector (GeoJSON) AOIs, and
Sentinel-2 directly from the Copernicus Data Space Ecosystem.
