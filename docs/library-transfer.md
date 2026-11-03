# Imagery library: transfer to shared storage (runbook)

Moves the verified Landsat-ARD library from `/vol/v1/Landsat-ARD` (Islay)
to the permanent shared root `/home/server/datasets/Landsat-ARD`. Why it
is done this way: [library-architecture.md](library-architecture.md) §6.
Rehearsed end to end on a throwaway library on the destination
(2026-11-02): interrupted and killed copies resumed, a corrupted image
was caught and repaired, promote, a staged tile promoted, catalog query
and `data-loader plan` from the new path.

Nothing here writes to `/vol/v1`. The project archives and
`/vol/v1/Landsat-ARD` stay as they are.

| Step | What | Time (10.725 TB, 234,419 files, 1 Gb/s) |
|---|---|---|
| 0 | Record the source (done 2026-11-02: `~/landsat-ard-transfer/kit`) | 25 s |
| 1 | Copy into `staging/clarype/Landsat-ARD` (rsync, resumable) | about 35 h (85 MB/s) |
| 2 | Check the copy (`transfer_library.sh check`) | about 26-27 h (network-bound full SHA-256) |
| 3 | Promote into the permanent root (renames) | under a minute |

## Before starting

- No acquisition, update, import or catalog run on `/vol/v1/Landsat-ARD`
  until step 3 is done. If the source changes, record it again into a new
  kit directory (`prepare` refuses to overwrite one).
- `findmnt -no OPTIONS /home/server/datasets | tr , '\n' | grep -x -e hard -e soft`
  should say `hard` (see architecture §6). With `soft` the copy is still
  safe, because step 2 checks every byte. But an outage longer than about
  6 minutes fails the files in flight, and you rerun step 1.
- `ls -A /home/server/datasets/Landsat-ARD` shows only `staging`, and
  `ls -A /home/server/datasets/Landsat-ARD/staging/clarype` shows nothing.

## Steps 1 and 2: copy, then check (one command, in tmux)

```bash
tmux new -s ard_transfer
```

then paste:

```bash
rsync -rlptH --chmod=g-w --fsync --exclude=/staging/ --no-inc-recursive --info=progress2,stats2 --log-file=/home/server/pi/homes/clarype/landsat-ard-transfer/rsync.log /vol/v1/Landsat-ARD/ /home/server/datasets/Landsat-ARD/staging/clarype/Landsat-ARD/ && /vol/v1/data-loader-lib/library/transfer_library.sh check /home/server/pi/homes/clarype/landsat-ard-transfer/kit /home/server/datasets/Landsat-ARD/staging/clarype/Landsat-ARD 2>&1 | tee -a /home/server/pi/homes/clarype/landsat-ard-transfer/check.log
```

The rsync options:

| Option | Why |
|---|---|
| `-r -l -p -t` | recursive; symlinks as symlinks (catalog); modes; modification times |
| `-H` | hard links within the library stay hard links |
| `--chmod=g-w` | no group write at the destination (the files' group becomes your primary group there) |
| `--fsync` | each file is flushed to the server before it gets its final name; costs no measurable speed |
| `--exclude=/staging/` | staging is per filesystem and isn't copied |
| `--no-inc-recursive` | full file list up front, so progress and ETA are for the whole job |
| `--info=progress2,stats2`, `--log-file` | one progress line; every file logged |
| no `-o -g -A -X` | owner and group can't be kept as a normal user; the `/vol/v1` ACLs only mirror the modes, and NFSv4 can't store them |
| no `--delete` | nothing at the destination is ever removed by rsync |

rsync writes each file under a temporary name (`.<name>.XXXXXX`) and
renames it when it is complete, so no file at its final name is ever half
written. Don't add `--partial-dir`: a read-only source file makes the
saved partial read-only, and the rerun can't reopen it (seen in the
rehearsal).

### Monitoring

- `tmux attach -t ard_transfer` (detach: `Ctrl-b d`). The progress line
  shows bytes copied, %, MB/s and the time left.
- From any shell, files done out of 234,419:
  `grep -c '] >f' /home/server/pi/homes/clarype/landsat-ard-transfer/rsync.log`
- Errors so far: `grep -E 'rsync: |rsync error' /home/server/pi/homes/clarype/landsat-ard-transfer/rsync.log`
- The check starts by itself when rsync succeeds. It prints `== 1/5` to
  `== 5/5`, then one line per tile as the imagery verify proceeds (30
  tiles), and ends with `CHECK PASSED` (in `check.log`).

### If it is interrupted

Paste the same command again, in the same or a new tmux session. rsync
skips every file already copied (same size and mtime) and copies the
rest. That includes after a reboot, a lost session, `Ctrl-C`, or rsync
exit codes 20, 23 or 30. The check runs again from the start.

If `check` stops, it says why and what to do:
- **temp files left by an interrupted rsync**: it prints the exact `rm`
  command for those files only. Run it, then paste the command again.
- **file lists differ**: paste the command again (rsync copies what is
  missing or different).
- **verify reported problems**: it lists the failed files and prints the
  `rm` command for them. Delete them, then paste the command again. rsync
  copies them again because they're missing, and the check re-verifies.

## Step 3: promote into the permanent root

Only after `CHECK PASSED`:

```bash
/vol/v1/data-loader-lib/library/transfer_library.sh promote /home/server/pi/homes/clarype/landsat-ard-transfer/kit /home/server/datasets/Landsat-ARD
```

It refuses unless the check passed, both paths are on the same
filesystem, and the permanent root holds no library yet. It renames
`README.md`, `admin`, `catalog`, `collection_2` and then `LIBRARY.yaml`
into place, rebuilds the catalog there, and confirms the 30 tiles equal
the source. `staging/clarype` is left empty.

## After the transfer

- Researchers use `/home/server/datasets/Landsat-ARD` (README at the root).
- Install the updated README (new location; maintainers once D6 is
  decided): `cp /vol/v1/data-loader-lib/library/landsat_ard/README.md /home/server/datasets/Landsat-ARD/README.md`.
- Once the maintainers' group exists: `imagery-library permissions --fix`
  at the new root (seals imagery 0444; files have one name there).
- Acquisitions and updates run against the new root, with
  `DATALOADER_LOCK_DIR=/home/server/datasets/Landsat-ARD/admin/locks`
  (after the DataLoader 1.2 release and a hard mount).
- `/vol/v1/Landsat-ARD` and the project archives are handled by a
  separate decision (D7, not before 2026-11-30).
