"""The library catalog: generated from the tile datasets, never edited.

    catalog/current -> <build-id>/        swapped atomically when a build finishes
    catalog/<build-id>/tiles.parquet       GeoParquet, one row per tile dataset (footprint in EPSG:4326)
    catalog/<build-id>/observations.parquet  one row per acquired observation
    catalog/<build-id>/catalog.json        build summary

Tile manifests and items.jsonl are the source of truth; the catalog can be
rebuilt from them at any time and reads no imagery. Readers open
`catalog/current`, so a build in progress never shows them a mix of old
and new files. Verification results come from admin/verification.jsonl,
written by `verify_tiles`.
"""
from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from imagery_library.grids import footprint_lonlat
from imagery_library.layout import (LINEAGE_FILE, Library, _resolved_request, append_jsonl, atomic_write, fsync_dir,
                                    software, spec_hash, spec_identity)

CURRENT = "current"
KEEP_BUILDS = 3
VERIFY_LOG = "verification.jsonl"


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def run_in_progress(tile_dir: Path) -> bool:
    """True if a DataLoader run holds this tile's lock right now (read-only probe)."""
    import fcntl

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


def _latest_verification(lib: Library) -> dict:
    out = {}
    p = lib.admin_dir / VERIFY_LOG
    if p.exists():
        for line in p.read_text().splitlines():
            if line.strip():
                r = json.loads(line)
                out[r["path"]] = r  # later lines win
    return out


def _tile_records(lib: Library, product, system: str, tile: str, path: Path, verified: dict):
    from data_loader.dataset import open_dataset

    ds = open_dataset(path)
    m = ds.manifest
    rel = str(path.relative_to(lib.root))
    lineage = json.loads((path / LINEAGE_FILE).read_text()) if (path / LINEAGE_FILE).exists() else None
    rows = list(ds.items(status=None))
    counts = {"acquired": 0, "failed": 0, "pending": 0, "filtered": 0}
    for r in rows:
        counts[r.get("status", "pending")] = counts.get(r.get("status", "pending"), 0) + 1
    acquired = [r for r in rows if r.get("status") == "acquired"]
    sensor, grid = next((s, g[tile]) for s, g in m["grids"].items() if tile in g)
    v = verified.get(rel)
    commits = sorted({(r.get("software") or {}).get("gitCommit") or "unknown" for r in acquired})
    request = _resolved_request(m.get("request", {}), m.get("products"))
    shash = spec_hash(spec_identity(request))
    tile_row = {
        "product": product.name, "version": product.version, "grid_system": system, "tile": tile, "path": rel,
        "geometry": footprint_lonlat(grid),
        "crs_wkt": grid.get("crsWkt"), "transform": list(grid.get("transform", [])[:6]),
        "width": grid.get("width"), "height": grid.get("height"),
        "observations": counts["acquired"], "failed": counts["failed"], "pending": counts["pending"],
        "filtered": counts["filtered"],
        "first_date": min((r["date"] for r in acquired if r.get("date")), default=None),
        "last_date": max((r["date"] for r in acquired if r.get("date")), default=None),
        "bytes": sum(f.get("bytes", 0) for r in acquired for f in r.get("files", [])),
        "complete": bool(m.get("dataset", {}).get("complete")) and counts["failed"] == 0 and counts["pending"] == 0,
        "open_ended": bool(m.get("coverage", {}).get("openEnded")),
        "discovered_through": m.get("coverage", {}).get("discoveredThrough"),
        "updated": m.get("dataset", {}).get("updated"),
        "run_in_progress": run_in_progress(path),
        "spec_hash": shash, "spec_matches_product": shash == product.spec_hash(tile, system),
        "dataloader_commits": ",".join(commits),
        "source_verified": sum(1 for r in acquired
                               if (r.get("sourceIntegrity") or {}).get("files") == (r.get("sourceIntegrity") or {}).get("of")
                               and (r.get("sourceIntegrity") or {}).get("of")),
        "imagery_present": True if lineage is None else bool(lineage.get("imageryPresent", True)),
        "imported_from": None if lineage is None else lineage["source"]["path"],
        "last_verified": v.get("time") if v else None,
        "verify_ok": v.get("ok") if v else None,
        "verify_checksums": v.get("checksums") if v else None,
    }
    obs = []
    for r in acquired:
        prov = r.get("provenance") or {}
        f = next((f for f in r.get("files", []) if f.get("bandSet") == "bands"), (r.get("files") or [{}])[0])
        obs.append({
            "product": product.name, "version": product.version, "grid_system": system, "tile": tile,
            "key": r["key"], "item_id": r.get("itemId"), "sensor": r.get("sensor"), "platform": r.get("platform"),
            "date": r.get("date"), "datetime": r.get("datetime"), "season_year": r.get("seasonYear"),
            "cloud_percent": r.get("eo:cloud_cover"), "fill_percent": r.get("fillPercent"),
            "valid_fraction": r.get("validFraction"),
            "upstream_product_id": prov.get("upstream_product_id"), "collection": prov.get("collection"),
            "processing_date": prov.get("generation_time"), "processing_baseline": prov.get("processing_baseline"),
            "path": f"{rel}/{f['path']}" if f.get("path") else None, "bytes": f.get("bytes"), "sha256": f.get("sha256"),
            "files": len(r.get("files", [])),
            "source_verified": bool((r.get("sourceIntegrity") or {}).get("of"))
            and (r.get("sourceIntegrity") or {}).get("files") == (r.get("sourceIntegrity") or {}).get("of"),
            "dataloader_commit": (r.get("software") or {}).get("gitCommit"),
        })
    return tile_row, obs


def build_catalog(lib: Library, *, log=print) -> Path:
    import geopandas as gpd
    import pandas as pd

    verified = _latest_verification(lib)
    tiles, obs, problems = [], [], []
    for product, system, tile, path in lib.tiles():
        try:
            t, o = _tile_records(lib, product, system, tile, path, verified)
        except Exception as e:  # one unreadable tile must not hide the others
            problems.append(f"{path.relative_to(lib.root)}: {e}")
            continue
        if not t["spec_matches_product"]:
            problems.append(f"{t['path']}: spec hash {t['spec_hash']} does not match {product.key}; left out")
            continue
        tiles.append(t)
        obs.extend(o)
    build_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    lib.catalog_dir.mkdir(exist_ok=True)
    out = lib.catalog_dir / build_id
    out.mkdir()
    cols = ["product", "version", "grid_system", "tile", "path", "geometry"]
    tdf = gpd.GeoDataFrame(tiles, geometry="geometry", crs="EPSG:4326") if tiles else \
        gpd.GeoDataFrame({c: [] for c in cols}, geometry="geometry", crs="EPSG:4326")
    tdf.to_parquet(out / "tiles.parquet", index=False)
    odf = pd.DataFrame(obs)
    if not obs:
        odf = pd.DataFrame({c: [] for c in ("product", "version", "grid_system", "tile", "date", "path")})
    odf.to_parquet(out / "observations.parquet", index=False)
    summary = {
        "library": lib.name, "built": _now(), "build": build_id, "builder": software(),
        "tiles": len(tiles), "observations": len(obs), "bytes": int(sum(t["bytes"] for t in tiles)),
        "products": sorted({f"{t['product']}/{t['version']}" for t in tiles}),
        "problems": problems,
    }
    atomic_write(out / "catalog.json", json.dumps(summary, indent=2) + "\n")
    fsync_dir(out)
    link = lib.catalog_dir / CURRENT
    tmp = lib.catalog_dir / f".{CURRENT}.{os.getpid()}"
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    os.symlink(build_id, tmp)
    os.replace(tmp, link)
    fsync_dir(lib.catalog_dir)
    builds = sorted(p for p in lib.catalog_dir.iterdir() if p.is_dir() and not p.is_symlink() and p.name[:1].isdigit())
    for old in builds[:-KEEP_BUILDS]:
        shutil.rmtree(old, ignore_errors=True)
    log(f"catalog {build_id}: {len(tiles)} tiles, {len(obs)} observations, "
        f"{summary['bytes'] / 1e12:.3f} TB" + (f"; {len(problems)} problem(s)" if problems else ""))
    for p in problems[:20]:
        log(f"  problem: {p}")
    return out


def current_catalog(lib: Library) -> Path:
    link = lib.catalog_dir / CURRENT
    if not link.exists():
        from imagery_library.layout import LibraryError

        raise LibraryError(f"{lib.root}: no catalog yet (maintainers: `imagery-library catalog {lib.root}`)")
    return link.resolve()


def verify_tiles(lib: Library, *, product: Optional[str] = None, tiles: Optional[list] = None,
                 checksums: bool = True, log=print) -> list[dict]:
    """Run DataLoader's verify on each tile dataset and log the result."""
    from data_loader.dataset import open_dataset

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
        t0 = datetime.now(timezone.utc)
        ds = open_dataset(path)
        problems = ds.verify(checksums=checksums)
        files = sum(1 for _ in ds.files(band_set=None))
        row = {"time": _now(), "path": rel, "product": p.key, "tile": tile, "ok": not problems,
               "checksums": checksums, "files": files, "problems": len(problems), "examples": problems[:10],
               "seconds": round((datetime.now(timezone.utc) - t0).total_seconds(), 1), "software": sw}
        append_jsonl(lib.admin_dir / VERIFY_LOG, row)
        results.append(row)
        log(f"  {rel}: {'OK' if not problems else f'{len(problems)} problem(s)'} "
            f"({files} files, {'sha256' if checksums else 'sizes'}, {row['seconds']} s)")
    return results
