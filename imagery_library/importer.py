"""Bring an existing (project-named, multi-tile) DataLoader archive into the
library: one new tile dataset per tile, the source left exactly as it was.

For each tile the importer writes a new manifest.json / items.jsonl /
request.yaml describing only that tile (item rows are copied verbatim) and
a LINEAGE.json saying where it came from. Imagery and source metadata are
hard-linked (same filesystem: no copy, no extra space), copied, or left
out entirely (`metadata` mode, for dry runs against production metadata).
Each tile is assembled in the maintainer's staging area (staging/<user>/,
same filesystem) and renamed into place only when complete, so the catalog
never sees half a tile. Hard-linked files keep the source's permissions:
the library's permission policy skips files that still have another name.

The source is only read: its manifest and items are hashed before and
after, and every file it lists is stat-checked before anything is linked.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

from imagery_library.grids import grid_system_of
from imagery_library.layout import (LINEAGE_FILE, Library, LibraryError, Product, _resolved_request, append_jsonl,
                                    atomic_write, fsync_dir, software, spec_hash, spec_identity)

MODES = ("hardlink", "copy", "metadata")
STATUSES = ("acquired", "failed", "pending", "filtered")


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _rows(path: Path) -> list[dict]:
    out = []
    with open(path) as f:
        for line in f:
            if line.strip():
                out.append(json.loads(line))
    return out


@dataclass
class TilePlan:
    grid_system: str
    tile: str
    dest: Path
    rows: list
    files: list            # (relative path, bytes) of imagery + source metadata to bring along
    action: str            # "import" | "skip (already imported)" | "refuse: ..."
    problems: list = field(default_factory=list)

    @property
    def counts(self) -> dict:
        c = {s: 0 for s in STATUSES}
        for r in self.rows:
            c[r.get("status", "pending")] = c.get(r.get("status", "pending"), 0) + 1
        return c

    @property
    def bytes(self) -> int:
        return sum(b for _, b in self.files)


@dataclass
class ImportResult:
    source: Path
    product: str
    mode: str
    dry_run: bool
    tiles: list
    source_unchanged: Optional[bool] = None


def _source_fingerprint(src: Path) -> dict:
    return {"manifest.json": _sha256(src / "manifest.json"), "items.jsonl": _sha256(src / "items.jsonl")}


def plan_import(lib: Library, product: Product, src, *, tiles: Optional[list] = None,
                check_files: bool = True) -> tuple[dict, list[TilePlan]]:
    """Read-only: what importing `src` would do, tile by tile."""
    src = Path(src)
    mpath = src / "manifest.json"
    if not mpath.exists():
        raise LibraryError(f"{src}: no manifest.json -- not a DataLoader dataset")
    m = json.loads(mpath.read_text())
    if m.get("schema") != "dataloader-manifest" or str(m.get("schemaVersion", "")).split(".")[0] != "1":
        raise LibraryError(f"{src}: manifest is {m.get('schema')} {m.get('schemaVersion')}; "
                           "the importer reads dataloader-manifest 1.x (run `data-loader upgrade` first)")
    if (src / ".state" / "journal.jsonl").exists() and (src / ".state" / "journal.jsonl").stat().st_size:
        raise LibraryError(f"{src}: has an unfinished run (non-empty .state/journal.jsonl); "
                           "let it finish or re-run it before importing")
    request = _resolved_request(m.get("request", {}), m.get("products"))
    grids = m.get("grids", {})
    tile_ids = sorted({t for g in grids.values() for t in g})
    for t in tile_ids:
        kinds = {g[t].get("kind") for g in grids.values() if t in g}
        if kinds != {"tile"}:
            raise LibraryError(f"{src}: grid {t!r} is not a native tile ({kinds}); only tile archives "
                               "can join the library")
    if tiles:
        unknown = sorted(set(tiles) - set(tile_ids))
        if unknown:
            raise LibraryError(f"{src}: does not hold tile(s) {', '.join(unknown)}")
        tile_ids = [t for t in tile_ids if t in set(tiles)]

    rows_by_tile: dict[str, list] = {t: [] for t in tile_ids}
    for r in _rows(src / m.get("items", "items.jsonl")):
        if r.get("grid") in rows_by_tile:
            rows_by_tile[r["grid"]].append(r)

    src_ident = spec_identity(request)
    plans = []
    for t in tile_ids:
        system, canonical = grid_system_of(t, product.grid_systems)
        if canonical != t:
            raise LibraryError(f"{src}: tile {t!r} is not in canonical form ({canonical!r}); upgrade the archive")
        want = product.spec_identity(t, system)
        dest = product.tile_path(system, t)
        rows = rows_by_tile[t]
        problems = []
        if want != src_ident:
            diff = sorted(k for k in set(want) | set(src_ident) if want.get(k) != src_ident.get(k))
            problems.append(f"product spec differs from {product.key} in: {', '.join(diff)}")
        files = []
        for r in rows:
            if r.get("status") != "acquired":
                continue
            for f in r.get("files", []):
                files.append((f["path"], f.get("bytes")))
            meta = r.get("sourceMetadata")
            if meta:
                files.append((meta, None))
        if check_files:
            for rel, size in files:
                p = src / rel
                try:
                    st = p.stat()
                except FileNotFoundError:
                    problems.append(f"missing in source: {rel}")
                    continue
                if size is not None and st.st_size != size:
                    problems.append(f"size differs in source: {rel} ({st.st_size} != {size})")
        files = [(rel, size if size is not None else (src / rel).stat().st_size if (src / rel).exists() else 0)
                 for rel, size in files]
        if problems:
            action = "refuse"
        elif (dest / "manifest.json").exists():
            have = {r["key"] for r in _rows(dest / "items.jsonl") if r.get("status") == "acquired"}
            need = {r["key"] for r in rows if r.get("status") == "acquired"}
            if need <= have:
                action = "skip (already in library)"
            else:
                action = "refuse"
                problems.append(f"{dest} already exists and lacks {len(need - have)} of this archive's "
                                "observations; merging tile datasets is not supported")
        else:
            action = "import"
        plans.append(TilePlan(system, t, dest, rows, files, action, problems))
    return m, plans


def _tile_manifest(m: dict, tile: str, rows: list, dest: Path,
                   source_unfinished_elsewhere: bool = False) -> tuple[dict, dict]:
    """The source manifest narrowed to one tile (same contract, same schema).
    A tile is complete when it has no failed/pending rows and the source was
    complete, or the source was incomplete only because of other tiles'
    failed/pending rows (its own completeness must not be inherited)."""
    out = copy.deepcopy(m)
    req = out.get("request", {})
    req.setdefault("aoi", {})["tiles"] = [tile]
    req.setdefault("output", {})["dir"] = str(dest)
    out["grids"] = {s: {tile: g[tile]} for s, g in m.get("grids", {}).items() if tile in g}
    counts = {s: 0 for s in STATUSES}
    for r in rows:
        counts[r.get("status", "pending")] = counts.get(r.get("status", "pending"), 0) + 1
    acquired = [r for r in rows if r.get("status") == "acquired"]
    dates = sorted(r.get("date") or r.get("windowStart") for r in acquired if r.get("date") or r.get("windowStart"))
    cov = out.setdefault("coverage", {})
    cov.update({"counts": counts, "firstAcquired": dates[0] if dates else None,
                "lastAcquired": dates[-1] if dates else None,
                "bytes": sum(f.get("bytes", 0) for r in acquired for f in r.get("files", []))})
    out["dataset"]["complete"] = counts["pending"] == 0 and counts["failed"] == 0 and (
        m["dataset"].get("complete", False) or source_unfinished_elsewhere)
    out["items"] = "items.jsonl"
    return out, req


def _place(src: Path, dst: Path, mode: str) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if mode == "hardlink":
        try:
            os.link(src, dst)
        except OSError as e:
            if e.errno == 18:  # EXDEV
                raise LibraryError(f"cannot hard-link across filesystems ({src} -> {dst}); use --mode copy") from None
            raise
    else:
        shutil.copy2(src, dst)


def import_archive(lib: Library, product: Product, src, *, mode: str = "hardlink", tiles: Optional[list] = None,
                   dry_run: bool = False, log=print) -> ImportResult:
    if mode not in MODES:
        raise LibraryError(f"mode {mode!r}: expected one of {', '.join(MODES)}")
    src = Path(src).resolve()
    before = _source_fingerprint(src)
    m, plans = plan_import(lib, product, src, tiles=tiles, check_files=True)
    result = ImportResult(src, product.key, mode, dry_run, plans)
    if dry_run:
        result.source_unchanged = _source_fingerprint(src) == before
        return result
    sw = software()
    all_rows = [r for tp in plans for r in tp.rows]  # (plans cover every tile unless --tile narrowed them)
    unfinished_elsewhere = tiles is None and any(r.get("status") in ("failed", "pending") for r in all_rows)
    for tp in plans:
        if tp.action != "import":
            log(f"  {tp.tile}: {tp.action}" + (f" -- {tp.problems[0]}" if tp.problems else ""))
            continue
        stage = lib.staging_dir() / f".import-{product.version}-{tp.grid_system}-{tp.tile}-{os.getpid()}"
        if stage.exists():
            shutil.rmtree(stage)
        stage.mkdir(parents=True)
        try:
            if mode != "metadata":
                for rel, _ in tp.files:
                    _place(src / rel, stage / rel, mode)
            manifest, request = _tile_manifest(m, tp.tile, tp.rows, tp.dest,
                                               source_unfinished_elsewhere=unfinished_elsewhere)
            atomic_write(stage / "items.jsonl", "".join(json.dumps(r, default=str) + "\n" for r in tp.rows))
            atomic_write(stage / "manifest.json", json.dumps(manifest, indent=2, default=str) + "\n")
            atomic_write(stage / "request.yaml", yaml.safe_dump(request, sort_keys=False))
            lineage = {
                "importedAt": _now(), "importer": sw, "mode": mode,
                "imageryPresent": mode != "metadata",
                "product": product.key, "gridSystem": tp.grid_system, "tile": tp.tile,
                "specHash": spec_hash(spec_identity(_resolved_request(request, m.get("products")))),
                "source": {"path": str(src), "fingerprint": before,
                           "tiles": sorted({t for g in m.get("grids", {}).values() for t in g}),
                           "request": m.get("request"), "dataset": m.get("dataset")},
                "rows": len(tp.rows), "counts": tp.counts, "files": len(tp.files), "bytes": tp.bytes,
                "note": ("rows copied verbatim from the source items.jsonl; manifest narrowed to this tile. "
                         "dataset.runs lists the runs that wrote the source archive (all of its tiles)."),
            }
            atomic_write(stage / LINEAGE_FILE, json.dumps(lineage, indent=2, default=str) + "\n")
            fsync_dir(stage)
            if tp.dest.exists():
                raise LibraryError(f"{tp.dest} appeared during the import; not replacing it")
            tp.dest.parent.mkdir(parents=True, exist_ok=True)
            os.rename(stage, tp.dest)
            fsync_dir(tp.dest.parent)
        except BaseException:
            shutil.rmtree(stage, ignore_errors=True)
            raise
        tp.action = "imported"
        from imagery_library.permissions import apply_quietly

        apply_quietly(lib, [tp.dest])
        append_jsonl(lib.admin_dir / "imports.jsonl", {k: v for k, v in lineage.items() if k != "source"}
                     | {"source": str(src), "dest": str(tp.dest.relative_to(lib.root))})
        apply_quietly(lib, [lib.admin_dir])
        log(f"  {tp.tile}: imported {tp.counts['acquired']} observations, {len(tp.files)} files "
            f"({tp.bytes / 1e9:.1f} GB, {mode}) -> {tp.dest.relative_to(lib.root)}")
    result.source_unchanged = _source_fingerprint(src) == before
    if not result.source_unchanged:  # pragma: no cover - would mean something else wrote the source
        raise LibraryError(f"{src}: manifest/items changed during the import; check what else is writing it")
    return result
