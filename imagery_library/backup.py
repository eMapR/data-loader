"""What to back up, by tier (docs/library-backup-policy.md), as lists of
paths relative to the library root, for `rsync --files-from` or any
backup tool. Reads only metadata.

    metadata        must: specs, every tile's manifest/items/request/lineage and USGS
                    source records, admin/ (logs, verify records), the current catalog
    irreplaceable   should: files of acquired observations the source has since replaced
                    with a newer processing version (`supersededBy`): USGS stops
                    distributing the old version, so these cannot be downloaded again
    imagery         optional: every other acquired file (re-downloadable, at a cost)
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterator

from imagery_library.catalog import CURRENT, tile_rows
from imagery_library.layout import (ADMIN_DIR, LIBRARY_FILE, LINEAGE_FILE, PRODUCT_FILE, README_FILE, Library,
                                    LibraryError)

TIERS = ("metadata", "irreplaceable", "imagery")
_TILE_META = ("manifest.json", "items.jsonl", "request.yaml", LINEAGE_FILE)


def _files_under(root: Path, d: Path, skip=("locks",)) -> Iterator[str]:
    for p in sorted(d.rglob("*")):
        if p.is_file() and not p.is_symlink() and not any(s in p.relative_to(d).parts for s in skip):
            yield str(p.relative_to(root))


def backup_list(lib: Library, tier: str) -> Iterator[str]:
    import json

    if tier not in TIERS:
        raise LibraryError(f"tier {tier!r}: expected one of {', '.join(TIERS)}")
    root = lib.root
    if tier == "metadata":
        yield LIBRARY_FILE
        if (root / README_FILE).exists():
            yield README_FILE
        for p in lib.products():
            yield str((p.root / PRODUCT_FILE).relative_to(root))
            if p.published_tiles_file:
                yield str((p.root / p.published_tiles_file).relative_to(root))
        cur = lib.catalog_dir / CURRENT
        if cur.exists():
            for f in sorted(cur.resolve().iterdir()):
                yield str(f.relative_to(root.resolve()))
        yield from _files_under(root, root / ADMIN_DIR)
    for p, system, tile, path in lib.tiles():
        rel = path.relative_to(root)
        if tier == "metadata":
            for n in _TILE_META:
                if (path / n).exists():
                    yield f"{rel}/{n}"
            if (path / "metadata").is_dir():
                yield from _files_under(root, path / "metadata")
            continue
        m = json.loads((path / "manifest.json").read_text())
        for r in tile_rows(path, m):
            if r.get("status") != "acquired":
                continue
            retired = bool(r.get("supersededBy"))
            if (tier == "irreplaceable") != retired:
                continue
            for f in r.get("files", []):
                yield f"{rel}/{f['path']}"
            if tier == "irreplaceable" and r.get("sourceMetadata"):
                yield f"{rel}/{r['sourceMetadata']}"
