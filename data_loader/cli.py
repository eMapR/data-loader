"""Command line: `data-loader <command> ...` (or `python -m data_loader ...`).

    data-loader validate CONFIG     check the config (no network)
    data-loader plan CONFIG         discovery only: what exists, what's already acquired
    data-loader run CONFIG          acquire; re-run the same command to resume or update
    data-loader status DIR          progress of a dataset (no network)
    data-loader verify DIR          check every file against the manifest (size + sha256)

Exit codes: 0 ok; 1 unexpected error; 2 invalid config/request;
3 run finished but some acquisitions are failed or still pending
(re-run to retry); 4 verify found problems.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime

from data_loader._version import __version__
from data_loader.config import ConfigError, load_config
from data_loader.dataset import DatasetError, open_dataset


def _log(msg: str) -> None:
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def _load(args):
    return load_config(args.config, output_dir=args.output_dir)


def cmd_validate(args) -> int:
    from data_loader.engine import check_request

    cfg = _load(args)
    warnings = check_request(cfg)
    windows = cfg.time.windows()
    print(f"OK: {args.config}")
    print(f"  provider {cfg.provider}, sensors {', '.join(s.name for s in cfg.sensors)}, "
          f"temporal_mode {cfg.temporal_mode}, encoding {cfg.output.encoding}")
    print(f"  aoi: " + (f"tiles {', '.join(cfg.aoi.tiles)}" if cfg.aoi.tiles
                        else f"bbox {cfg.aoi.upper_left} -> {cfg.aoi.lower_right}"))
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
    print(f"  {'sensor':10} {'grid':10} {'units':>7} {'acquired':>9} {'to do':>7} {'filtered':>9}")
    for r in s["grids"]:
        print(f"  {r['sensor']:10} {r['grid']:10} {r['units']:7d} {r['acquired']:9d} "
              f"{r['units'] - r['acquired']:7d} {r['filtered']:9d}")
    tot = sum(r["units"] for r in s["grids"])
    done = sum(r["acquired"] for r in s["grids"])
    print(f"  {'total':21} {tot:7d} {done:9d} {tot - done:7d}")
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
    except KeyboardInterrupt:
        print("\ninterrupted -- finished acquisitions are saved; run the same command to resume", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
