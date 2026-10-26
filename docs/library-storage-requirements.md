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
| File sizes | GeoTIFF mean 92 MB (up to ~110 MB); JSON ~35 KB | same |
| Directory fan-out | one directory per tile per year (~37 per tile), ≤ ~600 files each | same |

Growth: USGS keeps publishing Landsat 8/9 observations, measured at a median
of 18 GB per tile per year (2025: 9-21 GB per tile, 0.65 TB for 36 tiles).
Each new study area adds roughly 0.2-0.43 TB per new ARD tile.

Write pattern: one maintainer process at a time (USGS allows one download
session per account), ~20-25 MB/s sustained for days, each file written to
a temp name, fsynced, then renamed. Read pattern: researchers read whole
GeoTIFFs, often a whole tile history (~350 GB) for time-series work.

## Permissions

### Roles

| Role | Who | Needs |
|---|---|---|
| Maintainers | 2-3 named people (eMapR) | create/update tile datasets, run verify, rebuild the catalog, write `admin/` |
| Researchers | eMapR and other OSU researchers with an account on the compute hosts | read imagery, manifests and the catalog; nothing else |
| Acquisition credentials | USGS M2M token(s) | held only in maintainers' environment (`~/.config/data-loader/*.env`, mode 600); never stored in the library, its logs or its configs |

### Proposed layout of permissions

| Path | Owner : group | Mode | Why |
|---|---|---|---|
| `<root>/` and every directory below it | maintainer (or a service account) : `imglib-maint` | `2775` (setgid) | maintainers create/rename files (DataLoader writes a temp file and renames it); others can list and read but not create, rename or delete |
| GeoTIFFs and `metadata/*.json` once acquired | same | `0444` | nobody overwrites imagery in place; a maintainer can still replace a damaged file (rename needs only the directory's write bit) |
| `manifest.json`, `items.jsonl`, `request.yaml`, `LINEAGE.json`, `PRODUCT.yaml` | same | `0644` | rewritten atomically by maintainers' tools |
| `catalog/` | same | `2775` / `0644` | researchers read `catalog/current/*`; only maintainers rebuild |
| `admin/` (logs, locks, verify records, generated configs) | same | `2770` / `0660` | maintainers only; locks must be writable by every maintainer |
| `.state/` in each tile (DataLoader lock + journal) | same | `2770` | written by a running acquisition only |

Researchers read via `other` (`r-x` / `r--`) or, if access must be limited
to OSU accounts in particular groups, a readers group through an ACL; the
library needs no per-project access control because all imagery is the same
public USGS product.

Today, on `/vol/v1/data`, every file is `664` and every directory is
group-writable with a default ACL (`group::rwx`), so all 13 members of
`trenders` can modify or delete production imagery. That does not meet the
read-only requirement; we'd like to tighten it (or move to a volume where we
can) as part of the migration.

### How it is enforced

- `imagery-library` applies this policy (LIBRARY.yaml `permissions:`) to
  everything it writes: after each import, acquisition, verify and catalog
  build. `imagery-library permissions ROOT` checks the whole library (anyone
  can run it); `--fix` applies it (maintainers). Run `--fix` after any
  direct `data-loader run`, whose files arrive with the maintainer's umask.
- **The maintainers' group must be set** (`permissions.group`): Linux
  silently drops the setgid bit when the person running `chmod` is not in
  the file's group, which happens on `/vol/v1/data` (files inherit group
  `trenders` there). The tool reports any bit it could not set.
- Directories on `/vol/v1/data` carry a default POSIX ACL
  (`default:group::rwx`); the mode the tool sets becomes the ACL mask, so
  the effective permissions follow the policy, but new files arrive
  group-writable until the next pass.
- Changing the mode of a hard-linked file changes it for every name of the
  file: sealing a hard-link-migrated library makes the old project-named
  archive read-only too (intended, but part of the migration approval).

### One acquisition per USGS account

`data-loader run` itself holds an account lock for the whole run (exit 6,
or `--wait-for-account` to queue). Every maintainer sets
`DATALOADER_LOCK_DIR=<library>/admin/locks` in the env file that holds the
account's credentials, so any run on that account, through
`imagery-library acquire` or directly, uses the same lock, across machines
(flock on NFSv4 is held by the server).

## Backup and recovery

The policy (tiers, minimum and preferred, the cost of re-acquiring) is in
[library-backup-policy.md](library-backup-policy.md). In short: ~5 GB of
metadata **must** be backed up daily; imagery USGS has since retired
(listed by `imagery-library backup-list --tier irreplaceable`, empty
today) **should** be; the remaining ~12.5 TB can be re-acquired in about
8.5 days of exclusive USGS account time, or not at all once USGS stops
distributing Collection 2. What we ask of storage: a backed-up location
for tier 1, and ideally snapshots or a replica for the imagery.

## Other storage requirements

1. **A directory we own as a group**, e.g. `<volume>/imagery_library/`.
2. **Quota** of at least 20 TB to start, with room to grow by ~1 TB/yr plus
   new study areas (each ~0.3-4 TB).
3. **POSIX semantics the library relies on:** atomic `rename` (files and the
   catalog switch), `fsync`, `flock` (one writer per tile; one acquisition
   per USGS account across machines: NFSv4 must pass locks to the server,
   i.e. not `local_lock=all`), hard links within the volume (zero-copy
   migration), symlinks (`catalog/current`).
4. **Throughput** good enough for time-series reads from the machines
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
3. Is the volume backed up or snapshotted? If not, can the ~5 GB of
   metadata (tier 1) be included in a backed-up location, and is there an
   option for the imagery (tiers 2-3)?
4. What disks/filesystem are behind `datasets:/data/datasets`, and do hard
   links, `flock` across clients and atomic rename work as described above
   over NFS 4.2?
5. Can the library directory be owned by a maintainers group (2-3 people,
   or a service account) with read-only access for other researchers? Can
   OSU researchers outside our groups get read access?
6. Is the `soft` mount intentional for a volume we would write to for days?
7. Do the compute nodes researchers use have a faster path to it than this
   server's 1 Gb/s link?
8. Was `testing/` a throughput benchmark, and are its results available?
9. If `/vol/v1` stays in use for now: is it backed up, and can we tighten
   its permissions as above without affecting other users?

## What we will do once these are answered

The migration is phased, each phase gated on verification, and the
existing archives stay untouched (they are the rollback) until a separate
sign-off: build the library tree beside them with hard links (same volume,
no extra space) or copy and re-verify every sha256 (another volume, ~3 days
for 12.5 TB at 1 Gb/s), rebuild the catalog, and only then retire the
project-named directories.
