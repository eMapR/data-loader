# Imagery library: production migration checklist

Moving the project-named Landsat ARD archives into the shared library
(docs/library.md, layout 2; decisions in
[library-architecture.md](library-architecture.md)). **Nothing here has
been done yet.** Each phase lists who approves it, what is run, how it is
verified, and how it is undone. Commands assume
`LIB=/vol/v1/Landsat-ARD` and DataLoader/imagery-library 1.2 (branch
`imagery-library`) installed in one maintainers' venv.

## What is being migrated

| Archive | Tiles | Observations | Size | Failed rows today |
|---|---|---|---|---|
| `/vol/v1/data/oregon_landsat_ard` | 23 CONUS | 89,027 | 7.5 TiB | 1 (superseded version) |
| `/vol/v1/data/usfs_r6_columbia_mtns_wa_landsat_ard` | 4 CONUS | 16,937 | 1.5 TiB | 2 (superseded versions) |
| `/vol/v1/data/nccn_olym_lpa01_landsat_ard` | 2 CONUS | 7,543 | 0.5 TiB | 0 |
| `/vol/v1/data/glkn_slbe_landtrendr_landsat_ard` | 1 CONUS | 3,613 | 0.3 TiB | 0 |
| **Total** | **30** | **117,120** | **9.8 TiB (10.7 TB)**, 234,240 files | 3 |

The Northern SE Alaska archive (6 Alaska tiles, 17,646 observations) was
deleted on 2026-10-28 to make room for Tanana. Its tiles can be acquired
into the library again if needed. No tile is in two archives. All
were checked read-only against the product spec on 2026-10-26. Every
source-metadata record belongs to exactly one observation (checked
2026-10-29), so every file is sealed after retirement.

## Approvals needed before touching production data

| # | Decision | Who | Blocks |
|---|---|---|---|
| A1 | **Library location.** DECIDED 2026-10-30: `/vol/v1/Landsat-ARD`, hard links (same XFS volume as `/vol/v1/data`; < 1 GB new). Later move to shared storage: library-architecture.md §6. | done | - |
| A2 | **Maintainers' group** (e.g. `imglib-maint`, 2-3 people) created by the admins; researchers' read access (`other`, or a readers group/ACL). Required: setgid cannot be set without it. | storage admins + PI | phase 3 |
| A3 | **Backup minimum in place** (docs/library-backup-policy.md): daily metadata backup to separate storage, restore tested once. | storage admins + PI | phase 2 |
| A4 | **Code release.** Review and merge `imagery-library` (DataLoader 1.2: superseded status, account lock; imagery-library) to main, tag it, install one maintainers' venv. Retire the older venvs (`/vol/v1/data-loader`, `-stabilize`, `-ard`, `-dev`) for USGS runs: they predate the account lock. Add `DATALOADER_LOCK_DIR=$LIB/admin/locks` to the account's env file. | maintainers + PI | phase 1 |
| A5 | **Migration go**, including an acquisition freeze for the window. Hard links and sealing DECIDED 2026-10-30: the permission policy skips files that still have a project-archive name, so the project archives' files keep their mode, group and ACLs; sealing happens after retirement (phase 5). | PI | phases 2-3 |
| A6 | **First update run** (USGS account time, ~hours): downloads observations published since the last runs and turns the 3 failed rows into `superseded`. | PI | phase 4 |
| A7 | **Retirement of the project-named directories**, at least 30 days after the library is fully verified and catalogued (DECIDED 2026-10-30), after a successful backup and verify. Separate approval at the time. | PI | phase 5 |

## Storage requirements (summary)

- Hard links on `/vol/v1`: < 1 GB new (manifests and items per tile).
- Copy to another volume (the later move to shared storage): 10.7 TB +
  10% headroom now; ~15 TB with Tanana; +~0.9 TB/yr.
- Filesystem: atomic rename, fsync, `flock` across clients (NFSv4 with
  server-side locks), hard links within the volume, symlinks.
- Backup: ~5 GB metadata daily (must); retired imagery (should; empty
  today); imagery snapshot or replica (preferred).
- Details and measurements: docs/library-storage-requirements.md.

## Phase 0: preparation (no production change)

- [ ] A1-A4 approved; `$LIB` chosen.
- [ ] `imagery-library init $LIB --name Landsat-ARD --description ... --maintainer ... --readme library/landsat_ard/README.md [--group <maintainers>]`
      (README maintainer/contact TODOs filled in first: D6)
- [ ] `imagery-library add-product $LIB library/landsat_ard/collection_2/PRODUCT.yaml`
- [ ] `imagery-library permissions $LIB` reports 0 differences and no warnings.
- [ ] Account env file has `DATALOADER_LOCK_DIR=$LIB/admin/locks`; old venvs not used.

## Phase 1: pre-flight (read-only on production)

- [ ] **Freeze:** no DataLoader run on any of the four archives (`tmux ls`;
      `pgrep -af "data-loader run"` empty). The account lock now also
      refuses overlapping runs.
- [ ] **Fingerprint the sources** (kept with the migration record):
      ```
      for a in <four archives>; do sha256sum $a/manifest.json $a/items.jsonl $a/request.yaml; done > sources.sha256
      for a in <four archives>; do find $a -type f -printf '%i %n %m %g %s %p\n'; done > sources.files   # inode, links, mode, group, size
      ```
- [ ] **Back up the sources' metadata** (manifest, items, request, `metadata/`;
      ~5 GB) to the backup location (A3).
- [ ] **Size check of every source file:** `data-loader verify --no-checksums <archive>` (minutes) for each.
- [ ] **Dry run:** `imagery-library import $LIB <archive> --mode hardlink|copy --dry-run`
      for each archive: 30 tiles in all `import`, 0 problems, "source manifest/items unchanged: True".

Rollback: nothing to undo.

## Phase 2: import (A5)

Smallest first, so problems show early: Sleeping Bear, Olympic, Columbia,
Oregon.

- [ ] `imagery-library import $LIB <archive> --mode hardlink` (same volume; minutes)
      or `--mode copy` (other volume; ~3 days for all at 1 Gb/s), one archive at a time.
- [ ] After each: exit 0; per tile `imported`; `sha256sum -c sources.sha256` OK.
- [ ] Hard links: `find $LIB/collection_2 -name '*.tif' -links 1 | wc -l` is 0
      (every library file is the same inode as a source file).
- [ ] Source files unchanged: the `sources.files` listing (inode, links, mode, group, size)
      differs only in the link count (1 -> 2).

Rollback: delete `$LIB/collection_2/<grid>/<tile>` (with hard
links the source files are untouched: same inodes, one name fewer).

## Phase 3: verify, check, catalog

- [ ] **Full checksum verify of the library:** `imagery-library verify $LIB`
      (~15 h locally for 10.7 TB). Every tile OK.
      This is also the library's first verification record.
- [ ] **Permissions check:** `imagery-library permissions $LIB` reports every
      imported file as `shared` (left as is: the project archives' files keep
      their modes) and nothing else differing. Sealing waits for phase 5.
- [ ] **Catalog:** `imagery-library catalog $LIB` reports 30 tiles,
      117,120 observations, 10.7 TB, all `verified`; 27 complete,
      3 incomplete (the 3 superseded-version rows, until phase 4).
- [ ] **Counts per tile** (`imagery-library tiles $LIB`) equal the source
      manifests (`data-loader status <archive>`).
- [ ] **Queries:** each study area's AOI (`aois/*.geojson`) and an Oregon
      AOI return 100% coverage and the expected observation counts
      (Olympic: 12,199 from three tiles, one of them from the Oregon archive).
- [ ] **Backup** of the library's metadata tier
      (`imagery-library backup-list $LIB --tier metadata --out meta.list`).

Rollback: as phase 2 (nothing was sealed).

## Phase 4: first update (A6)

- [ ] `imagery-library acquire $LIB --aoi <each study area> --include-held`
      (or `--tile` for all 30), in tmux; one run at a time is enforced.
- [ ] Expect: new observations since the October runs; the 3 failed rows
      become `superseded` with their failure kept under `history`; every
      tile `complete`.
- [ ] `imagery-library verify $LIB` (new files), `imagery-library permissions $LIB --fix`,
      `imagery-library catalog $LIB`: all tiles `complete` and `verified`.
- [ ] New study areas (Tanana: 12 Alaska tiles, ~4.1 TB) are acquired straight into the library:
      `imagery-library acquire $LIB --aoi aois/usfs_r10_tanana_river.geojson` (staged, verified, moved in).
- [ ] Researchers told where the library is and how to query it (README.md at the root, docs/library.md).

Rollback: the library keeps working on the pre-update state if a run
fails; the project-named archives are unchanged (they never receive the
new observations).

## Phase 5: retirement (A7, at least 30 days later)

- [ ] A metadata backup and a full verify, both since the last update.
- [ ] Project configs and docs point at the library (`aois/`, DATALOADER docs).
- [ ] Hard links: remove the four project-named directory names (frees no
      space; the library holds the same inodes). Keep their
      `manifest.json`/`items.jsonl`/`request.yaml` in the backup as lineage
      (each tile's `LINEAGE.json` already records them).
- [ ] **Seal:** `imagery-library permissions $LIB --fix` (now that each file has one name),
      then `imagery-library permissions $LIB` reports 0 differences and 0 shared.

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
