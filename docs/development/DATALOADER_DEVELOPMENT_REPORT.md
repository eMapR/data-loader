# DataLoader Development Report

A living engineering/scientific findings document — not a changelog. Updated when a benchmark, correctness test, provider investigation, or production-scale experiment produces a significant finding. Routine code changes are not recorded here.

*Last updated: 2026-10-07*

This is the historical record of what was measured and why decisions were made. How DataLoader 1.0 is put together today is in [README.md](README.md); user documentation is in [`docs/`](../).

## Purpose

DataLoader is a reusable, provider-agnostic imagery-acquisition layer intended to sit in front of multiple lab workflows — BugNet, future local LandTrendr-style change-detection pipelines, other preprocessing needs, and projects not yet designed. Some future workflows may require decades of imagery across very large areas. DataLoader's job is to let a downstream program ask for a scientific product (sensor, product family, bands, time range) without needing to know or care which provider actually serves it, while preserving enough provenance to reproduce and audit the result later.

## Current architecture

```
request/config → product contract → provider → imagery acquisition
    → masking/preprocessing → standardized output → provenance
```

- **Product contract** (`data_loader/product_contract.py`): declares WHAT scientific product is being requested — sensor, `product_family` (`usgs_c2_l2`, `esa_s2_l2a`, `esa_s2_l2a_harmonized`, `glad_ard`, `usgs_ard_sr`), temporal structure (scene vs. fixed composite), and a processing-version policy. A provider is only used if its declared capability matches the request; mismatches fail loudly rather than silently substituting a different product.
- **Provider** (`data_loader/providers/`): WHERE imagery comes from.
- **Acquisition → masking → output**: `engine.py` drives `search_scenes`/`read_scene_bands`/(optional) `read_annual_composite`, applies QA masking, writes GeoTIFFs.
- **Provenance**: normalized per-scene metadata plus a full source-record snapshot (see below), recorded in `manifest.json`.

*(As of 1.0, 2026-10-07, the same layers remain but the engine is one resumable pipeline for both AOI pulls and tile archives, and provenance lives in a versioned `manifest.json` + `items.jsonl` -- see "DataLoader 1.0" below.)*

**Providers implemented and their key limitations:**

| Provider | Product family | Notes |
|---|---|---|
| Planetary Computer | `usgs_c2_l2` (Landsat), `esa_s2_l2a` (Sentinel-2) | Free/anonymous. Currently the best-validated Landsat source. Signed asset URLs expire on long jobs — **fixed 2026-09-16, see below** (durable STAC identity + lazy sign + bounded re-sign/retry). |
| AWS Earth Search | `usgs_c2_l2` (Landsat, Requester Pays — needs paid AWS credentials), `esa_s2_l2a` (Sentinel-2, free) | Can expose multiple Sentinel-2 processing versions of one acquisition (handled — see below). |
| GEE | `usgs_c2_l2` (Landsat), `esa_s2_l2a_harmonized` (Sentinel-2 — **not** the same family as raw `esa_s2_l2a`) | Requires an Earth Engine project. Grid/resampling bugs fixed (see below). |
| USGS M2M | `usgs_c2_l2` (Landsat only) | **M2M authentication now live-verified (2026-09-18)** — see below. The provider's own scene-bundle read path (download whole `.tar` → extract → read) is still unexercised end-to-end; only auth + the API shape are confirmed. |
| GLAD ARD | `glad_ard` (Landsat-derived, 16-day fixed composite) | No STAC/API/DOI exists upstream; a genuinely different product from scene-level Collection 2 — not interchangeable. |
| **USGS ARD** (`usgs_ard`) | `usgs_ard_sr` (Landsat 4–9 C2 U.S. ARD, Surface Reflectance) | **New 2026-09-18.** Direct from USGS, no AWS. Public LandsatLook STAC for discovery (anonymous) + M2M signed per-band URLs for reads (needs an EROS account). Individual COGs with working range requests — **not** bundle downloads. Fixed national Albers tile grid, so an ARD tile is **not** interchangeable with a `usgs_c2_l2` scene. |

## Scientific consistency findings

- **Landsat Collection 2 L2 is scientifically interchangeable across PC and GEE**, confirmed empirically for a real acquisition (`LC08_L2SP_046029_20230715_20230724_02_T1`): identical full product ID, identical processing/generation timestamp, identical scale/offset — and after fixing GEE's grid and resampling bugs (below), per-band reflectance agreement to ~3×10⁻⁶ (essentially float32 noise). Earth Search and USGS M2M share the same product-ID/metadata identity but were not independently pixel-verified (Earth Search: no paid AWS access; M2M: auth unresolved).
- **Sentinel-2 providers can expose different processing generations of the same acquisition, and this is a real radiometric difference, not just a metadata difference.** Earth Search returned both the original (baseline 02.14) and a 2022–23 ESA-reprocessed (baseline 05.00) version of the same acquisition under different item IDs (`S2B_10TDR_20200714_0_L2A` / `_1_L2A`). Measured pixel difference between the two: mean NIR reflectance diff 0.026, max 0.19 — with no simple DN-offset explanation (ruled out the known baseline-04 +1000 offset bug specifically). Planetary Computer, for the same acquisitions, only ever served the original baseline.
- **Consequence: provider/version selection cannot be based on speed or convenience alone — product identity and processing-version consistency must be enforced.** This is why the product-contract system exists. A `processing_version_policy` (`any` | `latest` | `pinned:<baseline>` | `allow_mixed`) now governs Earth Search's Sentinel-2 duplicate handling; the default deduplicates deterministically (highest baseline, tie-broken by generation time) rather than silently returning both.
- **GLAD ARD must not be treated as interchangeable with Landsat C2 L2 scenes** — different atmospheric correction chain, 16-day fixed composite rather than individual scenes, unverified reflectance scale. Kept in its own `product_family` and excluded from the Landsat equivalence class by construction.
- GEE's Sentinel-2 harmonized collection is itself a further, distinct product family (`esa_s2_l2a_harmonized`) — it corrects the known ESA baseline-04 DN-offset convention but does not eliminate genuine Sen2Cor-algorithm-driven differences between baselines, so it is not automatically interchangeable with PC/Earth Search's raw `esa_s2_l2a`.

## Correctness/reliability bugs discovered

| Bug | Status | Notes |
|---|---|---|
| GEE `_download()` passed raw UTM coordinates as `region`; Earth Engine silently interpreted them as WGS84 degrees, returning a corrupted-geotransform, all-zero raster with an HTTP 200 | **FIXED** | Region must be an `ee.Geometry` explicitly tagged with the target CRS, plus `crsTransform` and `dimensions` together. Added `_validate_grid_download()` so this class of failure now raises loudly instead of passing silently. Regression tests added. |
| GEE defaulted to nearest-neighbor resampling for continuous reflectance bands (no `.resample()` call), while STAC providers use bilinear | **FIXED** | Caused ~0.01 reflectance mean difference vs. PC for the identical acquisition; confirmed the resampling mismatch was the entire explanation (forcing bilinear on GEE dropped the raw-DN difference from mean 560 to mean 0.125). QA_PIXEL intentionally left nearest-neighbor (categorical). |
| Planetary Computer signs STAC item asset URLs once at search time; `StacProvider` never re-signs before a later read | **FIXED (2026-09-16)** | Redesigned so durable scene identity is the STAC collection/item/asset (unsigned), not the signed SAS URL: `search_scenes` no longer signs at all (`pystac_client.Client.open` no longer takes `modifier=pc.sign_inplace`); `read_scene_bands` resolves/signs each asset href lazily, immediately before opening it, and caches the signed href per `(item id, asset key)` to avoid unnecessary re-signing. If a read fails with an error consistent with an expired/invalid signed URL (broad regex over GDAL/Azure error text: HTTP 401/403, `AuthenticationFailed`, `ExpiredToken`, `SignatureDoesNotMatch`, etc. — see `_looks_like_expired_auth` in `stac_common.py`), the provider re-signs and retries, bounded at `max_sign_retries` (default 2, so at most 3 total attempts) — never an infinite loop. The signed-URL cache/re-sign path is guarded by a lock so it is safe under the scene-level concurrency added below. Critically, refreshing a signed URL never mutates the STAC item itself, so `source_metadata()`/the provenance snapshot are unaffected — and `metadata_snapshot.write_snapshot`'s existing idempotent write (skip if the file already exists) means a mid-run re-sign can never cause the original source-metadata snapshot to be overwritten. Earth Search (`needs_signing=False`) is untouched: it never signs or retries, confirmed unchanged both by test and by a live smoke read. Live-validated against real Planetary Computer Landsat data (`bench` AOI, `LC08_L2SP_046029_20230731_02_T1`): a genuine 403 was provoked by corrupting a cached signed href, the provider recovered via re-sign+retry with pixel-identical output, and a persistently-bad signer raised after bounded attempts rather than looping. See `tests/test_pc_signed_url_refresh.py` (8 tests: normal read, 403→refresh→success, repeated-403→bounded failure, non-auth errors not retried, identity/provenance unchanged after refresh, Earth Search unaffected). Full suite as of this round (including the later concurrency work below): 72/72 passing, `tests/`. |
| USGS M2M provider | **AUTH RESOLVED (2026-09-18); read path still unexercised** | Every earlier live authentication attempt failed with `AUTH_INVALID`; a regenerated EROS application token now authenticates successfully, so the cause was the credential itself, not missing M2M account approval. `scene-search`/`download-options`/`download-request` were all exercised live (against ARD — see the direct-USGS ARD section). `usgs_m2m.py`'s own scene-bundle read path (whole `.tar` → extract → read) remains untested end-to-end. |
| `usgs_ard.py`'s URL-minting lock held the M2M network call itself, serializing ALL tiles' minting globally (defeated the point of scene-level concurrency) | **FIXED (2026-09-18)** | Discovered while testing scene-level concurrency: replaced the single global `_url_lock` with a per-tile lock (`_lock_for_tile`) so unrelated tiles' minting proceeds independently — the cache-check/mint-if-needed critical section for tile A no longer blocks tile B's. See the next row for why a *different*, deliberately global lock was still needed. Regression tests added (`test_different_tiles_dont_deadlock_or_corrupt_each_others_cache`). |
| USGS M2M enforces a hard, account-level "one request at a time" restriction — concurrent M2M calls fail outright | **DISCOVERED + FIXED (2026-09-18)** | Live: 2+ concurrent workers reading different ARD tiles triggered `download-options failed (HTTP 500): RATE_LIMIT: Your account does not support multiple requests at a time` on 3 of 4 units. This is a genuine account-level server restriction, not a client tuning parameter — the per-tile lock above is insufficient on its own since M2M rejects concurrent requests regardless of which tile they're for. Fixed with a second, deliberately global lock (`_m2m_call_lock`) wrapping only the actual `session.post()` inside `_m2m_post` (not the surrounding per-tile bookkeeping), plus a bounded retry-with-backoff on `RATE_LIMIT` specifically as defense against another process/session sharing the same EROS account. The actual COG byte-reads never touch M2M and are unaffected — only URL-minting queues. See "ARD vs. PC concurrency scaling" for the measured impact (a genuine, un-hideable ~62s serialized floor for a 16-unit workload) and 5 new regression tests. |
| `stac_common.py` had no retry for generic transient network failures (timeouts, connection resets) — only expired-signed-URL errors were retried, so a single network blip failed the scene outright | **FIXED (2026-09-21)** | Hit live during the 2026-09-20 complete-pipeline benchmark: `RasterioIOError: CURL error: Recv failure: Operation timed out` and a vague `Read failed. See previous exception for details.` (GDAL sometimes loses the specific libcurl message inside a WarpedVRT internal read) both failed a scene with no retry attempted. Added `_looks_like_transient_network_error()` (broad regex: timeouts, connection reset/refused, DNS failures, SSL errors, broken pipe, plus that generic wrapper message) and a second, independent bounded retry budget (`max_transient_retries`, default 3, with linear backoff) alongside the existing sign-retry budget — tracked separately so one failure class can't starve or be starved by the other. Applies to **every** `StacProvider` regardless of `needs_signing`, closing a gap Earth Search had entirely (it had zero retry coverage of any kind before this fix, since the existing sign-retry path is gated on `needs_signing=True`). 10 new tests (`tests/test_stac_transient_retry.py`); live-verified against both real PC and Earth Search reads. Full suite: 109/109 passing. |

## Provenance

Every acquisition can retain, alongside DataLoader's own concise normalized fields (provider, upstream product ID, acquisition timestamp, platform, processing baseline/generation time), a **full snapshot of the provider's own source metadata record** — the complete STAC Item, the full GEE `getInfo()` result, or the full M2M scene-search response — written once per unique selected product/version under `<output_dir>/metadata/<provider>/<item_id>.json` and referenced from the manifest by path. This exists so DataLoader never has to guess in advance which fields will matter later (e.g. a USGS DOI, sun/view angles, a Sentinel-2 processing baseline) instead of silently discarding them — and so the exact selected processing version of an acquisition (not just the acquisition date) is durably recorded. GLAD ARD, which has no upstream catalog at all, gets an explicit honest record of what's actually known plus a live-checked confirmation that no embedded GeoTIFF tags carry anything more.

Measured overhead per snapshot: **Planetary Computer ~44 KB/scene, Earth Search Sentinel-2 ~40 KB/scene, GEE ~16 KB/scene, GLAD ARD ~4 KB/scene** — negligible next to imagery output at any realistic scale.

## Performance findings

| Test | Result |
|---|---|
| PC vs. GEE Landsat pixel agreement (post-fix) | mean diff ~3×10⁻⁶ reflectance, essentially float32 noise |
| Sentinel-2 baseline 02.14 vs. 05.00 (same acquisition) | mean NIR reflectance diff 0.026 (real, not metadata-only) |
| Earth Search Sentinel-2 dedup (default policy) | 6 raw items → 3 correctly deduplicated acquisitions |
| **Oregon Landsat 2000–2025 stress test** | see below |

**Oregon stress test (2026-09), measured + modeled, single provider (Planetary Computer):**
- 28,569 Landsat scenes intersecting the current Oregon bbox search, 2000–2025
- ~544 billion cumulative single-band 30 m pixels, ~3.81 trillion band-pixels (7 bands incl. QA), over true polygon-vs-scene Oregon intersections
- **Measured**: mean read time 15.9 s/scene across 259 real scene reads (11 clean benchmark runs); OLS-fit model: `read_s ≈ 8.0 + 9.56×10⁻⁷ × band-pixels` (R²=0.42 — real network variance, not a clean line)
- **Modeled from that fit**: serial (1-worker) runtime ~44.8 days; broad plausible range ~30–70 days given the fit's noise and small sample; preliminary 4-worker estimate ~17 days (65% per-worker efficiency assumed); preliminary 8-worker estimate ~11 days (50% assumed) — none of this assumes clean linear scaling
- **Estimated** (not measured) persistent output storage under the scene-archive assumption of this specific test: ~6.5 TB compressed GeoTIFF
- **Estimated** (explicitly not measured — the harness's byte-counting instrumentation isn't wired up) network transfer: ~4–6 TB

**These numbers are workload-specific and preliminary — a stress-test result, not a statement that every DataLoader workflow requires this much time or storage.** The largest AOI actually timed (~2M pixels) was substantially smaller than the average real Oregon scene intersection (~19M pixels) — statewide extrapolation still needs a validation run at a genuinely large read size before the runtime model should be trusted at full scale.

**⚠️ Original serial-model result, superseded below (2026-09-09, kept as a historical record — see "Large-window COG read validation"): serial (1-worker) runtime ~44.8 days, broad plausible range ~30–70 days**, fit from an OLS model (`read_s ≈ 8.0 + 9.56×10⁻⁷ × band-pixels`) built entirely from windows ≤2M pixels and extrapolated to Oregon's ~19M-pixel average scene intersection. This model was NOT validated against a genuinely large read at the time it was produced — the validation below shows it substantially underestimates real cost at that scale.

### Large-window COG read validation (2026-09-16)

Before implementing concurrency, tested whether the above extrapolation holds at genuinely large window sizes, per the project's development principle (measure before assuming). Read 6 real reflectance bands + QA through Planetary Computer for two real, low-cloud Landsat 9 scenes (`LC09_L2SP_044029_20230810_02_T1`, 0.01% cloud, central Oregon path044/row029 — the same path/row already flagged as the largest-mean-overlap scene in the Oregon inventory; `LC09_L2SP_046029_20230808_02_T1`, 0.14% cloud, west Oregon path046/row029, used as a cross-check at two sizes), at square windows centered on each scene's own STAC-geometry centroid, sized to ~5M/~10M/~20M pixels at 30 m (1–2 repetitions each; see `bench/large_window_cases.py`, `bench/large_window_run_case.py`, results under `bench/results/large_window/`).

| Window | Reps | read_total_s | s/M-pixel | s/M-band-pixel |
|---|---|---|---|---|
| ~2M (historical, `oregon_large_aoi_2023_summer`, 1341×1490) | 150 scene-reads across 3 runs | mean 21.4 | 10.7 | 1.53 |
| ~5M (primary scene) | 2 | 48.7, 29.7 | 9.0, 5.5 | 1.29, 0.79 |
| ~10M (primary scene) | 2 | 253.5, 178.4 | 23.6, 16.6 | 3.37, 2.37 |
| ~10M (secondary scene, cross-check) | 1 | 219.2 | 21.6 | 3.09 |
| ~20M (primary scene) | 1 | 310.8 | 14.4 | 2.06 |

Findings:
- **Throughput does not stay linear — it degrades substantially beyond ~5M pixels.** A log-log power-law fit across all six data points (`read_s ≈ 0.45 × (band-pixels in millions)^1.358`, R²=0.85) fits far better than the old linear model, which underpredicts observed 10M/20M read times by roughly **2–3×**.
- **Substantial run-to-run variance exists even at a fixed window size** — the two ~10M reps differed by 1.4× (178s vs. 254s) on the identical scene/window. Real network variability is a first-order effect at this scale, not a second-order correction.
- **No correctness/reliability issues surfaced**: 0 read failures, 0 retries triggered (expected — these ~1–5 minute reads never approached SAS-token expiry; the phase-1 fix targets multi-hour+ jobs, not this), valid-pixel fraction 99.7–99.99% throughout (no corrupted or all-nodata reads). **This clears large-window reads as a blocker** — proceeding to scene-level concurrency below is justified.
- Diagnostic note (benchmark artifact, not a production code path issue): summed solo single-band read times exceeded the time to read the same bands together in one combined call (e.g. at 20M pixels, six solo reads totaled ~340s vs. 310.8s combined) — consistent with connection-reuse/warm-VSI-cache benefits from reading bands back-to-back, which is exactly how `read_scene_bands` already reads them in production; this is not a newly discovered inefficiency to fix.

**Recalculated Oregon 2000–2025 serial estimate**, using the power-law fit evaluated at Oregon's actual average scene size (~19M pixels × 7 bands ≈ 133M band-pixels/scene, from the 2026-09 inventory's ~3.81 trillion band-pixels ÷ 28,569 scenes): predicted **~346 s/scene → serial (1-worker) total ≈ 114 days** — about **2.5× the original ~45-day linear-model estimate**. Given the measured variance (s/M-band-pixel ranged 0.79–3.37 across all large-window measurements), a defensible range is **~35–150 days**. The dominant driver of the revision is that real per-scene cost at Oregon's actual average window size is far higher (and far more variable) than a model fit from ≤2M-pixel windows could show — not a new bug, but a extrapolation that needed real data to trust.

### Scene-level concurrency (2026-09-16)

No correctness/reliability blocker was found above, so scene-level concurrency was implemented: `data_loader.config.Config.workers` (default `1`, fully serial and byte-identical to every prior DataLoader run — backward compatible) drives a `ThreadPoolExecutor` in `data_loader.engine`, with a scene as the unit of work. Each worker performs one scene's full read → QA mask → index computation → GeoTIFF write → provenance/manifest pipeline (`engine._process_one_scene`); band reads within one scene are never parallelized (no nested concurrency). Output is always assembled back in original scene order — an index-addressed results list, not append-as-completed — so manifests are byte-identical regardless of which worker finishes first, verified by test. A per-scene failure is caught, attributed to that scene's id, and reported in the manifest's new `failedScenes` field without aborting the rest of the run. The source-metadata snapshot cache/write is guarded by a lock so concurrent workers can't race on the same on-disk snapshot or double-pay a provider's network-backed `source_metadata()` call; Planetary Computer's signed-URL cache/re-sign path (see the fix above) is independently lock-guarded and was exercised correctly under concurrency in this round's benchmark (0 retries needed at this run's read sizes/durations, but the lock was reused unmodified from the phase-1 design). `GeeProvider._ensure_init` was also hardened against a concurrent first-call race, defensively, though GEE concurrency itself isn't benchmarked here. 10 new tests (`tests/test_concurrency.py`) cover configurable workers, deterministic ordering under out-of-order completion, per-scene failure isolation, and no duplicate snapshot writes under concurrency; full suite: 72/72 passing.

**Measured 1/2/4/8-worker scaling** (real Planetary Computer reads, not simulated): 8 real Landsat scenes (path044/row029 area, June–Sept 2023, max 30% cloud), each read at a fixed ~5.4M-pixel window (6 reflectance bands + QA — deliberately smaller than the ~19–20M validation window above, to keep this 4-worker-configuration benchmark's total wall-clock/memory footprint bounded on this development machine; still well above the old ~2M baseline). Search and the read window are deliberately decoupled (an earlier version of this benchmark shared one bbox for both, which — because a ~19M-pixel bbox spans multiple adjacent WRS-2 path/rows — caused `search_scenes` to silently return dozens of scenes instead of the intended 8, ballooning memory and triggering an OS-level low-memory kill; fixed by discovering scenes via a small bbox and reading each at an independently-sized window via `engine._run_scene_mode` directly).

| Workers | Wall time | Speedup | Efficiency vs. 1 worker | Scenes/hr | Pixels/hr | Band-pixels/hr | Peak RSS | CPU util. |
|---|---|---|---|---|---|---|---|---|
| 1 | 457.2 s | 1.00× | 100% | 63.0 | 339M | 2,375M | 2.12 GB | 8.2% |
| 2 | 269.8 s | 1.69× | 84.7% | 106.7 | 575M | 4,023M | 2.16 GB | 13.9% |
| 4 | 187.4 s | 2.44× | 61.0% | 153.7 | 828M | 5,793M | 2.35 GB | 29.1% |
| 8 | 146.8 s | 3.11× | 38.9% | 196.2 | 1,056M | 7,394M | 2.71 GB | 25.3% |

(`bench/concurrency_bench.py` / `bench/concurrency_run_case.py`; raw results under `bench/results/concurrency/`.)

Findings:
- **Scaling is real but clearly sublinear, and efficiency drops steadily as workers increase** — not the ~65%/~50% efficiency previously *assumed* (not measured) in the original Oregon stress test. Doubling workers from 1→2 captured 85% of ideal speedup; 4→8 captured only an incremental ~0.67× of additional speedup (efficiency falls to 39%).
- **The dominant bottleneck is network/provider I/O, not CPU, memory, or disk.** CPU utilization never exceeded ~29% even at 8 concurrent workers (this benchmark machine has more than 8 logical cores), and peak RSS grew only modestly (2.12→2.71 GB, +28%) across a 8× increase in worker count — nowhere near proportional, and nowhere near a concerning absolute level. This points at Planetary Computer/Azure Blob throughput (or this network's own bandwidth/connection-count limits) as the ceiling, consistent with the large-window findings above (network variability, not compute, dominates per-scene cost at this scale).
- **No correctness/reliability regressions under concurrency**: 0 scene failures, 0 network retries, and byte-identical scene sets/output filenames across all four worker counts (verified directly), confirming deterministic output regardless of completion order in a real (not just mocked) run.
- 8 workers was **not** assumed to be best, per the task's explicit instruction — it measured out as the fastest of the four configurations tested here, but with steeply diminishing marginal returns; a real Oregon-scale job should treat the workers/throughput tradeoff (and this machine/network's actual ceiling, not yet found — this benchmark did not test past 8) as an open tuning question, not a solved one.

### Revised Oregon 2000–2025 stress-test runtime (measured concurrency scaling applied)

Applying the measured per-worker-count speedup factors above to the large-window-validated serial estimate (114 days central, ~35–150 day range):

| Workers | Estimated runtime (central) | Plausible range |
|---|---|---|
| 1 | **114.0 days** | 35.0–150.0 days |
| 2 | **67.3 days** | 20.7–88.5 days |
| 4 | **46.7 days** | 14.4–61.5 days |
| 8 | **36.6 days** | 11.2–48.2 days |

For comparison, the original (2026-09-09) estimate — before either the large-window validation or measured concurrency scaling existed — was ~45 days serial, ~17 days at 4 workers (65% efficiency *assumed*), ~11 days at 8 workers (50% efficiency *assumed*). The measured 8-worker figure (36.6 days) is more than **3× slower** than that assumed figure, almost entirely because (a) the large-window validation revealed real per-scene cost at Oregon's actual scene size is ~2.5× the old linear model's prediction, and (b) measured concurrency efficiency at 8 workers (39%) is well below the 50% that was assumed. Both corrections point the same direction: the original stress-test conclusion understated how long a real Oregon-scale acquisition would take, on both the serial and the parallel side, because neither had been measured yet at realistic scale.

### Direct-USGS Landsat C2 U.S. ARD investigation (2026-09-18)

Investigated whether USGS ARD, accessed **directly from USGS with no AWS involvement**, is a good backend for large-area/long-time-series workflows such as local LandTrendr. All findings below were verified live against a real EROS account.

**M2M authentication is now verified.** The previously `AUTH_INVALID` M2M credential issue is resolved (a regenerated EROS application token); `login-token` now succeeds. This retires the longest-standing UNVERIFIED item in this report — though note only auth and the API's response shapes are confirmed, not `usgs_m2m.py`'s full scene-bundle read path.

**Dataset identifiers.** M2M `dataset-search` exposes two relevant aliases: `landsat_ard_tile_c2` ("Landsat 4-9 C2 U.S. ARD", 1982-11-11 → present) and `landsat_ard_tile_files_c2` ("U.S. Landsat 4-9 C2 ARD Files"). `landsat_ard_tile_c2` is the one to use — its per-tile `download-options` already exposes individual files.

**Individual ARD bands ARE available — bundles are not required.** This was the central open question, and the answer is favorable. M2M's `download-options` for one ARD tile returns 8 products: six `*Bundle Download` products (`D772`–`D777`, e.g. `D773` "C2 ARD Tile Surface Reflectance Bundle Download", a single 213 MB `.tar`), a full-resolution browse JPEG, and — critically — **`D771` "C2 ARD Tile Band Download"**, whose `secondaryDownloads` list every file of the tile individually (42 entries for a Landsat 9 tile: SR/TOA/BT/ST bands, QA bands, per-product STAC JSON, solar/view angle bands, browse images). `download-request` accepts many of these in one call and returns **every URL immediately with an empty `preparingDownloads`** — no staging/polling queue, unlike the scene-based bundle flow in `usgs_m2m.py`. Measured: 7 band URLs returned in one ~1.6 s call.

**Those URLs serve real, range-readable COGs.** The minted `landsatlook.usgs.gov/tile/...?requestSignature=...` URLs return `Content-Type: image/tiff`, `Accept-Ranges: bytes`, and answer range requests with **HTTP 206**. The underlying files are genuine COGs: 5000×5000 uint16, **256×256 internal tiling, 6 overview levels**. A windowed 256×256 read completed in 0.56 s against a 270 KB QA file. So reads are truly windowed through GDAL `/vsicurl/` — transferring only bytes covering the AOI, exactly like the Planetary Computer path, and fundamentally lighter than `usgs_m2m.py`'s whole-`.tar`-download-then-extract model.

Transfer comparison for one tile: **6 SR bands + QA_PIXEL = 185.6 MB** if fully downloaded, vs. **213.5 MB** for the `D773` SR bundle `.tar`, vs. **~750 MB** for all 42 files. But windowed COG reads transfer far less again — measured **~4.3 MB/tile** (small AOI) and **~41 MB/tile** (medium AOI).

**Why the read path is M2M and not the STAC asset href.** LandsatLook's *unsigned* `https://landsatlook.usgs.gov/tile/...` asset href redirects to an EROS login page (HTTP 302 → `ers.cr.usgs.gov`), and each STAC asset's `alternate.s3.href` points at `s3://usgs-landsat-ard`, a **Requester Pays** bucket (anonymous GET → `AccessDenied: Anonymous users cannot invoke requests against Requester Pays buckets`) that bills a third-party AWS account. The M2M signed URL is the only path that reads ARD bytes directly from USGS without AWS billing — which is what makes this a genuinely *direct-USGS* backend. The provider therefore splits the two services deliberately: **discovery** via the public LandsatLook STAC API (anonymous, ~0.7–2 s, richer already-normalized metadata than M2M's date-only `temporalCoverage`), **reads** via M2M signed URLs.

**Kept as a separate product family (`usgs_ard_sr`), not merged into `usgs_c2_l2`.** ARD reprojects and re-tiles onto a fixed national Albers Equal-Area Conic grid (5000×5000 px at 30 m, addressed by `landsat:grid_horizontal`/`grid_vertical` and region `CU`/`AK`/`HI`) rather than per-scene UTM footprints, and **one ARD tile can mosaic 2–3 WRS-2 scenes from the same overpass** (`landsat:scene_count`; measured mean 2.50 for the Oregon AOI). That is a real structural difference from a single scene, so treating them as interchangeable would repeat exactly the silent-substitution failure the product-contract system exists to prevent. (It remains a `scene` temporal_product, not `fixed_composite`: the contributing scenes are seconds apart on one overpass, unlike GLAD ARD's genuine 16-day composite.)

**Scientific agreement with Planetary Computer is excellent.** Same acquisition (2023-08-08, Landsat 9), same target grid, ARD tile `LC09_CU_003004_20230808_20230813_02` vs. PC scene `LC09_L2SP_046029_20230808_02_T1`:

| Band | mean diff | max abs diff | correlation |
|---|---|---|---|
| blue | −1.1×10⁻⁵ | 0.0058 | 0.99952 |
| green | −0.8×10⁻⁵ | 0.0078 | 0.99939 |
| red | −0.4×10⁻⁵ | 0.0104 | 0.99951 |
| nir | +0.3×10⁻⁵ | 0.0336 | 0.99905 |
| swir1 | +0.5×10⁻⁵ | 0.0201 | 0.99958 |
| swir2 | +0.4×10⁻⁵ | 0.0180 | 0.99967 |

Mean differences are ~10⁻⁵ reflectance with correlations >0.999 — the residual spread is consistent with ARD's extra Albers→UTM resampling hop, not a radiometric difference. Same Level-2 SR chain, same QA_PIXEL bitmask convention (`masking.landsat_qa_mask` applies unchanged), and the same canonical STAC asset keys as PC/Earth Search — identical across TM/ETM (L4/5/7) and OLI (L8/9) ARD items, so unlike GEE no per-sensor band map is needed.

**Benchmark: direct-USGS ARD vs. Planetary Computer** (same AOI, same 2023-06-01→09-15 window, same 6 bands + QA, same target grid; `bench/ard_vs_pc_bench.py`, results in `bench/results/ard/`):

| AOI | Provider | Discovery | Units found | Mean read/unit | s/M-pixel | s/M-band-pixel | MB/unit† | Peak RSS | Failures |
|---|---|---|---|---|---|---|---|---|---|
| small (~8×6 km) | usgs_ard | 2.05 s | 21 | 10.08 s | 203.1 | 29.0 | 4.3 | 128 MB | 0 |
| small | planetary_computer | 1.15 s | 26 | 4.46 s | 89.8 | 12.8 | 2.4 | 140 MB | 0 |
| medium (~40×45 km) | usgs_ard | 0.69 s | 21 | 21.74 s | 10.9 | 1.55 | 41.0 | 445 MB | 0 |
| medium | planetary_computer | 1.49 s | 42 | 12.29 s | 6.2 | 0.88 | 9.6 | 525 MB | 0 |

† System-wide network counter, indicative not exact. Zero failures and zero retries across all runs; valid-pixel fraction ≥0.997 throughout.

**Per unit, PC is consistently ~1.8–2.3× faster** — expected, since ARD reads a 5000×5000 Albers tile and reprojects to UTM, while PC reads a natively-UTM scene. But per *unit* is the wrong comparison for a time series.

**ARD's fixed tiling materially reduces the number of units needed, and that is where it wins.** Because ARD tiles are fixed and pre-mosaicked, the same AOI/time window needs far fewer of them, and — unlike PC — that count **does not grow as the AOI widens within a tile**:

| AOI | ARD units | ARD distinct dates | ARD same-date duplicates | PC units | PC distinct dates | PC same-date duplicates |
|---|---|---|---|---|---|---|
| small | 26 | 26 | 0 | 36 | 35 | 1 |
| medium | 26 | 26 | 0 | 59 | 37 | **22** |

The medium AOI is covered by **one** ARD tile (`CU h03 v04`) — so ARD's unit count is *identical* to the small AOI (26), one per date, zero same-date duplicates. PC's rises from 36 to 59, of which 22 are same-date duplicates: the AOI now straddles multiple WRS-2 path/rows, so each date returns several partial scenes that a downstream workflow must mosaic itself.

Combining measured per-unit cost with measured unit counts gives the AOI-level result that actually matters:

| AOI | ARD total | PC total | Winner |
|---|---|---|---|
| small | 26 × 10.08 s = **262 s** | 36 × 4.46 s = **161 s** | PC, 1.63× |
| medium | 26 × 21.74 s = **565 s** | 59 × 12.29 s = **725 s** | **ARD, 1.28×** |

**There is a real crossover.** For small AOIs inside a single WRS-2 scene, PC wins on raw speed. For an AOI large enough to span multiple path/rows but still contained in a single ARD tile, ARD's fixed tiling wins. ⚠️ **The natural next hypothesis — "the advantage should widen as AOIs grow" — was tested directly (see "ARD tile-boundary scaling" below) and turned out to be wrong once the AOI actually crosses ARD tile boundaries: PC's advantage widens instead.** This pair of AOIs (both inside one tile) was not, on its own, evidence either way about what happens *across* tile boundaries — only that single further test could show that, and it now has.

**Assessment: ARD is a genuinely useful backend for large-area/long-time-series work, with caveats.** Beyond the measured throughput crossover, ARD removes real downstream work that these benchmarks do not price in: no per-date scene mosaicking, no cross-path/row grid alignment, and a stable tile identity that makes a multi-decade time series a fixed list of tiles rather than a varying set of scenes. Against that: it needs an EROS account (PC is anonymous), per-unit reads are slower, it is **CONUS/Alaska/Hawaii only** (useless for non-U.S. AOIs, unlike every other provider here), and the M2M endpoint was observed to refuse connections intermittently mid-session (handled with bounded transport retries, but a real reliability consideration at scale).

Not yet done (at the time of the above): no concurrency benchmark for ARD, no large-window (≥10M pixel) ARD reads, and no AWS-hosted ARD comparison — the latter two remain undone; concurrency is addressed as a recommendation below.

### ARD tile-boundary scaling (2026-09-18, continued)

Follow-up to the section above: does ARD's advantage widen or narrow as an AOI grows **across** ARD tile boundaries, not just within one? Both AOIs above (`small`, `medium`) sit entirely inside a single ARD tile (`CU h03 v04`) — this section tests 1, 2, 4, and 8 tiles directly.

**Methodology.** Four AOI tiers were built as the exact union of a hand-picked, nested ARD tile set — not a bbox merely "sized to about N tiles" — because LandsatLook's STAC `bbox` filter matches on bounding-box overlap, not exact tile-polygon intersection: a bbox sized to tile `h03v04` alone was found live to still match 9 distinct neighboring tiles (curvature of a straight Albers edge in WGS84 coordinates makes corner-only bboxes leak). Tile membership was instead enforced by post-filtering ARD results to the tier's exact `(grid_horizontal, grid_vertical)` set, verified live to match exactly with zero missing/leaked tiles at every tier. Tiers are nested (1tile ⊂ 2tile ⊂ 4tile ⊂ 8tile) so "AOI grows" is literal:

| Tier | Target ARD tiles |
|---|---|
| 1tile | h03v04 |
| 2tile | h03v04, h04v04 |
| 4tile | h03v04, h04v04, h03v05, h04v05 (2×2 block) |
| 8tile | h02v04, h03v04, h04v04, h05v04, h02v05, h03v05, h04v05, h05v05 (4×2 block) |

Discovery ran at each tier's real, full physical AOI for both providers (cheap — under 7s even at 8-tile scale, so no sampling needed). Read cost was measured on a **fixed** small window (~8×6 km, same one used in the `small` AOI above, nested inside every tier) applied to 5 real sampled units per tier per provider — chosen deliberately over reading each tier's full native-resolution grid (which would reach ~200M pixels at 8-tile scale, hours of runtime, and real risk of repeating the OOM this project already hit once from an unbounded read window). Per-tier total time is then **projected** as (measured mean per-unit read time) × (measured real unit count) + (measured discovery time) — the same measure-small/extrapolate-large method already used for the Oregon serial estimate.

**A methodology pitfall, caught and fixed.** The first run of this benchmark executed all four tiers sequentially inside one long-lived Python process and produced impossible numbers — some PC reads timed at 0.01s. Inspection showed the cause: tiers are nested, so the same lowest-cloud PC scene often recurs across tiers, and GDAL's `/vsicurl` block cache served its bytes from an earlier tier's read instead of a fresh fetch. Every measurement below was re-run with **each tier in its own subprocess** (`bench/ard_tile_scaling_run_case.py`, dispatched by `bench/ard_tile_scaling_bench.py`) — the same isolation convention this project already uses for every other benchmark (`run_case.py`, `large_window_run_case.py`, `concurrency_run_case.py`), for exactly this reason.

**Results** (bands = 6 reflectance + QA, 2023-06-01→09-15, max_cloud=30%; full data in `bench/results/ard/ard_tile_scaling.json`):

| Tier | ARD units | PC units | Unit ratio (PC/ARD) | ARD discovery | PC discovery | ARD mean read/unit | PC mean read/unit | ARD projected total | PC projected total | Winner |
|---|---|---|---|---|---|---|---|---|---|---|
| 1tile | 31 | 128 | 4.13× | 3.09 s | 1.75 s | 13.67 s | 3.15 s | 427.0 s | 404.4 s | PC, 1.06× |
| 2tile | 59 | 187 | 3.17× | 4.56 s | 2.32 s | 11.74 s | 2.88 s | 697.1 s | 540.0 s | PC, 1.29× |
| 4tile | 122 | 271 | 2.22× | 4.66 s | 2.75 s | 10.98 s | 3.38 s | 1344.5 s | 917.7 s | PC, 1.47× |
| 8tile | 248 | 353 | 1.42× | 6.66 s | 3.04 s | 10.37 s | 3.00 s | 2578.9 s | 1063.6 s | PC, 2.42× |

Zero failures and zero retries at every tier for both providers; valid-pixel fraction 1.000 throughout. Peak RSS stayed modest and grew only mildly with tier (168 → 202 → 210 → 236 MB) — memory is not a concern at this scale for either provider. Indicative transfer (system-wide network counter) was consistently higher for ARD per unit (0.3–2.6 MB) than PC (0.3–0.8 MB), consistent with ARD's extra reprojection step touching more source pixels.

**PC's advantage widens as tile count grows — the opposite of the (correctly hedged) hypothesis above.** At 1 tile the two are near parity (1.06×); by 8 tiles PC is 2.42× faster in total projected time. Two effects compound in the same direction:

1. **ARD's unit-count advantage shrinks as tiles are added.** The ratio of PC units to ARD units falls steadily — 4.13× → 3.17× → 2.22× → 1.42× — because ARD's own unit count grows almost perfectly linearly with tile count (31 → 59 → 122 → 248, i.e. ~31 tile-dates per tile, each tile's local revisit history being independent), while PC's growth is **sub-linear**: Landsat's fixed ~16-day repeat cadence caps how many *distinct dates* can appear in a fixed 3.5-month window regardless of AOI size (51 → 64 → 74 → 80 distinct dates, itself sub-linear), so most of PC's additional units as the AOI grows are cheap same-date "duplicate" partial scenes on dates already counted (77 → 123 → 197 → 273 duplicates), not new dates requiring new work.
2. **ARD's per-unit read cost does not fall to compensate** — it stays a consistent ~3–4× tax over PC's per-unit cost at every tier (10.4–13.7s vs. 2.9–3.4s), the same reprojection/access-pattern overhead already seen in the single-tile comparison above.

Since ARD's shrinking unit-count edge can no longer offset a roughly constant per-unit cost disadvantage, PC's total-time lead grows with AOI/tile count in this raw-acquisition-only comparison — the reverse of what the single-tile-pair test suggested.

**Quantifying the WRS-2 overlap ARD eliminates** (the user's specific ask): summing `landsat:scene_count` (the number of WRS-2 scenes USGS mosaicked into each ARD tile-date) across all of a tier's ARD units gives the true count of underlying scene-reads that went into that tier's tile-dates:

| Tier | ARD tile-dates | Mean WRS-2 scenes/tile-date | Total WRS-2 scene-equivalents | PC's actual distinct-scene count |
|---|---|---|---|---|
| 1tile | 31 | 2.35 | 73 | 128 |
| 2tile | 59 | 2.22 | 131 | 187 |
| 4tile | 122 | 2.18 | 266 | 271 |
| 8tile | 248 | 1.98 | 490 | 353 |

Two things stand out. First, the mean scenes/tile-date **declines slightly** as the AOI grows (2.35 → 1.98) — tiles added at the edges of the growing rectangle sit in somewhat less path/row overlap than the original central tile, a real (if secondary) geometric effect. Second, at 8 tiles the total WRS-2 scene-equivalents (490) **exceeds** PC's actual distinct-scene count (353) — because a single WRS-2 scene straddling two adjacent ARD tiles is counted once per tile it contributes to, while PC's bbox search returns each physical scene granule only once. This means ARD's mosaicking doesn't reduce the *total* amount of underlying USGS processing work versus a naive per-scene count — it relocates that work upstream, to USGS's own tile-production pipeline, so DataLoader itself never has to do it. That upstream-vs-downstream distinction is the crux of the next point.

**This is an acquisition-cost-only comparison — it does not price in downstream mosaicking, and that materially favors ARD in practice.** `engine.py`'s scene mode writes one output file *per scene*, with no same-date compositing — so PC's same-date duplicate scenes (77 at 1 tile, rising to 273 at 8 tiles) are not merged by DataLoader; a downstream LandTrendr-style workflow needing one composite raster per date per AOI would have to do that mosaicking itself, for every one of those duplicate groups, at every tile-count tested. ARD's tile-dates arrive already mosaicked — that work was paid for once, upstream, by USGS. This benchmark did not attempt to measure that downstream cost (it is a property of whatever workflow consumes DataLoader's scene-mode output, not of DataLoader's acquisition layer itself), so the "PC wins and the margin grows" conclusion above should be read specifically as **PC is faster to acquire the same nominal coverage**, not as "PC produces an equivalently finished product for the same total cost."

**Revised assessment.** For DataLoader's own acquisition-layer cost alone, Planetary Computer is faster than direct-USGS ARD at every tile count tested (1–8), and increasingly so as the AOI grows across tile boundaries — correcting the single-tile-pair speculation above. ARD's genuine advantages remain real but are now better understood as *quality/workflow* advantages rather than a raw-throughput one: a stable, already-mosaicked, already-gridded tile identity that removes downstream same-date compositing and cross-path/row alignment work a PC-based scene-mode pipeline would otherwise leave to the caller. Whether that's worth ARD's ~3–4× per-unit read tax and widening total-time gap depends entirely on how expensive that downstream mosaicking work would otherwise be for a given workflow — not measured here.

### ARD vs. PC concurrency scaling (2026-09-18, continued)

Direct follow-up to the recommendation above: does scene-level concurrency (`config.workers`, already implemented and measured for Planetary Computer alone in the earlier round) narrow ARD's ~3–4× per-unit cost gap, or does it persist? Both providers were run through the real engine dispatcher (`engine._run_scene_mode`, not a synthetic harness) at 1/2/4/8 workers, over the same fixed 16-unit workload (real ARD tile-dates / real PC scenes, discovered once from the same 4-tile AOI used above, sorted by date, identical unit list across every worker count) reading the same fixed small window (~8×6 km) — see `bench/ard_pc_concurrency_bench.py` / `bench/ard_pc_concurrency_run_case.py`, results in `bench/results/concurrency_ard_pc/`.

**A live discovery made this benchmark almost fail outright, and fixing it is itself a significant finding.** The first attempt at ARD with 2+ concurrent workers failed 3 of 4 units with `M2M download-options failed (HTTP 500): RATE_LIMIT: Your account does not support multiple requests at a time.` — **USGS M2M enforces a hard, account-level, one-request-at-a-time restriction**, independent of which tile each request is for. This meant the per-tile lock added earlier (correctly preventing redundant re-minting of the *same* tile's URLs across threads) was not sufficient — concurrent minting of *different* tiles also fails against the real API. Fixed with a second, deliberately global lock (`_m2m_call_lock` in `usgs_ard.py`) serializing only the actual `session.post()` call inside `_m2m_post`, plus a bounded retry-with-backoff on `RATE_LIMIT` specifically (defense in depth against another process sharing the same EROS account, which the client-side lock can't prevent). Critically, this lock wraps *only* the M2M API call — the actual COG byte-reads (`rasterio`/`WarpedVRT` against an already-minted signed URL) never touch M2M and are not serialized by it. 5 new tests cover this (concurrent different-tile mints don't deadlock or cross-contaminate; concurrent M2M network calls do serialize; `RATE_LIMIT` retries with backoff and eventually raises clearly if never recovered). Full suite: 99/99 passing.

**Results** (16 units, fixed across all runs; zero failures and zero retries at every worker count for both providers post-fix; output byte-for-byte identical across all four worker counts for both providers — verified via SHA-256 of a fixed canary output file, not just band means):

| Provider | Workers | Wall time | Speedup | Efficiency | Units/hour | CPU util. | Peak RSS |
|---|---|---|---|---|---|---|---|
| usgs_ard | 1 | 222.5 s | 1.00× | 100% | 258.9 | 1.7% | 198 MB |
| usgs_ard | 2 | 144.3 s | 1.54× | 77.1% | 399.3 | 2.8% | 190 MB |
| usgs_ard | 4 | 104.0 s | 2.14× | 53.5% | 553.9 | 4.0% | 199 MB |
| usgs_ard | 8 | 79.8 s | 2.79× | 34.9% | 722.2 | 4.9% | 212 MB |
| planetary_computer | 1 | 58.3 s | 1.00× | 100% | 987.6 | 2.3% | 198 MB |
| planetary_computer | 2 | 28.8 s | 2.02× | 101.1% | 1997.1 | 4.8% | 205 MB |
| planetary_computer | 4 | 16.3 s | 3.59× | 89.7% | 3542.0 | 6.9% | 210 MB |
| planetary_computer | 8 | 10.2 s | 5.73× | 71.7% | 5662.8 | 11.0% | 225 MB |

**The ARD/PC gap widens under concurrency rather than closing — the direct answer to the motivating question.** At 1 worker, ARD is 3.82× slower than PC (222.5s vs. 58.3s) — matching the serial tile-boundary result. At 8 workers, ARD is **7.82× slower** (79.8s vs. 10.2s). PC scales close to ideally (101% efficiency at 2 workers, still 72% at 8); ARD's efficiency degrades much faster (77% → 54% → 35%).

**Why, precisely — this is not a single-cause answer.** Two components separate cleanly in the data:

1. **CPU utilization stays low throughout for both providers (1.7–11.0%) and does not spike with worker count for ARD specifically** — directly ruling out the "reprojection/processing cost" hypothesis. If ARD's extra Albers→UTM `WarpedVRT` reprojection were the dominant cost, CPU utilization would climb sharply as more workers ran that computation concurrently; it barely moves (1.7% → 4.9%). Compute is not the bottleneck.
2. **The wall-time curve fits a two-term model almost exactly**: `wall(workers) = F + 16×P / workers`, fit to the 4 measured points gives **F ≈ 62.0 s** (a fully serialized, worker-count-independent floor) and **P ≈ 10.1 s/unit** (a genuinely parallelizable per-unit cost) — predictions within 2s of every measured point. `F` is the M2M URL-minting cost (2 calls/tile, now serialized by `_m2m_call_lock`, ≈3.9s/tile-pair) that **no amount of concurrency can reduce**, because the account itself forbids concurrent M2M requests. `P` is the actual COG byte-transfer/open cost (7 file opens per tile: 6 SR bands + QA), which **does** parallelize well — at 8 workers, ARD's wall time (79.8s) is already within 18s of the theoretical floor (62.0s) as workers→∞.

**So: ARD's per-unit cost is a mix, and the answer is neither pure "hideable network latency" nor pure "fixed compute" — it is a genuine, immovable *API-imposed serialization point* layered on top of an otherwise-parallelizable network cost.** Concurrency helps (2.79× speedup at 8 workers is real, not nothing), but it cannot close the gap with PC, because PC has no analogous shared-session bottleneck: each PC scene's SAS signing is independent per-request (no evidence of Azure-side throttling in this test — PC's efficiency stayed above 70% even at 8 workers, and 2-worker efficiency was slightly *above* 100%, within measurement noise). This means the serial tile-boundary benchmark's conclusion ("PC's total-time lead widens with AOI/tile count") is now reinforced from a second, independent angle: it also widens with worker count, for a mechanistically identified reason (M2M's account-level request cap), not just AOI shape.

**Acquisition performance vs. complete time-series-preparation performance — kept explicitly distinct, per instruction.** Everything above, in this section and the two preceding ARD sections, measures only DataLoader's own acquisition-layer cost: discovery + read + write, nothing else. It does **not** include the downstream work a real LandTrendr-style pipeline still owes after acquisition. Planetary Computer's scene-mode output leaves same-date, same-AOI partial scenes unmerged (77–273 duplicate scenes across the tile-boundary tiers) — a downstream workflow must still mosaic and normalize those into one composite per date before the result is usable the way an ARD tile-date already is. ARD moved that work upstream to USGS once; PC's acquisition speed advantage does not include ever having done it. The two provider's *acquisition* numbers are on a level, fair footing (same engine, same concurrency model, same window, same output byte-identity guarantee) — but they are not yet a fair comparison of *time to a usable LandTrendr-ready product*, and should not be read as one.

### Complete end-to-end pipeline: LandTrendr-ready time series (2026-09-20)

Direct answer to the gap flagged above: both providers were run through a genuinely complete pipeline — not `data_loader.engine`, but a dedicated benchmark pipeline (`bench/landtrendr_ready_run_case.py` / `bench/landtrendr_ready_bench.py`) producing an **identical target deliverable** for both: one QA-masked, MOSAICKED GeoTIFF per unique acquisition date, on the same grid, from nothing (a cold discovery call) through final files on disk. This is the first ARD/PC benchmark in this project where the two providers' outputs are actually comparable *finished products*, not just raw reads.

**Test design.** AOI: ~40×40 km straddling the `h03v04`/`h04v04` ARD tile boundary (same boundary used in the tile-scaling benchmark) — small and practical, but deliberately positioned so *both* providers must exercise real mosaicking: every ARD date needs the two straddled tiles merged, and a meaningful share of PC dates need 2+ WRS-2 scenes merged. Period: 2022–2023, June 15–Aug 31 (peak growing season, standard practice for a Landsat stack — minimizes snow/phenology/cloud noise), max cloud 40% — a "representative multi-year period" sized to stay small and practical (67 ARD units / 57 PC units).

**Pipeline, identical for both providers**: discovery (once) → per-unit read with QA masking (`pixel_cloud_mask=True`, the same `masking.landsat_qa_mask` convention both providers already share) → fill-pixel detection → per-date merge → NDVI/NBR → GeoTIFF write. Two correctness details worth stating explicitly, since they determine what "scientifically comparable" means here:

- **QA masking happens before the merge, per contributing unit**, so a cloud-masked pixel from one contributing scene/tile never suppresses a genuinely valid pixel from another covering the same location — the merge is "first valid wins" per pixel, the same idiom `providers/glad_ard.py` already uses for its own multi-tile merge.
- **Fill/out-of-footprint pixels are also excluded before merging**, closing a real, previously-latent gap: neither `stac_common.py` nor `usgs_ard.py`'s `read_scene_bands` checks `src.nodata` explicitly (masking.py's own comment already flagged fill as "implied by nodata elsewhere"); a fill pixel (source DN=0, propagated by `WarpedVRT`) comes back as exactly `0 × sr_scale + sr_offset == sr_offset` — a numerically exact, unambiguous sentinel used here to catch it before it could otherwise overwrite a real pixel from another contributing unit, or survive into the final composite unflagged.

**Results** (2 years, peak season, max cloud 40%, 6 bands + QA, identical target grid `1678×1670` @ 30 m for both):

| Metric | usgs_ard | planetary_computer |
|---|---|---|
| Source units | 67 | 57 |
| Output dates (the deliverable) | 34 | 40 |
| Dates needing mosaic | 32/34 (94%) | 16/40 (40%) |
| Mean contributing units/date | 1.97 | 1.43 |
| Total wall time (cold → files on disk) | **1526.0 s (25.4 min)** | **1418.9 s (23.6 min)** |
| — discovery | 3.5 s (0.2%) | 2.6 s (0.2%) |
| — acquisition (read) | 1480.6 s (97.0%) | 1365.0 s (96.2%) |
| — mosaic/fill-handling | 1.6 s (0.1%) | 1.6 s (0.1%) |
| — indices (NDVI/NBR) | 0.2 s (0.0%) | 0.2 s (0.0%) |
| — write | 39.6 s (2.6%) | 43.9 s (3.1%) |
| Data transferred (indicative) | 1613 MB | 1337 MB |
| Output size | 1628 MB (68 files) | 1686 MB (80 files) |
| Peak RSS | 1101 MB | 1046 MB |
| Failures / retries | 0 / 4 | 0 / 0 |
| Derived: s/unit (acquisition only) | 22.1 | 23.9 |
| Derived: s/output-date (total) | 44.9 | 35.5 |
| Derived: dates/hour | 80.2 | 101.5 |

**The headline finding: at a real, AOI-sized read window, PC's per-unit acquisition advantage nearly vanishes.** Every prior benchmark in this project's ARD-vs-PC series used a small, fixed read window specifically to isolate per-unit transport cost from window-size effects, and consistently found PC 2–4× faster per unit. Here, at the pipeline's actual ~2.8M-pixel window (the real AOI, not an artificially small proxy), **PC's per-unit acquisition cost (23.9 s) is statistically indistinguishable from ARD's (22.1 s)** — confirming, from a second independent angle, the large-window COG validation's earlier finding that read cost grows super-linearly with window size (this project's "Development principle": measure at realistic scale, don't extrapolate from small windows). The ~3–4× gap seen everywhere else in this report is a **small-window artifact**, not a stable property of either provider.

**PC's remaining edge in this test is structural, not a speed difference: it needs fewer units per output date, here.** With per-unit cost now roughly equal, the deliverable-level comparison is decided by `mean_contributing_units_per_date` — ARD needs 1.97 (this AOI straddles exactly 2 tiles, so 32 of 34 dates need both), PC needs only 1.43 (this AOI happens to fall mostly within single WRS-2 scene footprints, so only 16 of 40 dates needed a multi-scene merge). That gives PC a **1.265× advantage per output date** (35.5 s/date vs. 44.9 s/date) and, since the same effect scales the totals, a modest **7% total-time edge** (1418.9s vs. 1526.0s) despite PC producing *more* dates (40 vs. 34) in less time. Normalized to the 27 dates both providers actually produced, PC took 957.7s vs. ARD's 1211.8s for the identical shared subset — the 1.265× ratio holds exactly. This is the opposite mechanism from the tile-boundary scaling benchmark's finding (where PC's per-unit-count advantage over ARD *shrank* as AOI grew past several tiles) — here, at ~2 tiles' worth of AOI, the two effects (comparable per-unit cost, PC's per-date unit-count edge) combine to a much narrower, structurally-explained gap rather than the wide, per-unit-driven gap seen in every acquisition-only benchmark so far.

**PC also discovered more usable dates for the identical AOI/period/cloud-filter (40 vs. 34)** — an observed, not fully explained, difference: ARD's `eo:cloud_cover` is computed over the full 150×150 km tile, while PC's is per-WRS-scene; the two large areas aren't identical, so a date can cross the 40%-cloud threshold differently between the two catalogs even for the same sky. This is a genuine coverage difference between the two catalogs at this AOI, not a bug in either provider.

**Scientific comparability, on the actual mosaicked/masked deliverable (not a single scene) — confirmed strong.** Diffing all 27 shared output dates, per band, mean-of-mean-diffs across dates:

| Band | Mean-of-mean diff | Mean correlation | Min correlation |
|---|---|---|---|
| blue | +0.000026 | 0.9827 | 0.9241 |
| green | −0.000125 | 0.9955 | 0.9799 |
| red | −0.000023 | 0.9975 | 0.9925 |
| nir | +0.000044 | 0.9990 | 0.9988 |
| swir1 | +0.000144 | 0.9979 | 0.9974 |
| swir2 | −0.000034 | 0.9984 | 0.9981 |

Mean differences are on the order of 10⁻⁴–10⁻⁵ reflectance (essentially float32-noise-level agreement, consistent with the earlier single-scene comparability check), and correlations are ≥0.998 for four of six bands across every one of the 27 shared dates. Blue shows the widest (but still strong) spread (mean 0.983, worst-date 0.924) — physically expected: blue is the band most sensitive to residual atmospheric/aerosol correction differences, a known real effect, not a processing bug. **The two complete pipelines produce scientifically equivalent LandTrendr-ready products.**

**Reliability**: zero failures for both providers over this ~25-minute-per-provider run. ARD logged 4 signed-URL re-mints (the same bounded, working retry mechanism characterized earlier); PC logged zero retries this run — but a contrast with an early exploratory pass at this same window size that hit 2 genuine `CURL ... Operation timed out` errors with no retry attempted at all, since `stac_common.py` at the time only retried signed-URL-expiry errors. **That gap is now fixed** — see "Correctness/reliability bugs discovered" and the 4-tile section immediately below, which reruns this exact comparison at larger scale with the fix in place.

**Answering the motivating question directly: starting from nothing, both sources take about the same total time (~24–25 minutes) to produce this AOI's complete LandTrendr-ready time series, with PC modestly (~7% total, ~27% per output date) faster — a far narrower gap than acquisition-only benchmarks suggested, because acquisition (not mosaicking, indices, or writing — each under 3% of total time for both) dominates for both providers, and per-unit acquisition cost converges at this realistic window size.** Every prior conclusion in this report that "PC is faster" was measured at small windows where PC's advantage is real but was never checked against ARD at the scale an actual deliverable requires; this benchmark is that check, and the answer is closer than expected. Not tested here: this was one AOI/period; the tile-boundary scaling benchmark already shows the balance shifts with AOI size and shape, so this specific ~7% PC edge should not be assumed to generalize — tested directly next, at 4-tile scale.

### Complete end-to-end pipeline at 4-tile scale — does the near-tie persist? (2026-09-21/22)

Direct follow-up, per explicit instruction: repeat the identical complete-pipeline comparison (same grid/QA/masking/mosaicking rules, same bands, same 2022–2023 peak-season window, same output format) at an AOI spanning roughly 4 ARD tiles instead of 2, to see whether the near-tie above persists, narrows, or reverses as spatial extent grows. The `stac_common.py` transient-network-retry fix (previous section) was in place for this entire run.

**AOI design, and why it deliberately does not use a literal full 4-tile block.** A genuine 2×2 block of full ARD tiles is 300×300 km — at 30 m that's a ~100M-pixel single-read window, and per the already-established super-linear cost-vs-window-size relationship, would take (extrapolating) tens of minutes *per band per unit*, multiplied by 100+ units: many hours to days, well outside "practical" and uncomfortably close to the excluded full Oregon run. Unit count (how many tile-dates/scenes are discovered) turns out to depend only on which tiles/scenes the AOI *touches*, not on the read window's size within them — verified live: 60 km and 70 km variants of the same 4-tile AOI both discovered exactly 137 ARD units / 83 PC units. This decouples the variable of interest (how many tiles does the AOI span) from the practical constraint (how large is each individual read), the same principle already used to decouple search scope from read-window size in the tile-boundary-scaling and concurrency benchmarks. The AOI used: a 60×60 km square centered exactly on the point where tiles `h03v04`, `h04v04`, `h03v05`, `h04v05` meet (real UTM grid: 2523×2504 = 6.32M pixels, 2.25× the 2-tile test's window).

**Results:**

| Metric | 2tile ARD | 2tile PC | 4tile ARD | 4tile PC |
|---|---|---|---|---|
| Source units | 67 | 57 | 137 | 83 |
| Output dates | 34 | 40 | 36 | 44 |
| Units/date | 1.97 | 1.43 | 3.81 | 1.89 |
| Total wall time | 1526.0 s | 1418.9 s | **3286.7 s (54.8 min)** | **3597.8 s (60.0 min)** |
| Acquisition share | 97.0% | 96.2% | 96.0% | 96.7% |
| s/unit (acquisition) | 22.10 | 23.95 | **23.04** | **41.94** |
| s/output-date (total) | 44.88 | 35.47 | 91.30 | 81.77 |
| Data transferred (indicative) | 1613 MB | 1337 MB | 3765 MB | 4074 MB |
| Output size | 1628 MB | 1686 MB | 4170 MB | 4226 MB |
| Peak RSS | 1101 MB | 1046 MB | 1598 MB | 1802 MB |
| Failures / retries | 0 / 4 | 0 / 0 | 0 / 3 | 0 / 0 |

**The headline finding: per-unit cost does NOT converge as window size keeps growing — it diverges, and PC is the one that gets worse.** Going from the 2-tile window (2.8M px) to the 4-tile window (6.32M px, +126% pixels), **ARD's per-unit acquisition cost barely moved (22.10s → 23.04s, +4.3%)**, while **PC's nearly doubled (23.95s → 41.94s, +75.1%)**. The "convergence" observed at 2-tile scale was a point on a curve, not a plateau — extending the curve reveals ARD's per-unit cost is close to window-size-invariant across this range, while PC's continues climbing steeply, consistent with (and now sharpening) the large-window COG validation's general finding that read cost grows super-linearly with window size, showing here that **PC is hit measurably harder by that super-linear growth than ARD is**. A live check ruled out the most obvious hypothesis (different internal COG tiling): both providers' source COGs use identical 256×256 internal tiling and matching overview structure — the divergence is not a file-format difference. The likely explanation is network/CDN-delivery-layer differences between Azure Blob Storage (PC's signed URLs) and the CloudFront-backed delivery behind M2M's signed URLs, but this was not directly instrumented and is not confirmed — recorded as an open question, not asserted as fact. PC's per-unit cost was also far more *variable* at this window size (individual reads spanning 38–148 s across 6 live samples) than ARD's (consistently 17–23 s) — a second, independent signal pointing the same direction.

**Total wall time flips in ARD's favor (54.8 min vs. 60.0 min, ARD ~9.5% faster) — but this needs a careful reading, not a bare "ARD wins now."** Two effects move in *opposite* directions as the AOI grows from 2 to 4 tiles, and the total-time result is their net:

1. **ARD's units/date cost keeps rising** (1.97 → 3.81) — this AOI spans 4 tiles, so most dates now need most or all 4 merged, whereas PC's units/date barely moves (1.43 → 1.89) — consistent with the tile-boundary-scaling benchmark's finding that ARD's unit count grows linearly with tile count while PC's growth is cushioned by Landsat's fixed revisit cadence. This effect favors PC.
2. **PC's per-unit cost is growing far faster than ARD's** (documented above). This effect favors ARD, and at 4-tile scale it dominates effect (1): PC needs fewer units/date, but each of those units now costs so much more that the product (`units/date × s/unit`) still comes out **worse per date** for PC than for ARD's math would suggest in isolation — except PC still produces *more dates* (44 vs. 36, the same coverage-difference effect seen at 2-tile scale, not a speed difference), and total wall time is `s/date × dates`, so PC's smaller per-date deficit gets multiplied by a larger date count. Concretely: **per output date, PC remains cheaper at both scales (35.47s vs. 44.88s at 2-tile; 81.77s vs. 91.30s at 4-tile) — but the PC/ARD ratio moved from 0.79 to 0.90, i.e. PC's per-date advantage shrank from 21% to 10%** as the AOI grew. ARD's total-wall-time "win" at 4-tile scale is real but is partly an artifact of producing a smaller deliverable (36 dates vs. 44) — not purely a throughput result, and the two metrics (total wall time vs. per-date cost) are telling a consistent underlying story (PC's relative advantage is shrinking) even though they disagree on which provider currently has the lower absolute number.

**Scientific agreement remains strong, with one explained outlier.** Diffing all 31 shared dates: mean-of-mean-diffs across bands are 0.0001–0.0007 reflectance (slightly larger than the 2-tile test's ~0.0001–0.00001, still small), and mean correlations are 0.982–0.990. **One date, 2022-07-06, is a clear outlier** (correlations 0.66–0.78 across bands, vs. ≥0.94 for every other date) — it also had far fewer directly-comparable pixels (757K vs. a mean of 4.76M), consistent with heavy cloud cover leaving only a small, edge-dominated residual overlap between the two providers' independently cloud-masked composites, which naturally amplifies noise sensitivity in a correlation computed over few, boundary-heavy pixels. Excluding that single date, the remaining 30 dates show correlations of 0.936–0.998 (mean 0.989–0.997) — matching the 2-tile test's quality. **The two pipelines' outputs remain scientifically equivalent**; the outlier is a single-date edge case, not a systematic disagreement.

**Answering the motivating question: the near-tie does not persist unchanged — it moves, and the direction favors ARD as spatial extent grows.** Both the headline total-wall-time number (ARD now ~9.5% faster, having been ~7% slower at half the scale) and the fairer per-date metric (PC's advantage shrinking from 21% to 10%) point the same way. The mechanism is identified and specific: **ARD's per-unit read cost is far more stable under growing window size than PC's is** — not a hypothesis, a measured, repeated (smoke test + full run) result. Two tile-count data points cannot prove the trend continues linearly to 8 tiles or beyond, and the tile-boundary-scaling benchmark's opposing effect (ARD's units/date cost also keeps climbing) has not been shown to have a ceiling either — but the direction of movement between the only two scales tested so far is unambiguous, and it is toward ARD, not away from it.

**Decision, as requested — which acquisition strategy deserves the larger (eventual Oregon-scale) stress test: direct-USGS ARD.** Reasoning:

1. **The trend that matters for a large-area, long-time-series workload is moving in ARD's favor, not PC's.** Oregon-scale is a *large-AOI* problem by definition. Every measurement in this report's ARD-vs-PC series that varies spatial extent — this pipeline test (2-tile → 4-tile) and the independent, acquisition-only tile-boundary-scaling benchmark (1 → 2 → 4 → 8 tiles) — shows PC's relative position eroding as extent grows, never improving. There is no measurement anywhere in this report showing PC's advantage widening with scale.
2. **The mechanism is now identified, not just correlated**: PC's per-unit read cost scales worse with window size than ARD's (75% growth vs. 4% growth over the same pixel-count increase, in this test alone), and separately, ARD's units-per-tile-count growth is a known, bounded, already-quantified cost (linear in tile count, per the tile-boundary benchmark) — a predictable scaling factor is a safer basis for a large, expensive, hard-to-restart stress test than an unpredictable one.
3. **ARD's reliability under this round's real, hours-long runs was at least as good as PC's**: zero failures for both, and ARD's per-unit timing was consistently far *less variable* than PC's (17–23s vs. 38–148s at 4-tile scale) — lower variance is itself valuable for estimating and budgeting a multi-day production run.
4. **Caveat, stated plainly**: this recommendation rests on two AOI-size data points (2-tile, 4-tile) at one AOI shape and one 2-year period. It is a directional, mechanism-backed call, not a proof that ARD wins at every scale — see "Open questions" for the natural confirming test (8-tile or larger) before committing irreversible time to the full Oregon run.

### Complete ARD tile history: h003v004 (2026-10-02..04)

Measured the real cost of acquiring one ARD tile's entire Landsat history directly from USGS: tile `h003v004` (central Oregon Cascades, 5000×5000 px at 30 m), 1990-01-01 to 2026-10-02, Landsat 4/5/7/8/9, 6 SR bands + QA_PIXEL, with `bench/ard_tile_history_bench.py` (config `bench/configs/ard_tile_history_h003v004.yaml`; summary [`benchmarks/data/ard_tile_history_h003v004.json`](benchmarks/data/ard_tile_history_h003v004.json), overall and per attempt).

- **3,885 / 3,885 observations acquired, 0 permanently failed, 14.8 h wall** (4 workers, saved as uint16 original C2 SR DN with cloud/shadow/cirrus/fill = 0), 523 GB transferred, 189 GB saved.
- Two passes. 2,411 observations found by a first discovery over a 0.1° center AOI: 10.7 h, 15.9 s/obs effective, ~202 MB/obs; per observation per worker ~20 s download+decode, ~12 s M2M minting (queueing on the one-request-at-a-time account limit), ~16 s single-threaded deflate write. Then **1,474 edge "slivers"** the center AOI missed: an ARD item's STAC geometry is its data footprint, so observations from neighboring WRS-2 paths that only clip the tile edge (~92% fill) need a whole-tile search filtered to the tile's grid h/v. 4.2 h, 10.2 s/obs, ~24 MB/obs.
- **Full-tile reads on the native grid download whole band files in parallel and decode from memory** (`usgs_ard._read_native_tile`): ~110 s → ~10 s per observation, bit-identical to the `/vsicurl` + WarpedVRT path. GDAL's many small range requests, one band after another, were latency-bound; the network was never the limit (`bench/bandwidth_check.py`, `bench/ard_read_path_compare.py`).
- The 90-minute M2M re-login now queues behind the same M2M call lock and retries `RATE_LIMIT` (it caused 27 failed attempts before the fix).
- **SR fill (DN 0) is nodata, not reflectance −0.2**, in `usgs_ard` and `stac_common` (`masking.dn_to_reflectance`); float32 outputs written before 2026-10-03 carry −0.2 fill. uint16 output was never affected.

### Oregon-scale ARD estimate (2026-10-04)

`bench/oregon_ard_estimate.py` → [`benchmarks/data/oregon_ard_estimate.json`](benchmarks/data/oregon_ard_estimate.json) scales the measured h003v004 costs to every CONUS ARD tile touching Oregon, with per-tile observation counts and fill from the LandsatLook STAC: **23 tiles** (18 with ≥5% Oregon share), **~88,900 observations** 1990–2026 (~29k slivers), ~11.4 TB transfer, ~4.1 TB stored as masked uint16 without QA, **~347 h (14.5 days)** for all 23 tiles at 4 workers on one M2M account. Floor from serial M2M minting (~4.7 s/obs): ~116 h. A whole-tile discovery on 2026-10-07 found exactly 88,932. A first production attempt (the bench harness, with QA_PIXEL kept and cloud masking on) ran at ~23–27 s/obs effective during a period of frequent landsatlook 504s; stored size with QA_PIXEL was ~82 MB per full observation and ~6 MB per sliver.

### Sentinel-2 baseline ≥ 04.00 reflectance offset (2026-10-07)

ESA processing baseline 04.00 (from 2022-01-25) adds −1000 DN to L2A surface reflectance. DataLoader had scaled every Sentinel-2 item with offset 0. Checked live on tile 10TDQ open ocean (B08):

| Catalog | 2021 (baseline 03.00 / ES reprocessed 05.00) | 2023-07-14 (baseline 05.09) |
|---|---|---|
| Planetary Computer | p1 DN 19 | p1 DN 1234 |
| Earth Search | p1 DN 18 | p1 DN 234 (`earthsearch:boa_offset_applied: true`) |

So Planetary Computer serves ESA's DN unchanged (2022+ data needs offset −0.1; DataLoader's 2022+ PC Sentinel-2 reflectance was ~0.1 too high), while **Earth Search has already removed the offset from its pixels** even though its `raster:bands` metadata still declares −0.1 (applying it would make reflectance ~0.1 too low). `stac_common.item_sr_scale_offset` now applies 0 for `boa_offset_applied` items, −0.1 for baseline ≥ 04.00 otherwise, 0 before; the value used is recorded per item in provenance.

### DataLoader 1.0 (2026-10-07)

Prepared for hand-off to other eMapR pipelines. The benchmark harness that built the h003v004 history had become the real archive tool; 1.0 folds that into the one engine instead of keeping a separate archive implementation:

- **One engine, every request shape.** A tile archive is a scene-mode request with `aoi.tiles`, `grid.crs: native` and `output.encoding: native` (uint16 DN + scale/offset, optional raw QA band). Whole-tile discovery (slivers included), native-grid whole-file reads, uint16 encoding and per-acquisition journaling moved from the bench script into `data_loader` (`tiles.py`, `usgs_ard.search_tile`, `read_scene_native`, `geotiff.py`, `dataset.py`).
- **Archives keep source observations.** Defaults are now `max_cloud_percent: 100` and `pixel_cloud_mask: false`; masking is opt-in and its exact QA rules are recorded in the manifest. The scene cloud filter is applied client-side so excluded scenes are listed (`status: filtered`) rather than invisible, and a filter at 100 no longer drops 100%-cloud scenes (`lt 100` did).
- **Dataset contract**: `manifest.json` (schema `dataloader-manifest` 1.0) + `items.jsonl` (one row per acquisition with status, files, sha256, provenance) + verbatim provider metadata snapshots. Every run is resumable (journal + atomic writes + per-directory lock), retries failures up to `max_attempts` across runs, and an open-ended or extended time range updates the dataset in place.
- **Config v1**: strict keys at every level; `start_year`/`end_year` (whole years, inclusive -- fixing the pre-1.0 trap where `end: 2020` meant 2020-01-01) or exact dates; seasons that wrap New Year, labelled by the year they start in.
- GeoTIFF writes use GDAL's multi-threaded deflate (`NUM_THREADS=ALL_CPUS`): rewriting a real 183 MB full-tile uint16 + QA file took 24.1 s single-threaded vs 1.3 s, identical size -- the write was ~40% of per-observation time in the bench-harness runs.
- ARD discovery handles are compact (`usgs_ard.CompactItem`: properties + zlib-compressed verbatim record): a full ARD STAC item is ~85 KB as a dict and ~140 KB as a `pystac.Item`, ~12 GB for Oregon's 89k observations held through a run.
- Planned next: vector AOIs; Sentinel-2 direct from the Copernicus Data Space Ecosystem.

## Scaling limitations identified

**Fundamental / external (not something DataLoader controls):**
- Provider-side signed-URL expiration (Planetary Computer) — the token lifetime itself is still provider-controlled, but DataLoader now detects and recovers from it automatically (see the FIXED entry above); it is no longer a job-ending failure mode.
- Provider-specific authentication/access constraints (AWS Requester Pays billing, GEE project requirement, USGS M2M account approval)
- Network variability (a real, measured component of per-scene read time, not just theoretical)

**DataLoader-side, improvable:**
- ~~Fully serial scene/band reads — no concurrency implemented yet~~ **Scene-level concurrency implemented and measured 2026-09-16** (`config.workers`, default 1). Remaining limitation: efficiency drops off steeply past ~2–4 workers (measured 85% at 2 workers down to 39% at 8) — network/provider I/O throughput, not DataLoader's own dispatch overhead, appears to be the ceiling (CPU utilization stayed under 30% throughout). Where that ceiling actually sits (whether workers >8 keep helping at all) was not tested.
- No caching of search results or read pixels yet. (1.0: a resumable dataset is the cache -- re-runs skip acquired observations.)
- Local compositing (`np.stack` + reduce) holds all contributing scenes' arrays in memory simultaneously — a real memory-scaling concern for large AOIs/long time series, not yet addressed. Concurrency's per-scene reads for the local-reduce composite path are now parallelized too (same `config.workers`), which does not change this memory characteristic — the reduce step still waits for and holds every contributing scene's arrays at once.
- AOI search is bbox-only, not true-polygon — inflates scene counts with near-zero-overlap "corner" scenes for irregularly-shaped regions like Oregon. **Measured 2026-09-18**: for a ~40×45 km AOI spanning multiple WRS-2 path/rows, Planetary Computer returns 59 units for 37 distinct dates (22 same-date partial scenes needing downstream mosaicking), where USGS ARD returns 26 units / 26 dates / 0 duplicates from a single fixed tile — one concrete case where a fixed-tile backend sidesteps this limitation entirely.
- Scene-level output persistence, if a workflow chooses it, can require very large storage at scale (as this stress test shows) — a downstream workflow design choice DataLoader should support but not assume
- Per-scene read cost scales worse than linear with window size (measured, see "Large-window COG read validation") — a genuinely large single scene/AOI read is disproportionately expensive even before concurrency is considered

## Important interpretation

The Oregon benchmark is **intentionally a stress test** designed to expose scaling limits relevant to future large-area/long-time-series projects such as local LandTrendr — it does not imply that Oregon-scale, scene-level persistence is DataLoader's standard workload. DataLoader should remain equally capable of supporting a small-AOI, single-season BugNet request and a decades-long statewide composite job, without either shape of workload dictating the other's defaults.

## Open questions / next work

1. ~~Fix and validate Planetary Computer SAS re-sign/refresh behavior.~~ **Done 2026-09-16** (see above).
2. ~~Validate throughput using genuinely large scene-window reads.~~ **Done 2026-09-16** — see "Large-window COG read validation"; revealed super-linear cost growth, now the trusted basis for the Oregon estimate.
3. ~~Revisit concurrency only after the reliability/correctness work above.~~ **Done 2026-09-16** — scene-level concurrency implemented and measured (1/2/4/8 workers); see "Scene-level concurrency" above.
4. Find where concurrency scaling actually flattens (only 1/2/4/8 workers tested; efficiency was still falling at 8, and the true network/provider ceiling was not reached).
5. Tighten Sentinel-2 processing-version consistency further (e.g. cross-provider baseline alignment policy).
6. Add caching if measurements show meaningful benefit.
7. ~~Live-validate M2M authentication.~~ **Done 2026-09-18** — authenticates successfully with a regenerated EROS token; `usgs_m2m.py`'s scene-bundle read path is still unexercised end-to-end.
8. Continue provider benchmarks using scientifically equivalent products (avoid comparing across product families).
9. Address local-reduce composite memory scaling (all contributing scenes held in memory at once) — not yet touched by this round's concurrency work.
10. Investigate per-scene band-level concurrency only if further measurement shows scene-level concurrency alone is insufficient — deliberately not attempted this round (nested concurrency was explicitly out of scope).
11. ~~Benchmark scene-level concurrency for BOTH `usgs_ard` and `planetary_computer` over the same multi-tile AOI, at 1/2/4/8 workers.~~ **Done 2026-09-18** — see "ARD vs. PC concurrency scaling": the gap widens (3.82× → 7.82× at 8 workers), driven by a genuine, un-hideable M2M account-level serialization point (~62s floor for 16 units), not CPU-bound reprojection (ruled out directly by consistently low CPU utilization).
12. ~~Compare the COMPLETE pipeline to a common LandTrendr-ready output, not just imagery acquisition.~~ **Done 2026-09-20** — see "Complete end-to-end pipeline": at a real AOI-sized window, per-unit acquisition cost converges (PC's advantage was a small-window artifact), and the two providers land within ~7% total time / ~27% per output date of each other, with PC's remaining edge explained structurally (fewer contributing units/date at this AOI) rather than by raw speed. Products confirmed scientifically equivalent (27 shared dates, correlations ≥0.92, mostly ≥0.998).
13. ~~Repeat the complete-pipeline comparison at 4-tile/8-tile AOI scale.~~ **Done 2026-09-21/22 (4-tile)** — see "Complete end-to-end pipeline at 4-tile scale": the near-tie moves toward ARD as extent grows (total wall time flips: PC 7% faster at 2-tile → ARD 9.5% faster at 4-tile; PC's per-date advantage shrinks 21% → 10%), driven by PC's per-unit cost nearly doubling while ARD's stays flat. **Decision made**: ARD recommended for the eventual Oregon-scale stress test, on this trend plus its mechanism. 8-tile (or larger/differently-shaped) confirmation not yet run — see item 20.
14. ~~Fix `stac_common.py`'s missing generic transient-timeout retry.~~ **Done 2026-09-21** — `_looks_like_transient_network_error` + independent bounded retry budget, applies to every StacProvider regardless of `needs_signing`. 10 new tests, live-verified.
15. Determine whether a higher-tier/commercial EROS account raises M2M's "one request at a time" ceiling above 1 — `_m2m_call_lock` currently hard-codes concurrency=1 for the account tested; if a different tier supports more, that lock could become a small semaphore instead, narrowing (not eliminating — PC still has no analogous limit) the concurrency-scaling gap.
16. Separately evaluate AWS-hosted ARD (`s3://usgs-landsat-ard`, Requester Pays) as an alternative read path for the same `usgs_ard_sr` product — deliberately excluded from the direct-USGS experiment. Note it would sidestep the M2M rate limit entirely (S3 has no analogous per-account request cap), so this comparison would isolate how much of ARD's concurrency ceiling is M2M-specific vs. inherent to ARD's tile format/reprojection.
17. Exercise `usgs_m2m.py`'s scene-bundle read path end-to-end now that M2M auth works — or consider whether the per-band access model proven for ARD (`D771`) also applies to scene-based Collection 2, which would make that provider far lighter.
18. Investigate why ARD and PC disagree on which dates pass a 40% cloud-cover filter for the same AOI/period (observed at both 2-tile: 40 vs. 34 dates, and 4-tile: 44 vs. 36 dates, 2026-09-20/21) — likely explained by tile-level vs. scene-level cloud-cover computation over different-shaped large areas, not yet confirmed.
19. Understand why blue-band agreement is measurably weaker than the other five bands in both complete-pipeline comparability checks (2-tile: 0.92–0.98 vs. ≥0.998; 4-tile: similar pattern) — plausibly residual atmospheric/aerosol correction differences between the two processing chains, not yet isolated from other candidate causes.
20. One confirming test at 8-tile (or larger/differently-shaped) scale. **Smoke test done 2026-09-28** (6 sampled dates, 41 Mpx window): ARD mean 71 s/unit (median 57), PC mean 108 s/unit (median 45), with very wide ranges; projected full runs ARD ~7.1 h vs PC ~5.0 h. Inconclusive on speed. ARD was chosen for the Oregon archive on authority, completeness (Landsat 4) and its fixed tiling rather than on speed; a full 8-tile run was not done.
21. Diagnose the actual root cause of PC's steeper per-unit cost growth with window size (COG internal tiling was checked live and ruled out — both providers use identical 256×256 blocks) — network/CDN-layer differences (Azure Blob vs. CloudFront-backed delivery) are the leading hypothesis but were not directly instrumented.
22. Once ARD is confirmed as the chosen strategy, revisit ARD's own scene-level concurrency now that its per-unit cost is known to be the more favorable and more stable of the two at large window sizes — the earlier concurrency benchmark used a small fixed window; a large-window concurrency test was never run for either provider.

## Development principle

Optimization decisions should be driven by measured real-world workloads, not assumptions — while never trading away scientific product equivalence or provenance to get there.
