# data-loader: notes for AI coding assistants

DataLoader is eMapR's config-driven imagery-acquisition front end:
YAML/JSON request in; GeoTIFFs + `manifest.json` (schema
`dataloader-manifest` 1.0) + `items.jsonl` + verbatim source metadata out.
Users start at `README.md`; contributors at `docs/development/README.md`.

## Layout
- `data_loader/`: `config.py` (schema v1, strict), `engine.py` (discover ->
  units -> resumable acquisition), `dataset.py` (output contract: writer +
  `open_dataset` reader), `providers/` (one module per source; interface
  in `providers/base.py`), `tiles.py`, `masking.py`, `geotiff.py`, `cli.py`
  (`data-loader validate|plan|run|status|verify`).
- `examples/`: runnable configs. `docs/`: user docs.
  `docs/development/`: architecture, benchmark methodology/results,
  development report (history of findings).
- `bench/`: benchmark harnesses. Raw output in git-ignored `bench/results/`;
  compact summaries -> `docs/development/benchmarks/data/` and figures ->
  `docs/development/figures/`, always via scripts. Four harnesses use the
  pre-1.0 API (marked in their docstrings; run them from the `pre-1.0` tag).
- Tests: `pytest` (network-free; `tests/fakes.py` has a fake provider).

## Invariants worth protecting
- One engine for AOI pulls and tile archives. Don't add a separate archive
  code path; add config options instead.
- Defaults keep source data: no scene cloud filter (100), no pixel masking.
  Masking is opt-in and its exact QA rules go into the manifest.
- Config keys are strict at every level. A new key needs validation, docs
  (`docs/configuration.md`) and tests.
- Manifest/items changes: additive within 1.x; anything else bumps the
  schema major version and `dataset.MANIFEST_VERSION`. Document in
  `docs/outputs.md` and `CHANGELOG.md`.
- Credentials only from environment variables; never write
  `provider_options` values or tokens to outputs or logs.
- Don't assume "open data" is free or anonymous: check (Requester Pays,
  USGS M2M accounts, one-request-at-a-time per account).

## How the maintainers like to work
- Tools are config-file driven (YAML/JSON in, plain files + manifest out),
  not Python functions with growing keyword lists.
- Calculations, result aggregation and figures go in scripts, with short,
  targeted terminal output. Reusable analysis scripts are committed under
  `bench/`.
- Measure before optimizing; never trade product equivalence or provenance
  for speed. Record significant findings in
  `docs/development/DATALOADER_DEVELOPMENT_REPORT.md`.
- Long USGS runs: run them in tmux, and never two runs on one M2M account at once.
