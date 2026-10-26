# Imagery library: production migration checklist

Moving the five project-named Landsat ARD archives into the shared library
(docs/library.md). **Nothing here has been done yet.** Each phase lists who
approves it, what is run, how it is verified, and how it is undone.
Commands assume the library root is `$LIB` and DataLoader/imagery-library
1.2 (branch `imagery-library`) is installed in one maintainers' venv.

## What is being migrated

| Archive | Tiles | Observations | Size | Failed rows today |
|---|---|---|---|---|
| `/vol/v1/data/oregon_landsat_ard` | 23 CONUS | 89,027 | 7.5 TiB | 1 (superseded version) |
| `/vol/v1/data/usfs_r10_northern_se_alaska_landsat_ard` | 6 Alaska | 17,646 | 1.6 TiB | 0 |
| `/vol/v1/data/usfs_r6_columbia_mtns_wa_landsat_ard` | 4 CONUS | 16,937 | 1.5 TiB | 2 (superseded versions) |
| `/vol/v1/data/nccn_olym_lpa01_landsat_ard` | 2 CONUS | 7,543 | 0.5 TiB | 0 |
| `/vol/v1/data/glkn_slbe_landtrendr_landsat_ard` | 1 CONUS | 3,613 | 0.3 TiB | 0 |
| **Total** | **36** | **134,766** | **11.4 TiB (12.46 TB)**, 269,532 files | 3 |

No tile is in two archives. All 36 match the `landsat_ard_sr/c2` product
spec (checked read-only 2026-10-26).

## Approvals needed before touching production data

| # | Decision | Who | Blocks |
|---|---|---|---|
| A1 | **Library location and quota.** `/vol/v1` (hard links: no extra space, but 3.9 TB free leaves no room for Tanana) or `/home/server/datasets` (copy: ~3 days at 1 Gb/s, ~13 TB of its space, read throughput ~110 MB/s from this server). | storage admins + PI | everything |
| A2 | **Maintainers' group** (e.g. `imglib-maint`, 2-3 people) created by the admins; researchers' read access (`other`, or a readers group/ACL). Required: setgid cannot be set without it. | storage admins + PI | phase 3 |
| A3 | **Backup minimum in place** (docs/library-backup-policy.md): daily metadata backup to separate storage, restore tested once. | storage admins + PI | phase 2 |
| A4 | **Code release.** Review and merge `imagery-library` (DataLoader 1.2: superseded status, account lock; imagery-library) to main, tag it, install one maintainers' venv. Retire the older venvs (`/vol/v1/data-loader`, `-stabilize`, `-ard`, `-dev`) for USGS runs: they predate the account lock. Add `DATALOADER_LOCK_DIR=$LIB/admin/locks` to the account's env file. | maintainers + PI | phase 1 |
| A5 | **Migration go**, including: hard links vs copy (follows A1); an acquisition freeze for the window; and **sealing**: applying the permission policy changes the mode and group of every imagery file, which with hard links also changes the project-named archives' files (to read-only, group = maintainers). Undoing the group change needs an admin (we are not in `trenders`). | PI | phases 2-3 |
| A6 | **First update run** (USGS account time, ~hours): downloads observations published since the last runs and turns the 3 failed rows into `superseded`. | PI | phase 4 |
| A7 | **Retirement of the project-named directories**, at least 30 days after phase 4 and after a successful backup and verify. Separate decision. | PI | phase 5 |

## Storage requirements (summary)

- Hard links on `/vol/v1`: < 1 GB new (manifests and items per tile).
- Copy to another volume: 12.5 TB + 10% headroom now; ~17 TB with Tanana;
  +~0.9 TB/yr.
- Filesystem: atomic rename, fsync, `flock` across clients (NFSv4 with
  server-side locks), hard links within the volume, symlinks.
- Backup: ~5 GB metadata daily (must); retired imagery (should; empty
  today); imagery snapshot or replica (preferred).
- Details and measurements: docs/library-storage-requirements.md.

## Phase 0: preparation (no production change)

- [ ] A1-A4 approved; `$LIB` chosen.
- [ ] `imagery-library init $LIB --name ... --description ... --maintainer ... --group <maintainers>`
- [ ] `imagery-library add-product $LIB library/products/landsat_ard_sr/c2/PRODUCT.yaml`
- [ ] `imagery-library permissions $LIB` reports 0 differences and no warnings.
- [ ] Account env file has `DATALOADER_LOCK_DIR=$LIB/admin/locks`; old venvs not used.

## Phase 1: pre-flight (read-only on production)

- [ ] **Freeze:** no DataLoader run on any of the five archives (`tmux ls`;
      `pgrep -af "data-loader run"` empty). The account lock now also
      refuses overlapping runs.
- [ ] **Fingerprint the sources** (kept with the migration record):
      ```
      for a in <five archives>; do sha256sum $a/manifest.json $a/items.jsonl $a/request.yaml; done > sources.sha256
      for a in <five archives>; do find $a -type f -printf '%i %n %m %g %s %p\n'; done > sources.files   # inode, links, mode, group, size
      ```
- [ ] **Back up the sources' metadata** (manifest, items, request, `metadata/`;
      ~5 GB) to the backup location (A3).
- [ ] **Size check of every source file:** `data-loader verify --no-checksums <archive>` (minutes) for each.
- [ ] **Dry run:** `imagery-library import $LIB <archive> --mode hardlink|copy --dry-run`
      for each archive: 36 tiles `import`, 0 problems, "source manifest/items unchanged: True".

Rollback: nothing to undo.

## Phase 2: import (A5)

Smallest first, so problems show early: Sleeping Bear, Olympic, SE Alaska,
Columbia, Oregon.

- [ ] `imagery-library import $LIB <archive> --mode hardlink` (same volume; minutes)
      or `--mode copy` (other volume; ~3 days for all at 1 Gb/s), one archive at a time.
- [ ] After each: exit 0; per tile `imported`; `sha256sum -c sources.sha256` OK.
- [ ] Hard links: `find $LIB/products -name '*.tif' -links 1 | wc -l` is 0
      (every library file is the same inode as a source file).

Rollback: delete `$LIB/products/landsat_ard_sr/c2/<grid>/<tile>` (with hard
links the source files are untouched: same inodes, one name fewer).

## Phase 3: verify, seal, catalog

- [ ] **Full checksum verify of the library:** `imagery-library verify $LIB`
      (~17 h locally for 12.5 TB; ~2x over 1 Gb/s NFS). Every tile OK.
      This is also the library's first verification record.
- [ ] **Seal:** `imagery-library permissions $LIB --fix`, then
      `imagery-library permissions $LIB` reports 0 differences.
      (With hard links this also makes the source files read-only; see A5.)
- [ ] **Catalog:** `imagery-library catalog $LIB` reports 36 tiles,
      134,766 observations, 12.46 TB, all `verified`; 33 complete,
      3 incomplete (the 3 superseded-version rows, until phase 4).
- [ ] **Counts per tile** (`imagery-library tiles $LIB`) equal the source
      manifests (`data-loader status <archive>`).
- [ ] **Queries:** each study area's AOI (`aois/*.geojson`) and an Oregon
      AOI return 100% coverage and the expected observation counts
      (Olympic: 12,199 from three tiles, one of them from the Oregon archive).
- [ ] **Backup** of the library's metadata tier
      (`imagery-library backup-list $LIB --tier metadata --out meta.list`).

Rollback: as phase 2; to undo sealing on shared inodes, restore modes from
`sources.files` (`chmod` as owner) and groups (`chgrp`, needs an admin for
`trenders`).

## Phase 4: first update (A6)

- [ ] `imagery-library acquire $LIB --aoi <each study area> --include-held`
      (or `--tile` for all 36), in tmux; one run at a time is enforced.
- [ ] Expect: new observations since the October runs; the 3 failed rows
      become `superseded` with their failure kept under `history`; every
      tile `complete`.
- [ ] `imagery-library verify $LIB` (new files), `imagery-library permissions $LIB --fix`,
      `imagery-library catalog $LIB`: all tiles `complete` and `verified`.
- [ ] Researchers told where the library is and how to query it (docs/library.md).

Rollback: the library keeps working on the pre-update state if a run
fails; the project-named archives are unchanged (they never receive the
new observations).

## Phase 5: retirement (A7, at least 30 days later)

- [ ] A metadata backup and a full verify, both since the last update.
- [ ] Project configs and docs point at the library (`aois/`, DATALOADER docs).
- [ ] Hard links: remove the five project-named directory names (frees no
      space; the library holds the same inodes). Keep their
      `manifest.json`/`items.jsonl`/`request.yaml` in the backup as lineage
      (each tile's `LINEAGE.json` already records them).
- [ ] Copy: delete the originals after one more verify of the copy; frees ~12.5 TB on `/vol/v1`.

Rollback after retirement: re-create a project view from the library
(each tile's LINEAGE.json names its source archive); the original
directory layout itself is not recreated.

## Verification procedures (reference)

| Check | Command | Proves | Time (12.5 TB) |
|---|---|---|---|
| Sizes | `data-loader verify --no-checksums DIR` | every listed file exists at its recorded size | minutes |
| Checksums | `imagery-library verify $LIB` (DataLoader verify per tile) | every file's sha256 equals the one recorded when it was written | ~17 h local, ~35 h at 1 Gb/s |
| Source untouched | `sha256sum -c sources.sha256`; importer's own before/after fingerprint | migration did not modify the source metadata | seconds |
| Same bytes, no copy | `find ... -links 1` / inode list | hard-link import reused source files | minutes |
| Counts | `imagery-library tiles` vs `data-loader status` | nothing lost or duplicated per tile | seconds |
| Permissions | `imagery-library permissions $LIB` | read-only imagery, maintainers-only admin | minutes |
| Discovery | `imagery-library query` on the study-area AOIs | researchers find the same imagery without project names | seconds |
| Resume | `data-loader plan` on a generated tile config | DataLoader treats the tile as its own dataset (0 to do) | seconds per tile |
