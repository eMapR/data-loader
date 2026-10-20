"""Command line: `data-loader <command> ...` (or `python -m data_loader ...`).

    data-loader validate CONFIG     check the config (no network)
    data-loader plan CONFIG         discovery only: what exists, what's already acquired
    data-loader run CONFIG          acquire; re-run the same command to resume or update
    data-loader status DIR          progress of a dataset (no network)
    data-loader verify DIR          check every file against the manifest (size + sha256)
    data-loader upgrade DIR         bring an older dataset's manifest/items to the current schema

Exit codes: 0 ok; 1 unexpected error; 2 invalid config/request;
3 run finished but some acquisitions are failed or still pending
(re-run to retry); 4 verify (or upgrade --check-files) found problems;
5 run stopped itself (disk full, credentials rejected, source down for
hours) -- fix the cause and re-run; 130 interrupted.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime

from data_loader._version import __version__
from data_loader.config import ConfigError, load_config
from data_loader.dataset import DatasetError, open_dataset
from data_loader.engine import RunStopped


def _log(msg: str) -> None:
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def _load(args):
    return load_config(args.config, output_dir=args.output_dir)


def cmd_validate(args) -> int:
    from data_loader.engine import check_request, make_provider

    cfg = _load(args)
    provider = make_provider(cfg)
    warnings = check_request(cfg, provider)
    windows = cfg.time.windows()
    print(f"OK: {args.config}")
    print(f"  provider {cfg.provider}, sensors {', '.join(s.name for s in cfg.sensors)}, "
          f"temporal_mode {cfg.temporal_mode}, encoding {cfg.output.encoding}")
    if cfg.aoi.tiles:
        print(f"  aoi: {len(cfg.aoi.tiles)} tile(s)")
        for t in cfg.aoi.tiles:  # region + where it is, so a tile on the wrong grid shows here
            d = provider.tile_grid.describe(t)
            w, s_, e, n = d["bboxLonLat"]
            print(f"    {d['id']:<12} {d['system']:<16} lon {w:8.2f}..{e:8.2f}  lat {s_:6.2f}..{n:6.2f}")
    else:
        print(f"  aoi: bbox {cfg.aoi.upper_left} -> {cfg.aoi.lower_right}")
    if windows:
        print(f"  time: {len(windows)} window(s) {windows[0].start}..{windows[-1].end}"
              + (" (open-ended: re-running later adds newer imagery)" if cfg.time.open_ended else ""))
    print(f"  output: {cfg.output.dir}")
    for w in warnings:
        print(f"  warning: {w}")
    return 0


def cmd_plan(args) -> int:
    from data_loader.engine import plan_summary

    s = plan_summary(_load(args), log=_log)
    print(f"\n{s['temporalMode']} units over {s['span'][0]}..{s['span'][1]} ({s['windows']} window(s)):")
    print(f"  {'sensor':10} {'grid':12} {'units':>7} {'acquired':>9} {'to do':>7} {'filtered':>9}")
    for r in s["grids"]:
        print(f"  {r['sensor']:10} {r['grid']:12} {r['units']:7d} {r['acquired']:9d} "
              f"{r['units'] - r['acquired']:7d} {r['filtered']:9d}")
    tot = sum(r["units"] for r in s["grids"])
    done = sum(r["acquired"] for r in s["grids"])
    print(f"  {'total':23} {tot:7d} {done:9d} {tot - done:7d}")
    for w in s["warnings"]:
        print(f"warning: {w}")
    return 0


def cmd_run(args) -> int:
    from data_loader.engine import run

    cfg = _load(args)
    if args.workers is not None:
        from dataclasses import replace

        cfg = replace(cfg, workers=args.workers)
    summary = run(cfg, log=_log)
    print(f"\nmanifest: {summary.manifest}")
    if not summary.complete:
        print("some acquisitions are failed or pending -- run the same command again to retry "
              "(`data-loader status` lists them)")
        return 3
    return 0


def cmd_status(args) -> int:
    ds = open_dataset(args.dir)
    s = ds.status()
    if args.json:
        print(json.dumps(s, indent=2))
        return 0
    m = ds.manifest
    print(f"{args.dir}: {m['request']['provider']} {', '.join(m['products'])}, "
          f"{m['processing']['temporalMode']}, encoding {m['processing']['encoding']}; "
          f"updated {m['dataset']['updated']}, complete={m['dataset']['complete']}")
    print("  " + ", ".join(f"{k} {v}" for k, v in s["byStatus"].items()))
    print(f"  {'grid':22} " + " ".join(f"{k:>9}" for k in s["byStatus"]))
    for g, c in s["byGrid"].items():
        print(f"  {g:22} " + " ".join(f"{c.get(k, 0):9d}" for k in s["byStatus"]))
    for f in s["failedExamples"]:
        print(f"  failed: {f['key']} (attempts {f['attempts']}): {f['error']}")
    return 0


def cmd_verify(args) -> int:
    problems = open_dataset(args.dir).verify(checksums=not args.no_checksums)
    for p in problems[:50]:
        print(p)
    if problems:
        print(f"{len(problems)} problem(s)")
        return 4
    print("OK: every acquired file is present" + ("" if args.no_checksums else " and matches its sha256"))
    return 0


def cmd_upgrade(args) -> int:
    from data_loader.upgrade import parse_run, upgrade_dataset

    try:
        runs = [parse_run(r) for r in args.run or []]
    except ValueError as e:
        raise ConfigError(str(e)) from None
    r = upgrade_dataset(args.dir, runs=runs, dry_run=args.dry_run, check_files=args.check_files)
    print(f"{args.dir}: schema {r.from_version} -> {r.to_version}" + (" (dry run, nothing written)"
                                                                      if args.dry_run else ""))
    print(f"  rows {r.rows}; files given full per-band scale/offset: {r.files_scale_offset_added}")
    for sensor, nd in r.qa_nodata_set:
        print(f"  {sensor}: QA band nodata set to the file's nodata ({nd})")
    for commit, n in r.rows_software_attributed.items():
        print(f"  rows attributed to {commit}: {n}")
    if r.rows_without_software:
        print(f"  acquired/failed rows left without software (no --run covers them): {r.rows_without_software}")
    if r.errors_cleaned:
        print(f"  error messages with doubled redaction cleaned: {r.errors_cleaned}")
    if args.check_files:
        print(f"  checked {r.checked_files} file(s) against the metadata: "
              + ("OK" if not r.check_problems else f"{len(r.check_problems)} problem(s)"))
        for p in r.check_problems[:20]:
            print(f"    {p}")
    if not args.dry_run and not r.written:
        print("  already current: nothing written")
    return 4 if r.check_problems else 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="data-loader", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"data-loader {__version__}")
    sub = p.add_subparsers(dest="command", required=True)
    for name, fn, help_ in (("validate", cmd_validate, "check a config without network access"),
                            ("plan", cmd_plan, "discovery only: count what the request covers"),
                            ("run", cmd_run, "acquire (resumable)")):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("config")
        sp.add_argument("--output-dir", help="override output.dir from the config")
        if name == "run":
            sp.add_argument("--workers", type=int, help="override workers from the config")
        sp.set_defaults(fn=fn)
    sp = sub.add_parser("status", help="progress of a dataset directory")
    sp.add_argument("dir")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_status)
    sp = sub.add_parser("verify", help="check files against the manifest")
    sp.add_argument("dir")
    sp.add_argument("--no-checksums", action="store_true", help="sizes only (fast)")
    sp.set_defaults(fn=cmd_verify)
    sp = sub.add_parser("upgrade", help="upgrade a dataset's metadata to the current schema (no imagery is touched)")
    sp.add_argument("dir")
    sp.add_argument("--run", action="append", metavar="START=COMMIT",
                    help="a past run's start time (UTC, ISO 8601) and git commit; repeat for each run. "
                         "Rows written after START are attributed to COMMIT")
    sp.add_argument("--dry-run", action="store_true", help="report what would change; write nothing")
    sp.add_argument("--check-files", type=int, default=0, metavar="N",
                    help="also open N sampled files and compare their scale/offset/nodata with the metadata")
    sp.set_defaults(fn=cmd_upgrade)
    return p


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--config"]:
        print("data-loader: the command line changed in 1.0 -- use `data-loader run CONFIG` "
              "(and see docs/configuration.md to migrate the config)", file=sys.stderr)
        return 2
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except (ConfigError, DatasetError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except RunStopped as e:
        print(f"error: {e}", file=sys.stderr)
        return 5
    except KeyboardInterrupt:
        print("\ninterrupted -- finished acquisitions are saved; run the same command to resume", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
