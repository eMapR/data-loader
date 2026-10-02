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

## Current task (as of 2026-10-02): full ARD tile history run on a server
Goal: measure the real cost of acquiring one complete ARD tile's Landsat
history (tile `h003v004`, central Oregon Cascades, 5000x5000 px at 30 m,
1990-01-01 to present, all of Landsat 4/5/7/8/9) directly from USGS. This
prices the eventual Oregon-scale run. Script: `bench/ard_tile_history_bench.py`
(its docstring explains the subcommands).

Steps on the server:
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export USGS_M2M_USERNAME=... USGS_M2M_TOKEN=...
python bench/ard_tile_history_bench.py discover   # ~30 s; observations.json isn't in git
python bench/ard_tile_history_bench.py pilot      # optional, ~20 min, re-baselines on this network
tmux new -s ardrun
python bench/ard_tile_history_bench.py run --workers 4 2>&1 | tee bench/results/ard_tile_history/h003v004/run.log
python bench/ard_tile_history_bench.py report     # any time; run is resumable
```

Pilot baseline from the user's Mac (2026-09-28, 15 observations, all OK,
0 retries):
- 2,410 observations (LT05 730, LT04 4, LE07 843, LC08 613, LC09 220)
- ~104 s per observation serial (LC09 slowest ~216 s); URL minting ~5%
- 4 workers: 2.36x speedup -> ~28 h projected (~86 obs/h)
- ~213 MB transferred per observation -> ~514 GB total; peak RSS ~3.2 GB
- If saved as float32 deflate GeoTIFF: ~158 MB/obs -> ~380 GB

Open decision: `run` currently reads every observation but does **not**
save it (timing-only; only `pilot` writes files, to `pilot_output/`). If
the user wants to keep the imagery, pass a `save_dir` to `acquire_one` in
`cmd_worker` (or write uint16 to roughly halve the ~380 GB) — confirm with
the user and check free disk space first.

When the run finishes: summarize `records.jsonl` and `attempts.jsonl` with
`report`, compare against the pilot baseline above, and put a compact
summary in `docs/benchmarks/data/` (raw results stay git-ignored).
