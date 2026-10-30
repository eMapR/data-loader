"""Maintainer side: turn "this AOI needs imagery" into per-tile DataLoader
runs, one per tile, never two at once on one source account.

A project never writes its own acquisition settings: the product spec
(PRODUCT.yaml) supplies them, the library supplies output.dir, and tiles
already in the library are reported, not acquired again. Re-running a held
tile is how it gets newly published observations (DataLoader resumes;
nothing already acquired is re-downloaded).

New tiles are staged: DataLoader writes them to staging/<user>/<version>/
<grid dir>/<tile>, and only a complete run whose files pass a checksum
verify is renamed into <version>/<grid dir>/<tile> (same filesystem, one
atomic rename). Until then the catalog does not see the tile, and an
interrupted run resumes in staging. Tiles the library already holds are
updated in place (the catalog shows them as `updating` while a run holds
them; DataLoader writes every file by temp file + rename).

Why duplicates cannot happen:
- a tile has exactly one directory: version/grid/<canonical tile id>
  (`Product.locate` turns CU_h003v004 into h003v004, and so on), and one
  staging directory per maintainer; a tile another maintainer is staging
  is skipped;
- DataLoader holds an exclusive lock on that directory for a whole run, so
  a second run on the same tile fails at once instead of writing;
- DataLoader itself holds a lock per source account for every run
  (data_loader.account_lock; USGS M2M allows one session per account).
  `acquire` points it at the library (DATALOADER_LOCK_DIR=admin/locks) and
  queues behind it (--wait-for-account); maintainers put the same
  DATALOADER_LOCK_DIR in their credentials env file, so a direct
  `data-loader run` honours the same lock. A run then resumes whatever an
  earlier one acquired.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

from imagery_library.grids import tiles_for_aoi
from imagery_library.layout import (ADMIN_DIR, STAGING_DIR, Library, LibraryError, Product, append_jsonl, fsync_dir,
                                    software)
from imagery_library.query import read_aoi

LOCK_DIR = "locks"


def tiles_needed(lib: Library, product: Product, *, aoi=None, tiles: Optional[list] = None) -> list[dict]:
    """[{grid_system, tile, aoi_share, held, published}] for an AOI or explicit
    tile ids. With an AOI, grid positions the source does not publish (ocean,
    outside the country) are left out."""
    held = {(g, t) for g, t, _ in product.tiles()}
    published = product.published_tiles()
    out = []
    if aoi is not None:
        geom = read_aoi(aoi)
        for system in product.grid_systems:
            found = tiles_for_aoi(system, geom)
            if found is None:
                raise LibraryError(f"{product.key}: no tile helper for grid system {system!r}; list tiles explicitly")
            out += [t for t in found if published is None or t["tile"] in published.get(system, ())]
    for t in tiles or []:
        try:
            system, canonical = product.locate(t)
        except ValueError as e:
            raise LibraryError(str(e)) from None
        if published is not None and canonical not in published.get(system, ()):
            raise LibraryError(f"{system}/{canonical}: the source publishes no data for this tile "
                               f"({product.key} published_tiles)")
        out.append({"grid_system": system, "tile": canonical, "aoi_share": None})
    seen, uniq = set(), []
    for r in out:
        k = (r["grid_system"], r["tile"])
        if k not in seen:
            seen.add(k)
            uniq.append({**r, "held": k in held, "staged_by": staged_by(lib, product, *k)})
    return uniq


def staged_by(lib: Library, product: Product, grid_system: str, tile: str) -> list:
    """Maintainers whose staging area holds this tile (an unfinished or unverified acquisition)."""
    rel = product.tile_rel(grid_system, tile)
    sdir = lib.root / STAGING_DIR
    return sorted(u.name for u in sdir.iterdir() if (u / rel).is_dir()) if sdir.is_dir() else []


def promote(lib: Library, product: Product, grid_system: str, tile: str, staged: Path, *, log=print) -> bool:
    """Move a staged tile into place if it is complete and its files pass a
    checksum verify; the verify is recorded for the tile's library path."""
    import json

    from imagery_library.catalog import verify_dataset

    dest = product.tile_path(grid_system, tile)
    rel = str(product.tile_rel(grid_system, tile))
    if not (staged / "manifest.json").exists():
        log(f"  {rel}: nothing staged at {staged}")
        return False
    if not json.loads((staged / "manifest.json").read_text()).get("dataset", {}).get("complete"):
        log(f"  {rel}: staged dataset is not complete; left in {staged}")
        return False
    if dest.exists():
        raise LibraryError(f"{dest} already exists; not replacing it with the staged copy in {staged}")
    row = verify_dataset(lib, product, tile, staged, rel, log=log)
    if not row["ok"]:
        log(f"  {rel}: staged files failed verification; left in {staged}")
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.rename(staged, dest)
    fsync_dir(dest.parent)
    for d in list(staged.parents)[:3]:  # empty <grid dir>/<version>/<user> staging folders
        try:
            d.rmdir()  # only if empty
        except OSError:
            break
    log(f"  {rel}: verified and moved into place")
    return True


def config_name(product: Product, grid_system: str, tile: str) -> str:
    return f"{product.name}_{product.version}_{grid_system}_{tile}.yaml"


def write_configs(lib: Library, product: Product, needed: list[dict], out_dir, *, include_held: bool = False,
                  staging: Optional[Path] = None) -> list[Path]:
    """One DataLoader config per tile (missing tiles; plus held ones with
    include_held, which is how a maintainer updates them). With `staging`
    (a maintainer's staging area), missing tiles are written there."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for r in needed:
        if r["held"] and not include_held:
            continue
        out = None if r["held"] or staging is None else staging / product.tile_rel(r["grid_system"], r["tile"])
        cfg = product.config_for_tile(r["tile"], r["grid_system"], out_dir=out)
        name = config_name(product, r["grid_system"], r["tile"])
        header = (f"# Generated by imagery-library from {product.key} PRODUCT.yaml -- do not edit; change the product.\n"
                  f"# {'update of a held tile' if r['held'] else 'new tile'}: {product.grid_dir(r['grid_system'])}/{r['tile']}\n"
                  f"# Run it with `imagery-library acquire`, which queues it behind other runs on the same account.\n")
        p = out_dir / name
        p.write_text(header + yaml.safe_dump(cfg, sort_keys=False))
        written.append(p)
    return written


def lock_dir(lib: Library) -> Path:
    """The DATALOADER_LOCK_DIR every maintainer uses with this library."""
    return lib.root / ADMIN_DIR / LOCK_DIR


def acquire(lib: Library, product: Product, needed: list[dict], *, include_held: bool = False, wait: bool = True,
            data_loader: Optional[str] = None, log=print) -> list[dict]:
    """Run DataLoader for each tile in turn. DataLoader holds the account
    lock (in the library's admin/locks) and the tile lock; this waits its
    turn (or, with wait=False, skips tiles while the account is busy).
    Output of each run goes to admin/logs/<grid>_<tile>.log. Credentials come
    from the environment, as for `data-loader run`."""
    from imagery_library.permissions import apply_quietly

    import getpass

    exe = data_loader or str(Path(sys.executable).with_name("data-loader"))
    user = getpass.getuser()
    staging = lib.staging_dir(user)
    busy = [r for r in needed if not r["held"] and set(r.get("staged_by") or []) - {user}]
    for r in busy:
        log(f"  {product.grid_dir(r['grid_system'])}/{r['tile']}: being staged by {', '.join(r['staged_by'])}; skipped")
    needed = [r for r in needed if r not in busy]
    cfg_dir = lib.admin_dir / "configs" / product.name / product.version
    configs = write_configs(lib, product, needed, cfg_dir, include_held=include_held, staging=staging)
    env = dict(os.environ, DATALOADER_LOCK_DIR=str(lock_dir(lib)))
    lock_dir(lib).mkdir(parents=True, exist_ok=True)
    set_dir = os.environ.get("DATALOADER_LOCK_DIR")
    if set_dir and Path(set_dir).resolve() != lock_dir(lib).resolve():
        log(f"  note: DATALOADER_LOCK_DIR is {set_dir} in this shell; using {lock_dir(lib)} for library runs")
    results = []
    for cfg in configs:
        r = next(x for x in needed if config_name(product, x["grid_system"], x["tile"]) == cfg.name)
        logf = lib.admin_dir / "logs" / f"{r['grid_system']}_{r['tile']}.log"
        logf.parent.mkdir(parents=True, exist_ok=True)
        t0 = time.monotonic()
        log(f"  {product.grid_dir(r['grid_system'])}/{r['tile']}: data-loader run ({'update' if r['held'] else 'new'}) -> {logf.name}")
        args = [exe, "run", str(cfg)] + (["--wait-for-account"] if wait else [])
        with open(logf, "a") as out:
            code = subprocess.run(args, stdout=out, stderr=subprocess.STDOUT, env=env).returncode
        outcome = {0: "complete", 3: "incomplete (failed/pending rows)", 5: "stopped (source/credentials/disk)",
                   6: "skipped: account busy",
                   2: "refused (config, or another run is using this tile)"}.get(code, f"error {code}")
        staged = staging / product.tile_rel(r["grid_system"], r["tile"])
        promoted = None
        if not r["held"]:
            promoted = code == 0 and promote(lib, product, r["grid_system"], r["tile"], staged, log=log)
            if not promoted and staged.exists():
                outcome += f"; staged in {staged.relative_to(lib.root)} (run again to resume)"
        row = {**r, "exit": code, "outcome": outcome, "promoted": promoted,
               "seconds": round(time.monotonic() - t0, 1)}
        results.append(row)
        append_jsonl(lib.admin_dir / "acquisitions.jsonl",
                     {"time": datetime.now(timezone.utc).replace(microsecond=0).isoformat(), "product": product.key,
                      **{k: row[k] for k in ("grid_system", "tile", "held", "exit", "outcome", "promoted", "seconds")},
                      "software": software()})
        tile_dir = product.tile_path(r["grid_system"], r["tile"])
        apply_quietly(lib, [p for p in (tile_dir, staged, lib.admin_dir) if p.exists()])
        log(f"  {product.grid_dir(r['grid_system'])}/{r['tile']}: {outcome} ({row['seconds']} s)")
        if code == 5:  # the source or credentials are the problem; the next tile would fail the same way
            log("  stopping: fix the cause and run the same command again to resume")
            break
    return results
