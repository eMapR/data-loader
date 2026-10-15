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

Measured on the Oregon archive run (DataLoader 1.0, 4 workers, one M2M
account, October 2026; about 76,000 observations in):

| | Full observation | Edge sliver (≥80% fill) |
|---|---|---|
| Downloaded | ~185 MB | ~16 MB |
| Stored (uint16 + QA_PIXEL, unmasked) | ~129 MB | ~15 MB |
| Time per observation, 4 workers | ~8.8 s effective, all observations averaged | |

- **Oregon (23 tiles, 1990 to present):** about 89,000 observations (about
  29,000 slivers), about **9–10 days** and about **8.3 TB** stored
  (7.5 TiB). Throughput varied from about 8,300 to 10,500 observations per
  day with how often landsatlook.usgs.gov returned 504s.
- **One tile, 1990 to present:** about 3,900 observations (about 1,500 of
  them slivers): roughly 9–10 hours and about 0.35 TB stored.

The floor is set by USGS: the M2M one-call-at-a-time limit (~5 s per
observation), and how fast landsatlook.usgs.gov responds (it sometimes
returns 504s, which are retried, first per file, then in retry passes
before the run moves on to the next tile). Unmasked archives compress
less well than masked ones. Check free space before starting (`df -h`),
and leave room to spare. The measurements behind these numbers are in
[development](development/README.md).

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
- **Memory:** the working set is about 1 GB per worker for full tiles plus
  about 25 KB per discovered observation (about 2–3 GB for all of Oregon),
  but memory grows slowly over a long run: the Oregon run (4 workers)
  peaked at **62 GB** after a week (34 GB resident at the time). The cause
  is not pinned down yet (see the development report); until it is, give
  a multi-day archive run a machine or job allocation with 64 GB or more,
  and watch the `rss` figure in the progress log. A run can be stopped
  (Ctrl-C) and resumed at any time, which resets memory.
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
  (default 3). The run works one tile at a time, and a tile's failures
  get retry passes (60 s pause first) before the next tile starts, so
  each tile is finished, gaps included, as the run goes. Attempts
  carry over across runs; after the last one an observation stays
  `failed`, with the last error, in `items.jsonl`. To try again later,
  raise `max_attempts` and re-run.
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
