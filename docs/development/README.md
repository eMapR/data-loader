# Development

How DataLoader is put together, how to extend it, and where the
measurement history lives. For using DataLoader, start at the
[README](../../README.md).

## Architecture

```
config (YAML/JSON)
  └─ config.py         validate (strict keys) -> Config; time windows; request identity
        └─ engine.py   check_request -> discover -> units -> acquire (thread pool) -> catalog
              ├─ providers/        WHERE: search + read, one module per source (Provider interface)
              ├─ product_contract  WHAT: product family per sensor; refuses silent substitution
              ├─ tiles.py          named tile grids (USGS ARD CONUS)
              ├─ masking.py        QA rules + their descriptions for the manifest; DN -> reflectance
              ├─ indices.py        NBR, NDVI, tasseled cap
              ├─ geotiff.py        atomic GeoTIFF writes, sha256
              ├─ metadata_snapshot verbatim provider records -> metadata/<provider>/
              └─ dataset.py        the output contract: DatasetWriter (journal, lock, items.jsonl,
                                   manifest.json) and Dataset/open_dataset (reader, status, verify)
```

**One engine for every request shape.** `engine.discover` turns a config
into *units*: one per (sensor, grid, acquisition) in scene mode, or one
per (sensor, grid, season) for composites. `engine.run` acquires the
units that the dataset doesn't already hold. Tile archives are not a
separate code path; they are scene-mode requests with tiles, a native
grid and native encoding.

**Units and resume.** A unit's `key` is stable (`sensor/grid/item-id`, or
`sensor/grid/composite/season-year`), so a re-run can match it against
`items.jsonl`. Every finished unit is appended to `.state/journal.jsonl`
immediately. `items.jsonl` and `manifest.json` are rewritten atomically
from the journal periodically and at the end. Files are written to
`*.tmp` and renamed. A unit counts as done only if its row says
`acquired` *and* its files exist at their recorded sizes.

**Request identity.** `config.request_identity` is the part of the
request that defines the dataset: everything except `output.dir`,
`workers`, `max_attempts`, `provider_options` and the end of the time
range. A run into an existing directory must match it. This is what makes
"extend the end date and re-run" an update, while a different request
cannot be mixed in.

**Masking and encoding happen in the engine.** Providers that implement
`read_scene_native` return source integers and the raw QA band. The
engine converts them to reflectance (float32), masks them
(`pixel_cloud_mask`), or stores them as-is (`encoding: native`). Because
there is one implementation, the manifest's description of the processing
is exact. Providers without a native read path (`gee`, `glad_ard`,
`usgs_m2m`) return float32 reflectance from `read_scene_bands` and support
float32 output only.

## The provider interface

`data_loader/providers/base.py` defines it. Required:

| Method | Returns |
|---|---|
| `name` | Provider id, as used in `provider:` |
| `capabilities()` | `{sensor: ProductIdentity}`. Static; no I/O. |
| `search_scenes(bbox, sensor, start, end, season_start, season_end, max_cloud_percent, processing_version_policy)` | `[SceneRef]`. The engine passes `season_*=None` and `max_cloud_percent=100` and filters itself, so the cloud filter is recorded. A provider must not drop items at 100. |
| `read_scene_bands(scene, sensor, bands, grid, pixel_cloud_mask)` | `{band: float32 reflectance on grid}`, fill as NaN |

Optional, detected by attribute:

| Capability | Enables |
|---|---|
| `read_scene_native(scene, sensor, bands, grid, include_qa)` → `NativeRead`, plus `native_encoding(sensor)` → `NativeEncoding` | `encoding: native`, `qa_band`, engine-side masking |
| `tile_grid` (a `tiles.TileGrid`) + `search_tile(tile_id, sensor, start, end, max_cloud_percent)` | `aoi.tiles`, `grid.crs: native` |
| `source_metadata(scene)` | Verbatim snapshot in `metadata/` |
| `processing_profile(sensor)` | Provider facts recorded in the manifest |
| `credential_problems(sensors)` | Early, clear "missing credentials" errors |
| `read_annual_composite(...)` + `last_composite_scenes` | Provider-side composites (GEE) |

**Adding a provider** (e.g. Copernicus Data Space Sentinel-2):

1. Write `providers/<name>.py` with a `make_provider(**provider_options)`
   factory, and register it in `providers/__init__.py`.
2. Declare the product family honestly. Reuse an existing family only if
   the product is byte-for-byte the same scientific product; otherwise
   add one to `product_contract.py`.
3. Fill `SceneRef.provenance` (`AcquisitionProvenance`) with everything
   the source can supply.
4. For STAC sources, `providers/stac_common.py` already does
   search/sign/retry/read; a new STAC provider is mostly a `StacConfig`
   (see `planetary_computer.py`).
5. Add network-free tests. `tests/fakes.py` has a fake provider covering
   the whole engine.

## Tests

```bash
pip install -e '.[dev]'
pytest              # ~200 tests, ~5 s, no network or credentials
```

Live behavior (STAC searches, signed URLs, M2M) is verified by running the
examples. `tests/` covers config validation and time windows, the
dataset contract and resume/update semantics, native encoding and
masking, concurrency, providers' retry/sign/offset logic, the ARD tile
grid, and the CLI.

## Benchmarks and history

- [`DATALOADER_DEVELOPMENT_REPORT.md`](DATALOADER_DEVELOPMENT_REPORT.md) is
  the narrative record: correctness bugs found and fixed, provider
  equivalence checks, performance findings, why USGS ARD was chosen, the
  ARD tile-history and Oregon-scale measurements, and open questions.
- [`benchmarks/`](benchmarks/README.md) has the methodology and headline
  result of each benchmark, the compact result data, and how to re-run.
- [`figures/`](figures/) holds the figures, generated by `bench/make_figures.py`.
- `bench/` contains the benchmark harnesses. Raw output goes to
  `bench/results/` (git-ignored); compact summaries go to
  `benchmarks/data/` via scripts. Never hand-edit those numbers.

## Conventions

- Tools are config-file driven (YAML/JSON in, files + manifest out), not
  Python functions with growing keyword lists.
- Measure before optimizing, and never trade product equivalence or
  provenance for speed.
- Don't assume "open data" is free or anonymous: check (Requester Pays,
  M2M accounts, rate limits) and document it.
- Credentials come only from environment variables. Values in
  `provider_options` are never written to outputs.
