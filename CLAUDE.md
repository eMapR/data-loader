# data-loader — notes for Claude

Config-driven, multi-provider satellite imagery loader. A YAML/JSON config
(AOI, sensors, day-precision date range, provider, `annual_composite` vs
`scene` mode, bands and/or indices) goes in; float32 GeoTIFFs (NaN nodata)
plus `manifest.json` come out. Entry point: `python -m data_loader <config>`
(see `examples/`). Background and benchmark history:
`docs/DATALOADER_DEVELOPMENT_REPORT.md`.

## Layout
- `data_loader/engine.py` — provider-agnostic engine; `data_loader/providers/`
  implement the shared `Provider` interface (`search_scenes`,
  `read_scene_bands`, optional `read_annual_composite`).
- Providers: `planetary_computer` (free, no auth), `usgs_ard` (direct USGS
  Landsat C2 U.S. ARD on the Albers tile grid; needs M2M credentials; M2M
  allows one request at a time per account), `usgs_m2m`, `glad_ard`,
  `aws_earth_search` (Landsat there is Requester Pays), `gee` (optional).
- Credentials come from environment variables; see `.env.example`.
- `bench/` — benchmark scripts. `bench/results/` is git-ignored (raw
  output); compact summaries go in `docs/benchmarks/data/`, figures in
  `docs/figures/` (`bench/make_figures.py`).
- Tests: `python -m pytest tests`.

## How the user likes to work
- Tools should be config-file-driven (YAML/JSON in, plain files + manifest
  out), not Python functions with growing kwarg lists.
- Do calculations, result aggregation and figures in scripts; keep terminal
  output short and targeted. Commit reusable analysis scripts under `bench/`.
- Don't assume "open data" is free or anonymous — check (Requester Pays,
  M2M auth).

## Done (2026-10-04): complete ARD tile history for h003v004
Measured the real cost of acquiring an ARD tile's Landsat history directly
from USGS: tile `h003v004` (central Oregon Cascades, 5000x5000 px at 30 m),
1990-01-01 to 2026-10-02, Landsat 4/5/7/8/9, 6 SR bands + QA_PIXEL.
Script: `bench/ard_tile_history_bench.py` (docstring explains subcommands);
summary: `docs/benchmarks/data/ard_tile_history_h003v004.json` (overall and
`by_attempt`).

Result: 3,885/3,885 observations acquired, 0 permanently failed, 14.8 h wall
(4 workers, `--save uint16`), 523 GB transferred, 189 GB saved (uint16
original C2 SR DN, cloud/shadow/cirrus/fill = 0 nodata, scale/offset in band
tags; QA_PIXEL itself not saved). Done in two passes:
- 2,411 observations found by the first discovery (a 0.1 deg center AOI):
  10.7 h, 15.9 s/obs effective, ~202 MB/obs. Per obs per worker ~20 s
  download+decode, ~12 s M2M minting (queueing on the one-request-at-a-time
  account limit), ~16 s single-threaded deflate write.
- 1,474 edge "slivers" the center AOI missed (an ARD item's geometry is its
  data footprint; slivers are neighboring WRS-2 paths clipping the tile,
  ~92% fill): `discover --end 2026-10-02` over the whole tile, then a
  resumed `run --label slivers`: 4.2 h, 10.2 s/obs effective, ~24 MB/obs.
  Center-AOI discovery kept in `.../h003v004/discovery_center_aoi/`.
- `net_gb_indicative` is 0.0 (netstat counter doesn't work on this server);
  bytes come from band file sizes.

What changed along the way:
- `usgs_ard` full-tile reads on the native grid download whole band files in
  parallel and decode from memory (`_read_native_tile`): ~110 s -> ~10 s per
  observation, bit-identical to the /vsicurl + WarpedVRT path (still used for
  subsets/other grids). The network was never the limit
  (`bench/bandwidth_check.py`, `bench/ard_read_path_compare.py`).
- The 90-min M2M re-login queues behind `_m2m_call_lock` and retries
  RATE_LIMIT (it caused 27 failed attempts before the fix).
- SR fill (DN 0) is NaN, not reflectance -0.2, in `usgs_ard` and
  `stac_common` (`masking.dn_to_reflectance`); float32 outputs written before
  2026-10-03 still carry -0.2 fill. uint16 imagery was never affected.

## Oregon-scale estimate (2026-10-04)
`bench/oregon_ard_estimate.py` -> `docs/benchmarks/data/oregon_ard_estimate.json`:
scales the measured h003v004 costs (15.9 s/full obs, 10.2 s/sliver, 4
workers, uint16 save, one M2M account) to every ARD tile touching Oregon,
with per-tile observation counts and fill from the LandsatLook STAC.
- 23 tiles touch Oregon (18 with >=5% Oregon share); ~88,900 observations
  1990-2026, ~29k of them slivers (fill >= 80%)
- ~11.4 TB transfer, ~4.1 TB saved as uint16
- ~347 h (14.5 days) for all 23 tiles; ~278 h (11.6 days) for the 18.
  Floor from serial M2M minting (~4.7 s/obs): ~116 h.

Remaining levers: multi-threaded GeoTIFF writes (GDAL `NUM_THREADS`) and more
workers, bounded by M2M's one-request-at-a-time limit per account.

## Running the tile-history bench (config-driven since 2026-10-05)
`bench/ard_tile_history_bench.py <discover|pilot|estimate|run|report> --config CFG [--tile hHHHvVVV] [--label TEXT]`.
The YAML config names tiles, date_range, bands, cloud_mask, save
(`format: uint16|float32|none`, `qa_pixel: true|false`), workers,
max_fails_per_obs, output_dir and summary_dir; unknown keys are errors.
- `bench/configs/ard_tile_history_h003v004.yaml` -- the benchmark's config;
  `report` with it reproduces the committed summary byte-for-byte.
- `bench/configs/ard_tile_history_oregon.yaml` -- template for all 23 Oregon
  tiles (ordered by Oregon share), `qa_pixel: true`. NOT RUN: confirm the
  tile list and end date with the user, and check free disk on the output
  volume first (~4.1 TB+ as uint16; /vol/v1 had 15 TB free on 2026-10-05).
- `save.qa_pixel` appends raw QA_PIXEL as the last band (scale 1, offset 0)
  via `usgs_ard.read_scene_bands(..., keep_qa=True)`; with `cloud_mask:
  false` + `qa_pixel: true` only fill is masked and nothing is lost.

Server notes: credentials live in `~/.config/data-loader/usgs.env`
(`set -a; source ...; set +a` before each command); run long jobs in tmux.
Raw run output stays in git-ignored `bench/results/ard_tile_history/h003v004/`
(including `imagery/`).
