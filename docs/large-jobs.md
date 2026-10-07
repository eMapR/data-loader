# Large jobs: archives, resume, updates

A large job is configured like any other request, and it runs for days.
This page covers planning one, running it, and keeping it current.

## The archive recipe

Request whole USGS ARD tiles and keep observations exactly as USGS
distributes them:

```yaml
version: 1
provider: usgs_ard
sensors: [landsat]
aoi:
  tiles: [h003v004, h003v005]          # CONUS ARD grid ids
time:
  start_year: 1990                     # no end_year = open-ended (through today)
temporal_mode: scene
filters:
  max_cloud_percent: 100               # keep every observation
  pixel_cloud_mask: false              # keep every pixel
output:
  dir: /big/disk/my_archive
  encoding: native                     # uint16 DN, lossless, half the size of float32
  bands: [blue, green, red, nir, swir1, swir2]
  qa_band: true                        # QA_PIXEL: downstream decides about cloud/shadow/snow
workers: 4
```

The result has no masking, no resampling and no rescaling. Downstream
pipelines read `manifest.json` for band order, scale/offset and the
QA_PIXEL bit meanings, and make their own decisions. eMapR's Oregon
archive is [`examples/oregon_landsat_ard_archive.yaml`](../examples/oregon_landsat_ard_archive.yaml).

## 1. Plan

```bash
data-loader plan my_archive.yaml
```

`plan` searches the catalog (anonymous STAC; no USGS login, no downloads)
and prints, per tile, how many observations the request covers and how
many the output directory already holds. Run it first to see the scale.
It's also how you check, later, how much new imagery an update would
fetch.

### What to expect (USGS ARD, measured on eMapR's server)

| | Full observation | Edge sliver (≥80% fill) |
|---|---|---|
| Downloaded | ~185 MB | ~16 MB |
| Stored (uint16 + QA_PIXEL) | ~80 MB | ~6 MB |
| Time per observation, 4 workers | ~16–24 s effective | ~10 s effective |

- **One tile, 1990 to present:** about 3,900 observations (about 1,500 of
  them slivers). That's roughly 15–25 hours, about 0.5 TB downloaded and
  about 0.2–0.3 TB stored.
- **Oregon (23 tiles):** about 89,000 observations (about 29,000
  slivers). That's roughly 3–4 weeks, about 11 TB downloaded and about
  5–6 TB stored.

These times were measured with the pre-1.0 benchmark harness, whose
single-threaded GeoTIFF writes took ~16–24 s per full tile. DataLoader 1.0
writes the same file in ~1–2 s (multi-threaded compression), so expect
faster runs; the floor is set by USGS: the M2M one-call-at-a-time limit
(~5 s per observation), and how fast landsatlook.usgs.gov responds (it
sometimes returns 504s, which are retried). Unmasked archives compress somewhat less well than masked
ones. Check free space before starting (`df -h`), and leave room to
spare. The measurements and the Oregon estimate behind these numbers are
in [development](development/README.md).

## 2. Run, in tmux

```bash
tmux new -s archive
set -a; source .env; set +a                 # USGS_M2M_USERNAME / USGS_M2M_TOKEN
data-loader run my_archive.yaml 2>&1 | tee -a archive_run.log
# detach: Ctrl-b then d      reattach: tmux attach -t archive
```

The run discovers everything first (about 15 s per tile), then acquires.
It logs progress every minute: done, failed, seconds per observation,
hours left.

- **Use one run per USGS account.** M2M allows one call at a time per
  account; a second job on the same account slows both.
- **`workers: 4`** is a good default. M2M calls queue internally, and
  downloads and writes overlap. Going beyond about 4 gains little.
- **Memory:** about 1–2 GB per worker for full tiles, plus about 100 KB of
  catalog metadata per discovered observation (about 8–10 GB for all of
  Oregon).
- **One directory, one run:** a lock file stops a second run from
  writing into the same directory.

## 3. Watch

```bash
data-loader status /big/disk/my_archive         # counts per tile and per status; no network
data-loader status /big/disk/my_archive --json
```

`status` reads `items.jsonl` plus the run's journal, so it shows a run
in progress. `items.jsonl` and `manifest.json` themselves are rewritten
every 200 observations or 10 minutes, and at the end.

## 4. Interruptions and resume

Every finished observation is written to disk and recorded (`.state/journal.jsonl`)
the moment it completes. GeoTIFFs are written under a temporary name and
renamed into place, so a file under its final name is always complete. A
crash, `Ctrl-C`, a reboot, or a killed process loses at most the
observations that were in flight.

To resume, **run the same command again**:

```bash
data-loader run my_archive.yaml
```

- Acquired observations are skipped, and their files are checked against
  their recorded size.
- Failed observations are retried, up to `max_attempts` attempts in total
  across runs (default 3). After that they stay `failed`, with the last
  error, in `items.jsonl`. To try again later, raise `max_attempts` and
  re-run.
- Exit code `0` means complete, `3` means some observations are failed
  or pending (re-run to retry), and `2` means a config or directory
  problem.
- For an unattended loop that keeps retrying transient failures:
  `until data-loader run my_archive.yaml; do sleep 600; done`. This only
  stops on success, so use it with care if something fails permanently.

## 5. Verify

```bash
data-loader verify /big/disk/my_archive                # size + sha256 of every file
data-loader verify /big/disk/my_archive --no-checksums # sizes only (fast)
```

## 6. Keep it current

An open-ended request (no `end_year`/`end_date`) runs through the day it
is run. Re-running it later discovers and adds only what's new; nothing
already acquired is downloaded again:

```bash
data-loader plan my_archive.yaml      # how many new observations are available
data-loader run my_archive.yaml       # fetch them
```

A request with a fixed end can be extended the same way: raise `end_year`
and re-run into the same directory. Any other change (bands, encoding,
tiles, provider, masking, start date) defines a *different* dataset, and
DataLoader refuses to mix it into an existing directory. Use a new
`output.dir` for it.

USGS occasionally reprocesses ARD. A reprocessed tile gets a new item ID,
which appears as a new observation alongside the old one; both are
listed in `items.jsonl`.

## 7. Moving or sharing an archive

Everything in the dataset directory uses relative paths: files,
`metadata/` and `items.jsonl`. Copy or move the whole directory (for
example with `rsync -a`) and run `data-loader verify` at the destination.
`--output-dir` lets one config be run against different storage locations.
