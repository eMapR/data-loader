# Imagery library: architecture decisions

The design of the shared imagery archive (`imagery_library/`, layout 2)
and why it is that way. Keep this file with the code: it is meant to
survive moves between storage systems. User-facing docs:
[library.md](library.md); the dataset's own README:
[`library/landsat_ard/README.md`](../library/landsat_ard/README.md);
migration: [library-migration-checklist.md](library-migration-checklist.md).

Decided 2026-10-29/30 (eMapR). Status: implemented on branch
`imagery-library`; production not migrated yet.

## Context

DataLoader first wrote one archive per project (Oregon, Columbia, Olympic,
Sleeping Bear, ...), each holding several ARD tiles. That ties imagery to
whoever asked for it first. A second project needing the same tile would
download it again, and nobody outside eMapR can find the data. The goal is
a universal research dataset, organised by what the data *is*, that
eMapR and other researchers query by area and date. It runs on `/vol/v1`
on Iowa first and moves to shared research storage later.

## Decisions

### 1. One library root per product family, named for researchers

```
/vol/v1/Landsat-ARD/                       root (README.md, LIBRARY.yaml)
  collection_2/                            product version (PRODUCT.yaml)
    conus/h003v004/  alaska/AK_h021v008/  hawaii/HI_h001v001/
  catalog/  staging/<user>/  admin/
```

- The root is a product family (`Landsat-ARD`). A future Sentinel-2 L2A
  archive is a sibling root with its own catalog, not a subfolder. The
  code refuses a second product family in one root.
- The root sits at `/vol/v1/Landsat-ARD` (D1), on the same filesystem as
  the project archives in `/vol/v1/data`, so the migration can use hard
  links.
- Layout 1 (`products/<product>/<version>/<grid system>/<tile>`) was a
  proof of concept. Layout 2 replaces it and nothing supports both.

### 2. Collection, then native grid, then tile

- **Collection / version.** `collection_2` is USGS Collection 2 *and*
  the exact acquisition definition in `PRODUCT.yaml`: SR bands + QA_PIXEL,
  native uint16, no cloud filter, no masking, from 1990 (D3). Anything
  that DataLoader treats as part of a dataset's identity (bands, encoding,
  masking, filters, start year, product family) is fixed. Changing it
  makes a new sibling folder (e.g. a future Collection 3, or a pre-1990
  version), never an edit. Extending the end date is not a change:
  updates add newly published observations.
- **1990 start (D3).** All existing archives start in 1990. A 1982 start
  would be a different request identity, and the existing 117,120
  observations couldn't be imported without downloading again. Pre-1990
  TM data (about 6% more observations) can be added later as its own
  version.
- **Native grid.** CONUS, Alaska and Hawaii ARD are three Albers
  projections, each numbering tiles from `h000v000`. Folders use short
  names (`conus`, `alaska`, `hawaii`), mapped in `PRODUCT.yaml`
  `grid_dirs` to DataLoader's grid systems (`usgs_ard_conus`, ...), which
  manifests and the catalog record.
- **Tile ids keep their region prefix (D2):** `alaska/AK_h021v008`, not
  `alaska/h021v008`. The id is what manifests, file names, item ids and
  the catalog use. A folder copied out of context still says which grid
  it is on. DataLoader 1.0 once fetched CONUS `h021v008` (Illinois) for an
  Alaska tile because the id lacked its region; the prefix prevents a
  repeat. CONUS ids stay bare (`h003v004`), as USGS and DataLoader write
  them.
- **One DataLoader dataset per tile.** A tile folder is exactly what
  `data-loader run` writes for a one-tile request. Acquisition, resume,
  update and verify need no library code, and a tile can be copied or
  checked on its own.

### 3. Catalog: generated, never edited, stable names

- `catalog/tiles.parquet`, `observations.parquet` and `catalog.json` are
  symlinks through `catalog/current` to the latest build in
  `catalog/builds/<id>/`. A rebuild writes a new build and switches
  `current` in one atomic rename. Old builds are kept for an hour so
  running readers can finish.
- The catalog is rebuilt from the tiles' own `manifest.json` and
  `items.jsonl`. It holds only paths relative to the root, so it works
  unchanged after the archive moves.

### 4. Staging for new tiles (D4)

- New tiles are acquired into `staging/<user>/<version>/<grid>/<tile>`,
  then checksum-verified. Only a complete, verified tile is renamed into
  place (same filesystem, one atomic rename). The verify is recorded for
  the tile's final path, so the catalog shows it as `verified`.
  Interrupted runs resume in staging. A tile another maintainer is staging
  is skipped.
- Tiles already in the library are updated in place. DataLoader writes
  every file as temp file + rename, and the catalog marks the tile
  `updating` while a run holds it. Staging a copy of a 400 GB tile for
  every update would double the space for no gain.
- Imports from project archives are assembled in `staging/<user>/` too.
- `staging/` and `admin/` are maintainers-only (mode 2770).
- A moved dataset keeps its old absolute `request.output.dir` in
  `manifest.json`. DataLoader doesn't use it. A relocated tile plans
  "0 to do" (tested 2026-10-29).

### 5. Hard-link migration; production files untouched until retired (D5, D7)

- `/vol/v1` is XFS (v4, `crc=0`, `reflink=0`, `noquota`), with
  `/vol/v1/data` on the same device. Hard links work. Reflink copies do not.
  A copy of the existing archives (about 10.7 TB) doesn't fit in the free
  space; hard links add under 1 GB.
- A hard link shares the file. Its bytes can't diverge, because DataLoader
  and the library never write in place (temp file + rename,
  `data_loader/geotiff.py`, `data_loader/dataset.py`). But a `chmod`
  through either name changes both. Tested on `/vol/v1` 2026-10-29.
- **D5:** the permission policy skips any file with more than one name
  (`permissions.py`, reported as `shared`). So a hard-link import changes
  nothing about the production files: not bytes, mode, owner, group, ACL
  or mtime. Only the link count and inode ctime change. Read-only sealing
  (0444) happens on the first `permissions --fix` after the old project
  names are removed.
- **D7:** the old project archive names are removed no sooner than 30
  days after the library is fully verified and catalogued, and only on
  explicit approval. Until then the library can be rolled back by deleting
  its tile folders.
- Every source-metadata record in the four production archives is used by
  exactly one observation (checked 2026-10-29: 117,120 rows, 0 shared), so
  after retirement every file is sealed.
- Rules for everyone: never edit library files in place (no `gdal_edit`).
  Backups and rsync of `/vol/v1` must preserve hard links (`-H`) while
  both names exist.

### 6. Moving to shared storage later

The root is self-contained: relative paths everywhere (catalog, tile
datasets, relative symlinks). After retirement each file has one name.

1. On `/vol/v1`: full verify, catalog, permissions sealed, old names retired.
2. `rsync -aHAX --numeric-ids /vol/v1/Landsat-ARD/ DEST/Landsat-ARD/` (about 27 h for
   10.7 TB at 1 Gb/s; about 38 h with Tanana).
3. On the destination: `imagery-library verify` (full SHA-256, about 35 h over 1 Gb/s
   NFS), `imagery-library catalog`, `imagery-library permissions --fix`.
   Set `DATALOADER_LOCK_DIR=DEST/Landsat-ARD/admin/locks` in the
   maintainers' environment files.
4. Delete the `/vol/v1` copy only after step 3 passes.

The destination must provide POSIX semantics: atomic rename, fsync,
`flock` across clients (NFSv4 with server-side locks), symlinks and hard
links within the volume. A **hard** mount if maintainers write over NFS
(a soft mount can drop writes on timeout). About 20 TB now plus about
1 TB per year. A maintainers' group. A daily metadata backup
(`imagery-library backup-list --tier metadata`).

### 7. Who maintains it (D6: open)

Maintainers acquire, verify, rebuild the catalog and answer requests;
researchers only read. Named in `LIBRARY.yaml` and the README. Still to
decide: the other maintainers, a contact address, and a maintainers' Unix
group from the admins (`permissions.group`, needed for setgid folders and
because `clarype` is not in `trenders`).

## Consequences

- DataLoader is unchanged; `imagery_library` changed: `layout.py` (roots,
  versions, `grid_dirs`, reserved names, staging), `permissions.py`
  (layout-2 paths, shared-file rule), `catalog.py` (builds + stable
  names, `grid` column, `verify_dataset`), `acquire.py` (staging,
  `promote`), `importer.py` (staging), CLI `init --readme` and `promote`.
- Catalog columns: `grid` (folder name) added; `grid_system`
  (DataLoader's name) kept.
- Adding a product family (e.g. Sentinel-2) is a new root with its own
  `PRODUCT.yaml`. Only naming *missing* tiles of a new grid needs a helper
  in `grids.py` (and tile-mode support in that DataLoader provider).
