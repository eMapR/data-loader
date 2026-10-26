"""Command line: `imagery-library <command> ...` (or `python -m imagery_library ...`).

Researchers (read-only):
    imagery-library query ROOT QUERY.yaml       imagery for an AOI + date range -> summary (+ CSV of files)
    imagery-library tiles ROOT                  what the library holds, per tile

Maintainers:
    imagery-library init ROOT --name ...        create an empty library
    imagery-library add-product ROOT SPEC.yaml  install a product spec (PRODUCT.yaml)
    imagery-library import ROOT ARCHIVE         split a project archive into library tiles (source untouched)
    imagery-library acquire-config ROOT ...     DataLoader configs for tiles an AOI needs and the library lacks
    imagery-library acquire ROOT ...            run those tiles one at a time, queued per source account
    imagery-library verify ROOT                 DataLoader verify on every tile, logged for the catalog
    imagery-library catalog ROOT                rebuild the catalog
    imagery-library permissions ROOT [--fix]    check (or apply) the permission policy
    imagery-library backup-list ROOT --tier T   paths to back up: metadata | irreplaceable | imagery

Exit codes: 0 ok; 2 invalid input; 4 verify/import found problems.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

from imagery_library.layout import LibraryError, add_product, init_library, open_library

_QUERY_KEYS = {"aoi", "start", "end", "product", "version", "platforms", "max_cloud_percent",
               "min_valid_fraction", "months", "verified_only"}


def cmd_init(a) -> int:
    lib = init_library(a.root, name=a.name, description=a.description, maintainers=a.maintainer or [],
                       contact=a.contact, group=a.group)
    print(f"created library {lib.name!r} at {lib.root}")
    return 0


def cmd_add_product(a) -> int:
    p = add_product(open_library(a.root), a.spec)
    print(f"product {p.key}: {p.title} (grids: {', '.join(p.grid_systems)})")
    return 0


def cmd_import(a) -> int:
    from imagery_library.importer import import_archive

    lib = open_library(a.root)
    product = lib.product(a.product, a.version)
    r = import_archive(lib, product, a.archive, mode=a.mode, tiles=a.tile, dry_run=a.dry_run, log=lambda m: None)
    print(f"{'DRY RUN: ' if a.dry_run else ''}{r.source} -> {product.key} ({r.mode})")
    for tp in r.tiles:
        c = tp.counts
        print(f"  {tp.grid_system}/{tp.tile:<12} {tp.action:<26} acquired {c['acquired']:>6}  failed {c['failed']:>3}  "
              f"pending {c['pending']:>3}  files {len(tp.files):>6}  {tp.bytes / 1e9:8.1f} GB")
        for p in tp.problems[:5]:
            print(f"      {p}")
    print(f"  source manifest/items unchanged: {r.source_unchanged}")
    return 4 if any(tp.problems for tp in r.tiles) else 0


def cmd_acquire_config(a) -> int:
    from imagery_library.acquire import tiles_needed, write_configs

    lib = open_library(a.root)
    product = lib.product(a.product, a.version)
    needed = tiles_needed(lib, product, aoi=a.aoi, tiles=a.tile)
    for r in needed:
        share = f"{r['aoi_share']:6.1%}" if r["aoi_share"] is not None else "     -"
        print(f"  {r['grid_system']}/{r['tile']:<12} {share}  {'held (update only)' if r['held'] else 'NEW'}")
    if a.out_dir:
        written = write_configs(lib, product, needed, a.out_dir, include_held=a.include_held)
        print(f"wrote {len(written)} config(s) to {a.out_dir}")
    return 0


def cmd_acquire(a) -> int:
    from imagery_library.acquire import acquire, tiles_needed

    lib = open_library(a.root)
    product = lib.product(a.product, a.version)
    needed = tiles_needed(lib, product, aoi=a.aoi, tiles=a.tile)
    todo = [r for r in needed if a.include_held or not r["held"]]
    print(f"{product.key}: {len(needed)} tile(s) needed, {len(todo)} to run"
          + ("" if a.include_held else f" ({len(needed) - len(todo)} already held; --include-held updates them)"))
    rows = acquire(lib, product, needed, include_held=a.include_held, wait=not a.no_wait)
    return 0 if all(r.get("exit") == 0 for r in rows) else 3


def cmd_verify(a) -> int:
    from imagery_library.catalog import verify_tiles

    rows = verify_tiles(open_library(a.root), product=a.product, tiles=a.tile, checksums=not a.no_checksums)
    return 4 if any(r["ok"] is False for r in rows) else 0


def cmd_permissions(a) -> int:
    from imagery_library.permissions import Policy, apply

    lib = open_library(a.root)
    pol = Policy.from_library(lib)
    rep = apply(lib, [Path(t) for t in a.path] if a.path else None, fix=a.fix, policy=pol)
    modes = ", ".join(f"{k} {v:04o}" for k, v in pol.modes.items())
    print(f"policy: group {pol.group or '(unchanged)'}; {modes}")
    if pol.group is None:
        print("warning: no permissions.group in LIBRARY.yaml: files keep whatever group they were created with, "
              "and setgid directories may not be settable; set it to the maintainers' group")
    print(f"checked {rep.checked} paths; {'changed ' + str(rep.changed) if a.fix else 'differ: ' + str(len(rep.problems))}"
          + (f"; {rep.acl} carry POSIX ACLs (the mode sets their mask)" if rep.acl else ""))
    for p in [p for p in rep.problems if p][:20]:
        print(f"  {p}")
    return 4 if rep.problems else 0


def cmd_backup_list(a) -> int:
    from imagery_library.backup import backup_list

    lib = open_library(a.root)
    n = 0
    out = open(a.out, "w") if a.out else sys.stdout
    try:
        for rel in backup_list(lib, a.tier):
            out.write(rel + "\n")
            n += 1
    finally:
        if a.out:
            out.close()
    if a.out:
        print(f"{n} paths ({a.tier}) -> {a.out}; e.g. rsync -a --files-from={a.out} {lib.root}/ DEST/")
    return 0


def cmd_catalog(a) -> int:
    from imagery_library.catalog import build_catalog

    build_catalog(open_library(a.root))
    return 0


def cmd_tiles(a) -> int:
    import geopandas as gpd

    from imagery_library.catalog import current_catalog

    lib = open_library(a.root)
    t = gpd.read_parquet(current_catalog(lib) / "tiles.parquet")
    print(f"{'product':<26} {'grid/tile':<28} {'obs':>7} {'verified':>8} {'first':>11} {'last':>11} {'TB':>7}  "
          f"{'acquisition':<11} verification")
    for r in t.sort_values(["product", "version", "grid_system", "tile"]).itertuples():
        acq = r.acquisition_state + (f" ({r.failed} failed)" if r.failed else "")
        ver = r.verification_state + (f" {str(r.last_verified)[:10]}" if r.last_verified else "")
        print(f"{r.product + '/' + r.version:<26} {r.grid_system + '/' + r.tile:<28} {r.observations:>7} "
              f"{r.verified_observations:>8} {str(r.first_date):>11} {str(r.last_date):>11} {r.bytes / 1e12:7.3f}  "
              f"{acq:<11} {ver}" + ("" if r.imagery_present else "  (metadata only)"))
    return 0


def cmd_query(a) -> int:
    lib = open_library(a.root)
    q = yaml.safe_load(Path(a.query).read_text()) or {}
    extra = sorted(set(q) - _QUERY_KEYS)
    if extra:
        raise LibraryError(f"{a.query}: unknown key(s) {', '.join(extra)} (allowed: {', '.join(sorted(_QUERY_KEYS))})")
    if "aoi" not in q:
        raise LibraryError(f"{a.query}: 'aoi' (a vector file) is required")
    aoi = Path(q.pop("aoi"))
    if not aoi.is_absolute():
        aoi = Path(a.query).parent / aoi
    r = lib.query(str(aoi), **q)
    print(r.summary())
    if a.out:
        cols = ["date", "product", "version", "grid_system", "tile", "platform", "item_id", "cloud_percent",
                "valid_fraction", "processing_date", "path", "bytes", "sha256"]
        df = r.observations[cols].copy()
        df["path"] = [str(p) for p in r.paths()]
        df.to_csv(a.out, index=False)
        print(f"wrote {len(df)} rows to {a.out}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="imagery-library", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)
    sp = sub.add_parser("init", help="create an empty library")
    sp.add_argument("root")
    sp.add_argument("--name", required=True)
    sp.add_argument("--description", required=True)
    sp.add_argument("--maintainer", action="append", help="repeat for each maintainer")
    sp.add_argument("--contact")
    sp.add_argument("--group", help="maintainers' group: owns everything (permissions.group)")
    sp.set_defaults(fn=cmd_init)
    sp = sub.add_parser("add-product", help="install a PRODUCT.yaml")
    sp.add_argument("root")
    sp.add_argument("spec")
    sp.set_defaults(fn=cmd_add_product)
    sp = sub.add_parser("import", help="split an existing archive into library tiles")
    sp.add_argument("root")
    sp.add_argument("archive")
    sp.add_argument("--product")
    sp.add_argument("--version")
    sp.add_argument("--mode", default="hardlink", choices=["hardlink", "copy", "metadata"])
    sp.add_argument("--tile", action="append", help="only these tiles (repeat)")
    sp.add_argument("--dry-run", action="store_true", help="check and report; write nothing")
    sp.set_defaults(fn=cmd_import)
    sp = sub.add_parser("acquire-config", help="per-tile DataLoader configs for an AOI or tiles")
    sp.add_argument("root")
    sp.add_argument("--product")
    sp.add_argument("--version")
    sp.add_argument("--aoi", help="vector file")
    sp.add_argument("--tile", action="append")
    sp.add_argument("--out-dir", help="write configs here (default: just report)")
    sp.add_argument("--include-held", action="store_true", help="also write configs for held tiles (updates)")
    sp.set_defaults(fn=cmd_acquire_config)
    sp = sub.add_parser("acquire", help="run DataLoader for an AOI's or the listed tiles, one at a time")
    sp.add_argument("root")
    sp.add_argument("--product")
    sp.add_argument("--version")
    sp.add_argument("--aoi", help="vector file")
    sp.add_argument("--tile", action="append")
    sp.add_argument("--include-held", action="store_true", help="also update tiles the library holds")
    sp.add_argument("--no-wait", action="store_true", help="skip tiles instead of queueing if the account is busy")
    sp.set_defaults(fn=cmd_acquire)
    sp = sub.add_parser("verify", help="verify tile datasets (logged for the catalog)")
    sp.add_argument("root")
    sp.add_argument("--product")
    sp.add_argument("--tile", action="append")
    sp.add_argument("--no-checksums", action="store_true")
    sp.set_defaults(fn=cmd_verify)
    sp = sub.add_parser("permissions", help="check (or with --fix apply) the permission policy")
    sp.add_argument("root")
    sp.add_argument("--fix", action="store_true")
    sp.add_argument("--path", action="append", help="only below these paths (repeat)")
    sp.set_defaults(fn=cmd_permissions)
    sp = sub.add_parser("backup-list", help="paths to back up, by tier")
    sp.add_argument("root")
    sp.add_argument("--tier", required=True, choices=["metadata", "irreplaceable", "imagery"])
    sp.add_argument("--out", help="write the list here (default: stdout)")
    sp.set_defaults(fn=cmd_backup_list)
    sp = sub.add_parser("catalog", help="rebuild the catalog")
    sp.add_argument("root")
    sp.set_defaults(fn=cmd_catalog)
    sp = sub.add_parser("tiles", help="list tiles in the catalog")
    sp.add_argument("root")
    sp.set_defaults(fn=cmd_tiles)
    sp = sub.add_parser("query", help="imagery for an AOI and date range")
    sp.add_argument("root")
    sp.add_argument("query", help="YAML: aoi, start, end, [product, version, platforms, max_cloud_percent, "
                                  "min_valid_fraction, months]")
    sp.add_argument("--out", help="CSV of matching observations and file paths")
    sp.set_defaults(fn=cmd_query)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    try:
        return args.fn(args)
    except LibraryError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
