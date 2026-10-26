# Imagery library: storage, permissions and backup requirements

For discussion with the storage administrators. It describes what the
shared imagery library ([library.md](library.md)) needs from the storage it
lives on, what we measured on the candidate volumes (2026-10-26), and the
questions we need answered before anything is migrated. Nothing has been
moved; production archives are untouched.

## What will be stored

| | Now | Within a year (planned) |
|---|---|---|
| Product | USGS Landsat C2 ARD surface reflectance, native uint16 + QA_PIXEL | same; Sentinel-2 L2A later |
| Tiles | 36 (30 CONUS, 6 Alaska) | + 12 Alaska (Tanana River basin) = 48 |
| Observations | 134,766 | ~180,000 |
| Size | 12.46 TB (11.4 TiB) | ~16.6 TB + ~0.9 TB/yr new observations |
| Files | 269,532 (one GeoTIFF + one source-metadata JSON per observation) | ~360,000 |
| File sizes | GeoTIFF mean 92 MB (up to ~110 MB); JSON a few KB | same |
| Directory fan-out | one directory per tile per year (~37 per tile), ≤ ~600 files each | same |

Growth: USGS keeps publishing Landsat 8/9 observations, measured at a median
of 18 GB per tile per year (2025: 9-21 GB per tile, 0.65 TB for 36 tiles).
Each new study area adds roughly 0.2-0.43 TB per new ARD tile.

Write pattern: one maintainer process at a time (USGS allows one download
session per account), ~20-25 MB/s sustained for days, each file written to
a temp name, fsynced, then renamed. Read pattern: researchers read whole
GeoTIFFs, often a whole tile history (~350 GB) for time-series work.

## What the library needs from storage

1. **A directory we own as a group**, e.g. `<volume>/imagery_library/`,
   owned by a maintainers group, with researchers able to read but not write.
2. **Read-only for researchers, writable for maintainers.** Proposed:
   directories `2775` (setgid) and files `0444` after acquisition, group =
   maintainers (2-3 people); researchers read via `other` or a readers
   group. Today on `/vol/v1/data` every file is `664` and every directory is
   group-writable with a default ACL, so all 13 members of `trenders` can
   modify or delete production imagery.
3. **Quota** of at least 20 TB to start, with room to grow by ~1 TB/yr plus
   new study areas (each ~0.3-4 TB).
4. **Backup or snapshots.** At minimum the metadata (manifests, items,
   source metadata JSON, catalog, admin logs: under 10 GB total) must be
   backed up. Ideally imagery too: it can mostly be downloaded again (days of
   USGS time per region), but USGS retires old processing versions when it
   reprocesses (we have already hit this twice), so some acquired files can
   no longer be re-downloaded and exist only here.
5. **POSIX semantics DataLoader relies on:** atomic `rename`, `fsync`,
   `flock` (one writer per tile), hard links within the volume (for a
   zero-copy migration), symlinks (the catalog's `current` pointer).
6. **Throughput** good enough for time-series reads from the machines
   researchers compute on; at 1 Gb/s a single tile history takes ~1 h to read.

## Candidate volumes (measured 2026-10-26 from this server)

| | `/vol/v1` (current) | `/home/server/datasets` | `/home/server/project` |
|---|---|---|---|
| Type | local XFS on a PERC H810 RAID, `noquota` | NFS 4.2 from `datasets:/data/datasets` | NFS 4.2 from `homes:/export/project` |
| Size / free | 37 TB / 3.6 TiB free (91% used) | 499 TB / 467 TB free (7%) | 649 TB / 47 TB free (93%) |
| Our access | owner `clarype`, group `trenders`, setgid + default ACL | **none**: `root:root 755`, a test write was denied | not checked |
| Quota | none (XFS mounted `noquota`) | unknown, nothing reported to us | unknown |
| Backup / snapshots | unknown, none visible | unknown, no `.snapshot` visible | unknown |
| Mount options | local | `soft,timeo=600,retrans=2`, `local_lock=none` | `soft` |
| Read throughput | ~200 MB/s measured during verify (higher when cached) | 103-114 MB/s (1 Gb/s `eno1`, routed via the campus gateway) | not measured |
| Fits library + Tanana (~17 TB)? | **no** (3.9 TB free) | yes | yes, but the volume is 93% full |

Notes: `/home/server/datasets` holds `ERA5/` (33 TB) and `testing/` (16 x 50 GB
files dated 22 June, likely a throughput test, plus a world-writable
`testing/all/`). Being able to write somewhere is not the same as being
allowed to; we have not written anything there.

A `soft` NFS mount can return I/O errors under load instead of retrying.
DataLoader fsyncs each file before renaming it into place, so a failed write
fails that observation (and is retried) rather than leaving a silently bad
file, and `verify` re-checks every sha256. For a write target a `hard` mount
may still be preferable.

## Questions for the storage administrators

1. May eMapR create a shared Landsat (later Sentinel-2) library under
   `/home/server/datasets`, ~17 TB in the first year and growing?
2. What quota applies, and how do we request more?
3. Is the volume backed up or snapshotted? If not, can a small metadata
   directory (<10 GB) be included in a backed-up location?
4. What disks/filesystem are behind `datasets:/data/datasets`, and does it
   support hard links, `flock` and atomic rename over NFS 4.2 as we use them?
5. Can the library directory be owned by a maintainers group (2-3 people)
   with read-only access for other researchers (a readers group, or
   `other`)? Can OSU researchers outside our groups get read access?
6. Is the `soft` mount intentional for a volume we would write to for days?
7. Do the compute nodes researchers use have a faster path to it than this
   server's 1 Gb/s link?
8. Was `testing/` a throughput benchmark, and are its results available?
9. If `/vol/v1` stays in use for now: is it backed up, and can we tighten
   its permissions (see requirement 2) without affecting other users?

## What we will do once these are answered

The migration is phased, each phase gated on verification, and the
existing archives stay untouched (they are the rollback) until a separate
sign-off: build the library tree beside them with hard links (same volume,
no extra space) or copy and re-verify every sha256 (another volume, ~3 days
for 12.5 TB at 1 Gb/s), rebuild the catalog, and only then retire the
project-named directories.
