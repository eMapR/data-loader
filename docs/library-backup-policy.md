# Imagery library: backup policy

What must be protected, what that costs, and what it costs not to. The
storage facts behind it are in
[library-storage-requirements.md](library-storage-requirements.md).

## What is at risk

| Tier | What | Size now | If lost |
|---|---|---|---|
| 1. Metadata | `LIBRARY.yaml`, product specs and published-tile lists, every tile's `manifest.json` / `items.jsonl` / `request.yaml` / `LINEAGE.json`, USGS source records (`metadata/`), `admin/` (acquisition, import and verification logs, verify records), the current catalog | **~5.0 GB** (4.75 GB source records, 0.28 GB manifests + items) | The imagery can no longer be verified (no checksums), attributed (no provenance, processing versions, software) or resumed. In practice every tile would have to be acquired again. |
| 2. Irreplaceable imagery | acquired observations whose processing version USGS has since replaced (`supersededBy` in items.jsonl and `superseded_by` in the catalog) | **0 files today**; grows as USGS reprocesses (DataLoader marks them at every update run) | Gone for good: USGS stops distributing a retired version (seen 3 times so far, each time before we had downloaded it). |
| 3. Other imagery | every other acquired GeoTIFF | **~12.5 TB** (16.6 TB with Tanana; +~0.9 TB/yr) | Re-downloadable while USGS distributes Collection 2 ARD, at the cost below. |

`imagery-library backup-list ROOT --tier metadata|irreplaceable|imagery
--out LIST` writes each tier as a list of paths for `rsync -a
--files-from=LIST ROOT/ DEST/` (or any backup tool).

### The cost of re-acquiring tier 3

Measured on this server (October 2026 runs, DataLoader 1.1):

| | |
|---|---|
| Throughput, one USGS account | ~1.4-1.5 TB stored per day (4.26 TB in 2.9 days for four regions) |
| Today's library (12.5 TB) | **~8.5 days** of exclusive use of the account |
| With Tanana (16.6 TB) | **~11.5 days** |
| Download volume | ~1.4x the stored size (~17 TB for today's library) |
| Side effects | no new acquisitions or updates on that account meanwhile; a maintainer watching multi-day runs (outages, circuit-breaker pauses: the 2026-10-21 USGS outage alone cost 3.5 h) |
| Not recoverable at all | tier 2; and **all** of tier 3 once USGS stops distributing Collection 2 ARD (it retired Collection 1 in 2022). From then on the library is the only copy. |

## Policy

### Minimum (required before production migration)

- **Tier 1 daily**, and after every acquisition/import, to storage
  separate from the library volume; keep 30 daily versions (they are
  small: ~5 GB each, mostly unchanged between days).
- **Tier 2 after every update run** that marked new retired versions (the
  list is empty until USGS reprocesses something we hold); keep
  indefinitely.
- **Verification:** a full checksum verify (`imagery-library verify ROOT`)
  monthly and after any storage incident (~17 h for 12.5 TB at the local
  ~200 MB/s; about twice that over 1 Gb/s NFS); a failed verify is
  repaired by re-running the tile's acquisition (missing or damaged files
  are downloaded again).
- **Restore test** twice a year: restore tier 1 of one tile to scratch
  space, `data-loader verify` it against the live imagery.
- Accept that losing tier 3 means **~1.5 weeks** of re-acquisition, and
  permanent loss if USGS no longer distributes Collection 2 by then.

### Preferred

Everything in the minimum, plus:

- **Tier 3 replicated or snapshotted** on separate storage, weekly,
  incremental (`rsync -aH`; only new observations change, ~20 GB per week),
  so a lost volume costs a restore, not 1.5 weeks of USGS time.
- **A full copy before USGS ends Collection 2 ARD distribution**, whatever
  else is in place: from then on nothing in the library can be
  re-acquired.
- Monthly verify of the replica as well.

### Practical notes

- **Hard links.** During a hard-link migration the old project-named
  paths and the library share files; a backup must preserve hard links
  (`rsync -H`) or it stores everything twice (~25 TB).
- **Exclude** each tile's `.state/` (a running acquisition's journal and
  lock) and old catalog builds (`catalog/2*/` other than the one
  `catalog/current` points to); `backup-list` already does.
- **Consistency.** A backup taken while an acquisition writes a tile is
  still usable: DataLoader writes each file under a temp name and renames
  it, and items.jsonl is replaced atomically, so a backup holds either the
  old or the new version of each file. Prefer backups between runs.
