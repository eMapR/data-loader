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

## Done (2026-10-03): full ARD tile history run on the server
Measured the real cost of acquiring one complete ARD tile's Landsat history
directly from USGS: tile `h003v004` (central Oregon Cascades, 5000x5000 px at
30 m), 1990-01-02 to 2026-09-26, Landsat 4/5/7/8/9, 6 SR bands + QA_PIXEL.
Script: `bench/ard_tile_history_bench.py` (docstring explains subcommands);
summary: `docs/benchmarks/data/ard_tile_history_h003v004.json`.

Result (4 workers, `run --save uint16`):
- 2,411/2,411 observations acquired, 0 permanently failed, in 10.7 h wall
  (226 obs/h, ~16 s/obs effective)
- 487 GB transferred; saved as uint16 GeoTIFF (original C2 SR DN, 0 = nodata,
  scale/offset in band tags): 165 GB; peak RSS 7.5 GB
- Per observation per worker: ~20 s download+decode, ~12 s M2M URL minting
  (mostly queueing on the one-request-at-a-time account lock), ~16 s uint16
  write (single-threaded deflate)
- `net_gb_indicative` in the summary is 0.0 because the netstat counter
  doesn't work on this server; bytes come from band file sizes instead

What changed along the way:
- `usgs_ard` full-tile reads on the native grid now download whole band files
  in parallel and decode from memory (`_read_native_tile`): ~110 s -> ~10 s
  per observation, bit-identical to the old /vsicurl + WarpedVRT path, which
  is still used for subsets/other grids. The network was never the limit
  (USGS landsatlook via CloudFront: 10-28 MB/s per stream, ~70 MB/s at 14
  streams; `bench/bandwidth_check.py`, `bench/ard_read_path_compare.py`).
- The 90-min M2M re-login now queues behind `_m2m_call_lock` and retries
  RATE_LIMIT; before the fix it caused 27 failed attempts (all succeeded on
  retry).
- Earlier baselines, for comparison: Mac pilot (old path) projected ~28 h;
  server pilot (old path) ~68 h; server pilot (new path) ~4.8 h, which left
  out the write and minting costs.

## Next: price the Oregon-scale run
Remaining levers per tile: multi-threaded GeoTIFF writes (GDAL `NUM_THREADS`)
and more workers, bounded by M2M's one-request-at-a-time limit per account.
Plausibly ~3-5 h per tile. Confirm with the user before any multi-tile run,
and check free disk first (~165 GB per tile as uint16).

Server notes: credentials live in `~/.config/data-loader/usgs.env`
(`set -a; source ...; set +a` before each command); run long jobs in tmux.
Raw run output stays in git-ignored `bench/results/ard_tile_history/h003v004/`
(including `imagery/`).
