# Troubleshooting

Each entry starts with the error message, or a description of what you
see.

## Install and environment

**`ModuleNotFoundError: No module named 'rasterio'`** (or `data_loader`)
: You're running a different Python than the one you installed into.
  Activate the virtualenv (`source .venv/bin/activate`) or call
  `.venv/bin/data-loader` directly. On shared servers the system `python`
  usually lacks these packages.

**`data-loader: command not found`**
: Run `pip install -e .` in the repo with the virtualenv active, or use
  `python -m data_loader ...`.

## Config

**`unknown key 'X' (did you mean 'Y'?)`**
: The config has a typo or a key that doesn't exist. See
  [configuration](configuration.md).

**`this config has no version: key ...`**
: The config uses the pre-1.0 format. See
  [Migrating from pre-1.0 configs](configuration.md#migrating-from-pre-10-configs).

**`the command line changed in 1.0`**
: Use `data-loader run CONFIG` instead of `python -m data_loader --config CONFIG`.

**`time: the requested period has no dates up to today`**
: Every requested window starts in the future. A wrapped season that
  starts later this year produces this too.

**`output.encoding: native ... provider X can't return source values`**
: Native encoding and QA bands are supported by `usgs_ard`,
  `planetary_computer` and `aws_earth_search`. Use `encoding: float32` and
  `qa_band: false` with the other providers.

## Output directory

**`... already holds a different dataset (request differs in: ...)`**
: One directory holds one dataset. Only the end of the time range,
  `workers`, `max_attempts`, `max_consecutive_failures` and `provider_options` may change between runs.
  Use a new `output.dir` (or `--output-dir`) for a different request.

**`... is not empty and has no manifest.json`**
: Point `output.dir` at an empty or new directory. DataLoader won't write
  into an unrelated directory.

**`another DataLoader run is using this directory`**
: A run is already in progress there (check `tmux ls` and `ps`). The lock
  is released automatically when that process exits, even if it crashed.

**`product_family ... is not the agency's source product`**
: The provider supplies a derived product (GLAD normalized composites, GEE
  harmonized Sentinel-2). Name it with `sensors[].product_family` if that
  is what you want.

**`processing_version_policy 'pinned:...': ... no processing versions to choose from`**
: Only Sentinel-2 has processing baselines. Use `any` for Landsat.

**`manifest schema 2.x was written by an incompatible DataLoader`**
: A newer DataLoader wrote this dataset. Upgrade DataLoader; this version
  won't modify it.

## Run stopped (exit code 5)

**`stopped: output disk: No space left on device`** (or quota, read-only)
: Free space (or fix the mount) and run the same command again. The
  acquisition that hit it was not counted against `max_attempts`.

**`stopped: ... M2M login-token failed ... AUTH_...`**
: The USGS token was rejected or has expired: generate a new one, update
  `USGS_M2M_TOKEN`, and re-run.

**`stopped: still 20 failures in a row after pausing ...`**
: The source has been failing for about two hours (the circuit breaker
  paused 5, 15, 30 and 60 minutes first). Check the provider's status
  page and re-run later; none of those failures were counted. To turn the
  breaker off, set `max_consecutive_failures: 0`.

## USGS (`usgs_ard`, `usgs_m2m`)

**`credentials are not accepted in provider_options`**
: Move `usgs_username`/`usgs_token` out of the config into
  `USGS_M2M_USERNAME`/`USGS_M2M_TOKEN` in the environment.

**`SHA-512 of the downloaded file does not match the STAC item's file:checksum`**
: The download was corrupted, or USGS republished the file after the
  catalog was read. Single cases are retried automatically; if it happens
  for every file, re-run (discovery re-reads the catalog) and report it.

**`usgs_ard reads need a USGS EROS account with M2M access`**
: Set `USGS_M2M_USERNAME` and `USGS_M2M_TOKEN`. See
  [getting started](getting-started.md#usgs-eros-account-and-m2m-token-usgs_ard-usgs_m2m).

**`AUTH_INVALID` from M2M login**
: The usual cause is the token. Generate a new application token in your
  ERS profile and update `USGS_M2M_TOKEN`. Also check that the account has
  M2M access approved.

**`RATE_LIMIT: Your account does not support multiple requests at a time`**
: Another process is using the same USGS account: a second DataLoader
  run, an old benchmark job, or another machine. DataLoader retries, but
  both jobs slow down. Run one job per account.

**Lots of `transport retry ... 504 Server Error: Gateway Time-out` lines**
: landsatlook.usgs.gov is slow or overloaded. These are retried with
  backoff, and an observation is marked failed only after repeated
  failures. Failed observations get retry passes before the run moves on
  to the next tile (up to `max_attempts` in total). Persistent
  504s for hours usually mean a USGS outage; stop and resume later.

**`STAC search retry ... Internal server error`**
: The LandsatLook STAC API returns occasional 500s. They're retried; if
  discovery still fails, re-run.

## Planetary Computer / Earth Search

**Errors mentioning `403`, `AuthenticationFailed` or `Signature`**
: An expired Planetary Computer signed URL. DataLoader re-signs and
  retries automatically. If it persists, re-run.

**`reads from a Requester Pays S3 bucket`**
: `aws_earth_search` Landsat needs AWS credentials and costs money. Use
  `planetary_computer` or `usgs_ard` for free Landsat.

## Results look wrong

**Sentinel-2 reflectance ~0.1 too high in data from before DataLoader 1.0**
: Planetary Computer data from processing baseline ≥ 04.00 (2022 onward)
  needs offset −0.1, which older versions didn't apply. See
  [providers](providers.md#sentinel-2-reflectance-offset-both-stac-providers).

**Clouds in the output**
: Expected: masking is off by default. Either set `filters.pixel_cloud_mask:
  true`, or keep `output.qa_band: true` and mask downstream (see
  [outputs](outputs.md#reading-a-dataset)).

**Mostly empty tiles**
: Edge "slivers" from neighboring satellite paths are mostly fill. Check
  `fillPercent` / `validFraction` in `items.jsonl` and filter on them
  downstream.

**Memory errors with `annual_composite` over big areas**
: A composite holds all of its scenes in memory at once. Use smaller AOIs
  or seasons, or acquire `scene` mode and composite downstream.

## Getting more detail

- `data-loader status DIR` lists failed observations and their last
  errors; `items.jsonl` has every error in full.
- Run logs are timestamped. Use `tee -a run.log` to keep them.
- `pytest` checks the installation without network access.
