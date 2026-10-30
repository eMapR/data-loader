"""The library catalog: generated from the tile datasets, never edited.

    catalog/tiles.parquet -> current/tiles.parquet   stable names researchers open (also
    catalog/observations.parquet, catalog.json)      observations.parquet, catalog.json)
    catalog/current -> builds/<build-id>/            swapped atomically when a build finishes
    catalog/builds/<build-id>/tiles.parquet          GeoParquet, one row per tile dataset (footprint in EPSG:4326)
    catalog/builds/<build-id>/observations.parquet   one row per acquired observation
    catalog/builds/<build-id>/catalog.json           build summary

The stable names resolve through `current` each time a file is opened; a
reader that needs tiles and observations from the same build resolves
`current` once (as `current_catalog` and the query API do).

Tile manifests and items.jsonl are the source of truth; the catalog can be
rebuilt from them at any time and reads no imagery.

Safe during acquisitions:
- a build writes a new directory and then repoints `current` with one
  atomic rename, so a reader sees the previous build or the new one, never
  a mix; old builds are kept (the current one always, others for at least
  BUILD_MIN_AGE_S) so a reader that resolved `current` earlier can finish;
- one build at a time (catalog/.build.lock);
- a tile a DataLoader run is writing is read without locking it: its
  journal first, then items.jsonl, keeping the newer row per observation,
  so a snapshot DataLoader writes in between cannot drop rows. DataLoader
  records an observation only after its GeoTIFF is fsynced and renamed
  into place, so every catalogued file exists;
- each tile and observation says what state it is in (see TILE STATES).

TILE STATES
    acquisition:  updating    a DataLoader run holds the tile now
                  incomplete  failed or pending observations, or the last run did not finish
                              (superseded processing versions do not count: completeness is
                              judged against the source's current inventory)
                  complete    everything discovered so far is acquired
    verification: verified            last verify passed and covers every observation
                  partially_verified  last verify passed; observations added since are not covered
                  failed              last verify found missing or corrupt files
                  unverified          never verified (or imagery not present here)
    An observation is `verified` when the last passing checksum verify of
    its tile checked it as it is now: each verify records exactly which
    observations it covered (admin/verified/...json, key -> row version), so
    there is no clock comparison and a re-acquired file needs a new verify.
"""
from __future__ import annotations

import fcntl
import json
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from imagery_library.grids import footprint_lonlat
from imagery_library.layout import (LINEAGE_FILE, Library, LibraryError, _resolved_request, append_jsonl,
                                    atomic_write, fsync_dir, software, spec_hash, spec_identity)

CURRENT = "current"
BUILDS = "builds"
CATALOG_FILES = ("tiles.parquet", "observations.parquet", "catalog.json")
KEEP_BUILDS = 3
BUILD_MIN_AGE_S = 3600
VERIFY_LOG = "verification.jsonl"
BUILD_LOCK = ".build.lock"
VERIFIED_DIR = "verified"


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def run_in_progress(tile_dir: Path) -> bool:
    """True if a DataLoader run holds this tile's lock right now (read-only probe)."""
    lock = tile_dir / ".state" / "lock"
    if not lock.exists():
        return False
    fd = os.open(lock, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    except OSError:  # pragma: no cover - filesystems without flock
        return False
    finally:
        os.close(fd)
    return False


def _jsonl(path: Path) -> list[dict]:
    out = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass  # a line being written right now
    except FileNotFoundError:
        pass
    return out


def tile_rows(path: Path, manifest: dict) -> list[dict]:
    """Every items row of a tile dataset, consistent while DataLoader writes it."""
    journal = _jsonl(path / ".state" / "journal.jsonl")
    items = _jsonl(path / manifest.get("items", "items.jsonl"))
    rows: dict[str, dict] = {}
    for r in items + journal:
        old = rows.get(r["key"])
        if old is None or (r.get("updated") or "") >= (old.get("updated") or ""):
            rows[r["key"]] = r
    return list(rows.values())


def _latest_verification(lib: Library) -> dict:
    out = {}
    for r in _jsonl(lib.admin_dir / VERIFY_LOG):
        if r.get("ok") is not None and r.get("checksums"):  # a sizes-only check verifies nothing
            out[r["path"]] = r  # later lines win
    return out


def _tile_records(lib: Library, product, system: str, tile: str, path: Path, verified: dict):
    m = json.loads((path / "manifest.json").read_text())
    rel = str(path.relative_to(lib.root))
    lineage = json.loads((path / LINEAGE_FILE).read_text()) if (path / LINEAGE_FILE).exists() else None
    imagery = True if lineage is None else bool(lineage.get("imageryPresent", True))
    active = run_in_progress(path)
    rows = tile_rows(path, m)
    counts = {"acquired": 0, "failed": 0, "pending": 0, "filtered": 0}
    for r in rows:
        counts[r.get("status", "pending")] = counts.get(r.get("status", "pending"), 0) + 1
    acquired = [r for r in rows if r.get("status") == "acquired"]
    sensor, grid = next((s, g[tile]) for s, g in m["grids"].items() if tile in g)
    v = verified.get(rel)
    covered = {}
    if imagery and v and v.get("ok"):
        cf = lib.admin_dir / VERIFIED_DIR / f"{rel}.json"
        covered = json.loads(cf.read_text())["rows"] if cf.exists() else {}

    def obs_verified(r) -> bool:
        return r["key"] in covered and covered[r["key"]] == r.get("updated")

    n_verified = sum(1 for r in acquired if obs_verified(r))
    if active:
        acq_state = "updating"
    elif counts["failed"] or counts["pending"] or not m.get("dataset", {}).get("complete"):
        acq_state = "incomplete"
    else:
        acq_state = "complete"
    if not imagery or v is None:
        ver_state = "unverified"
    elif not v.get("ok"):
        ver_state = "failed"
    elif n_verified == len(acquired):
        ver_state = "verified"
    else:
        ver_state = "partially_verified"
    failed = [r for r in rows if r.get("status") == "failed"]
    commits = sorted({(r.get("software") or {}).get("gitCommit") or "unknown" for r in acquired})
    request = _resolved_request(m.get("request", {}), m.get("products"))
    shash = spec_hash(spec_identity(request))
    tile_row = {
        "product": product.name, "version": product.version, "grid_system": system,
        "grid": product.grid_dir(system), "tile": tile, "path": rel, "geometry": footprint_lonlat(grid),
        "acquisition_state": acq_state, "verification_state": ver_state,
        "observations": counts["acquired"], "verified_observations": n_verified,
        "failed": counts["failed"], "pending": counts["pending"], "filtered": counts["filtered"],
        "superseded": counts.get("superseded", 0),
        "retired_versions_held": sum(1 for r in acquired if r.get("supersededBy")),
        "failed_example": (failed[0].get("error") or "")[:300] if failed else None,
        "first_date": min((r["date"] for r in acquired if r.get("date")), default=None),
        "last_date": max((r["date"] for r in acquired if r.get("date")), default=None),
        "bytes": sum(f.get("bytes", 0) for r in acquired for f in r.get("files", [])),
        "open_ended": bool(m.get("coverage", {}).get("openEnded")),
        "discovered_through": m.get("coverage", {}).get("discoveredThrough"),
        "updated": max([m.get("dataset", {}).get("updated") or ""] + [r.get("updated") or "" for r in rows]) or None,
        "last_verified": v.get("time") if v else None, "last_verify_checksums": v.get("checksums") if v else None,
        "imagery_present": imagery, "imported_from": None if lineage is None else lineage["source"]["path"],
        "spec_hash": shash, "spec_matches_product": shash == product.spec_hash(tile, system),
        "dataloader_commits": ",".join(commits),
        "crs_wkt": grid.get("crsWkt"), "transform": list(grid.get("transform", [])[:6]),
        "width": grid.get("width"), "height": grid.get("height"),
    }
    obs = []
    for r in acquired:
        prov = r.get("provenance") or {}
        f = next((f for f in r.get("files", []) if f.get("bandSet") == "bands"), (r.get("files") or [{}])[0])
        si = r.get("sourceIntegrity") or {}
        obs.append({
            "product": product.name, "version": product.version, "grid_system": system,
            "grid": product.grid_dir(system), "tile": tile, "key": r["key"], "item_id": r.get("itemId"), "sensor": r.get("sensor"), "platform": r.get("platform"),
            "date": r.get("date"), "datetime": r.get("datetime"), "season_year": r.get("seasonYear"),
            "cloud_percent": r.get("eo:cloud_cover"), "fill_percent": r.get("fillPercent"),
            "valid_fraction": r.get("validFraction"),
            "upstream_product_id": prov.get("upstream_product_id"), "collection": prov.get("collection"),
            "processing_date": prov.get("generation_time"), "processing_baseline": prov.get("processing_baseline"),
            "path": f"{rel}/{f['path']}" if f.get("path") else None, "bytes": f.get("bytes"), "sha256": f.get("sha256"),
            "files": len(r.get("files", [])), "written": r.get("updated"),
            "superseded_by": r.get("supersededBy"),  # a newer version exists: this file is likely irreplaceable
            "source_verified": bool(si.get("of")) and si.get("files") == si.get("of"),
            "verified": obs_verified(r),
            "dataloader_commit": (r.get("software") or {}).get("gitCommit"),
        })
    return tile_row, obs


class _BuildLock:
    def __init__(self, lib: Library):
        lib.catalog_dir.mkdir(exist_ok=True)
        self.fd = os.open(lib.catalog_dir / BUILD_LOCK, os.O_RDWR | os.O_CREAT, 0o664)

    def __enter__(self):
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self.fd)
            raise LibraryError("another catalog build is running; run again when it has finished") from None
        return self

    def __exit__(self, *exc):
        os.close(self.fd)


def _prune(lib: Library, keep_target: Path) -> None:
    bdir = lib.catalog_dir / BUILDS
    builds = sorted(p for p in bdir.iterdir() if p.is_dir() and not p.is_symlink() and p.name[:1].isdigit())
    now = time.time()
    for old in builds[:-KEEP_BUILDS]:
        if old.resolve() == keep_target or now - old.stat().st_mtime < BUILD_MIN_AGE_S:
            continue
        shutil.rmtree(old, ignore_errors=True)


def build_catalog(lib: Library, *, log=print) -> Path:
    import geopandas as gpd
    import pandas as pd

    with _BuildLock(lib):
        verified = _latest_verification(lib)
        tiles, obs, problems = [], [], []
        for product, system, tile, path in lib.tiles():
            try:
                t, o = _tile_records(lib, product, system, tile, path, verified)
            except Exception as e:  # one unreadable tile must not hide the others
                problems.append(f"{path.relative_to(lib.root)}: {type(e).__name__}: {e}")
                continue
            if not t["spec_matches_product"]:
                problems.append(f"{t['path']}: spec hash {t['spec_hash']} does not match {product.key}; left out")
                continue
            tiles.append(t)
            obs.extend(o)
        build_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        out = lib.catalog_dir / BUILDS / build_id
        out.mkdir(parents=True)
        cols = ["product", "version", "grid_system", "tile", "path", "geometry"]
        tdf = gpd.GeoDataFrame(tiles, geometry="geometry", crs="EPSG:4326") if tiles else \
            gpd.GeoDataFrame({c: [] for c in cols}, geometry="geometry", crs="EPSG:4326")
        tdf.to_parquet(out / "tiles.parquet", index=False)
        odf = pd.DataFrame(obs) if obs else \
            pd.DataFrame({c: [] for c in ("product", "version", "grid_system", "tile", "date", "path", "verified")})
        odf.to_parquet(out / "observations.parquet", index=False)
        states = {}
        for t in tiles:
            for k in ("acquisition_state", "verification_state"):
                states.setdefault(k, {}).setdefault(t[k], 0)
                states[k][t[k]] += 1
        summary = {
            "library": lib.name, "built": _now(), "build": build_id, "builder": software(),
            "tiles": len(tiles), "observations": len(obs),
            "verified_observations": int(sum(t["verified_observations"] for t in tiles)),
            "bytes": int(sum(t["bytes"] for t in tiles)), "states": states,
            "products": sorted({f"{t['product']}/{t['version']}" for t in tiles}), "problems": problems,
        }
        atomic_write(out / "catalog.json", json.dumps(summary, indent=2) + "\n")
        fsync_dir(out)
        link = lib.catalog_dir / CURRENT
        tmp = lib.catalog_dir / f".{CURRENT}.{os.getpid()}"
        if tmp.is_symlink() or tmp.exists():
            tmp.unlink()
        os.symlink(f"{BUILDS}/{build_id}", tmp)
        os.replace(tmp, link)
        for name in CATALOG_FILES:  # stable names, created once; they follow `current`
            if not (lib.catalog_dir / name).is_symlink():
                os.symlink(f"{CURRENT}/{name}", lib.catalog_dir / name)
        fsync_dir(lib.catalog_dir)
        _prune(lib, out.resolve())
        from imagery_library.permissions import apply_quietly

        apply_quietly(lib, [out, lib.catalog_dir / BUILD_LOCK])
    log(f"catalog {build_id}: {len(tiles)} tiles, {len(obs)} observations "
        f"({summary['verified_observations']} verified), {summary['bytes'] / 1e12:.3f} TB; "
        + ", ".join(f"{s} {n}" for d in states.values() for s, n in sorted(d.items()))
        + (f"; {len(problems)} problem(s)" if problems else ""))
    for p in problems[:20]:
        log(f"  problem: {p}")
    return out


def current_catalog(lib: Library) -> Path:
    link = lib.catalog_dir / CURRENT
    if not link.exists():
        raise LibraryError(f"{lib.root}: no catalog yet (maintainers: `imagery-library catalog {lib.root}`)")
    return link.resolve()


def verify_tiles(lib: Library, *, product: Optional[str] = None, tiles: Optional[list] = None,
                 checksums: bool = True, log=print) -> list[dict]:
    """Run DataLoader's verify on each tile dataset and log the result.
    Tiles a run is writing are skipped (verify them when it finishes)."""
    results = []
    sw = software()
    for p, system, tile, path in lib.tiles(product=product):
        if tiles and tile not in tiles:
            continue
        rel = str(path.relative_to(lib.root))
        lineage = json.loads((path / LINEAGE_FILE).read_text()) if (path / LINEAGE_FILE).exists() else {}
        if lineage.get("imageryPresent") is False:
            log(f"  {rel}: metadata-only tile, skipped")
            continue
        if run_in_progress(path):
            log(f"  {rel}: a DataLoader run is updating it; skipped")
            results.append({"path": rel, "tile": tile, "ok": None, "skipped": "updating"})
            continue
        results.append(verify_dataset(lib, p, tile, path, rel, checksums=checksums, software_=sw, log=log))
    return results


def verify_dataset(lib: Library, product, tile: str, path: Path, rel: str, *, checksums: bool = True,
                   software_: Optional[dict] = None, log=print) -> dict:
    """DataLoader's verify of the tile dataset at `path`, logged under the
    tile's library path `rel` (they differ when a staged tile is verified
    before it is moved into place)."""
    from data_loader.dataset import open_dataset

    started = _now()
    t0 = time.monotonic()
    ds = open_dataset(path)
    checked = {r["key"]: r.get("updated") for r in ds.items()}  # what this verify covers, taken first
    problems = ds.verify(checksums=checksums)
    files = sum(1 for _ in ds.files(band_set=None))
    if checksums:
        cf = lib.admin_dir / VERIFIED_DIR / f"{rel}.json"
        cf.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(cf, json.dumps({"started": started, "ok": not problems,
                                     "rows": checked if not problems else {}}) + "\n")
    row = {"time": _now(), "started": started, "path": rel, "product": product.key, "tile": tile,
           "ok": not problems, "checksums": checksums, "files": files, "problems": len(problems),
           "examples": problems[:10], "seconds": round(time.monotonic() - t0, 1), "software": software_ or software()}
    append_jsonl(lib.admin_dir / VERIFY_LOG, row)
    from imagery_library.permissions import apply_quietly

    apply_quietly(lib, [lib.admin_dir])
    log(f"  {rel}: {'OK' if not problems else f'{len(problems)} problem(s)'} "
        f"({files} files, {'sha256' if checksums else 'sizes'}, {row['seconds']} s)")
    return row
