#!/usr/bin/env python
"""Summarize the LandTrendr-ready pipeline benchmarks across AOI scales
(2tile, 4tile, and any 8tile full or smoke run) into one compact, tracked
JSON file, and -- for smoke runs -- project what the full run would cost.

Reads the (git-ignored) raw results under bench/results/landtrendr_ready*/
and writes docs/development/benchmarks/data/landtrendr_scaling.json. Prints only a short
table, so it can be re-run cheaply whenever a new result lands.

Usage:
    python bench/summarize_landtrendr.py
"""
from __future__ import annotations

import json
import statistics as st
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RESULTS = REPO / "bench" / "results"
OUT = REPO / "docs" / "benchmarks" / "data" / "landtrendr_scaling.json"

# results directory -> AOI preset label. Smoke dirs are found by glob.
FULL_RUN_DIRS = {"landtrendr_ready": "2tile", "landtrendr_ready_4tile": "4tile",
                 "landtrendr_ready_8tile": "8tile"}
PROVIDERS = ["usgs_ard", "planetary_computer"]


def _latest_complete(result_dir: Path) -> dict | None:
    """Per provider, the newest orchestrator JSON in which that provider
    finished without error (single-provider re-runs write their own JSON,
    so the newest file alone may not cover both providers)."""
    merged: dict = {"results": {}, "comparability": None, "_source": []}
    for f in sorted(result_dir.glob("landtrendr_ready_*.json"), reverse=True):
        d = json.loads(f.read_text())
        used = False
        for prov, r in d.get("results", {}).items():
            if prov not in merged["results"] and "error" not in r:
                merged["results"][prov] = r
                used = True
        if merged["comparability"] is None and (d.get("comparability") or {}).get("shared_dates") is not None:
            merged["comparability"] = d["comparability"]
            used = True
        if used:
            merged["_source"].append(str(f.relative_to(REPO)))
    return merged if merged["results"] else None


def _grid_mpx(grid: str) -> float:
    w, h = (int(v) for v in grid.split("x"))
    return round(w * h / 1e6, 2)


def _unit_reads(output_dir: Path) -> list[float]:
    p = output_dir / "unit_reads.jsonl"
    if not p.exists():
        return []
    rows = [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
    return [r["read_s"] for r in rows if r["ok"]]


def _summarize_provider(r: dict, smoke: bool) -> dict:
    t = r["timers"]
    dates_done = r["num_output_dates"]
    reads = _unit_reads(Path(r["output_dir"])) if smoke else []
    units_read = len(reads) if smoke else r["num_source_units"] - len(r["failures"])
    per_unit = (st.mean(reads) if reads else
                (t["acquisition_s"] / units_read if units_read else None))
    per_date_overhead = ((t["mosaic_s"] + t["indices_s"] + t["write_s"]) / dates_done
                         if dates_done else None)
    s = {
        "grid_mpx": _grid_mpx(r["grid"]),
        "units_discovered": r["num_source_units"],
        "dates_discovered": r.get("num_dates_discovered"),
        "dates_output": dates_done,
        "units_read": units_read,
        "per_unit_read_s": round(per_unit, 1) if per_unit else None,
        "per_date_overhead_s": round(per_date_overhead, 1) if per_date_overhead else None,
        "peak_rss_mb": round(r["peak_rss_mb"]),
        "output_mb_per_date": round(r["output_bytes"] / 1e6 / dates_done, 1) if dates_done else None,
        "failures": len(r["failures"]),
        "retries": r.get("retries_observed", 0),
        "attempts": r.get("attempts"),
    }
    if not smoke:
        s["wall_s"] = round(r["wall_s"], 1)
        s["wall_s_per_output_date"] = round(r["wall_s"] / dates_done, 1)
    else:
        s["per_unit_read_s_range"] = [round(min(reads), 1), round(max(reads), 1)] if reads else None
        s["per_unit_read_s_median"] = round(st.median(reads), 1) if reads else None
        # Projection: every discovered unit read at the sampled mean cost,
        # plus per-date mosaic/index/write overhead for every date, plus
        # one discovery call. Bracketed by the sample's slowest/fastest
        # quartile-ish bounds (min/max of means is too wide with few samples,
        # so use the sample's 25th/75th percentiles when there are >= 4).
        n_units = r["num_source_units"]
        n_dates = r["num_dates_discovered"]
        overhead = (per_date_overhead or 0) * n_dates + t["discovery_s"]
        q = st.quantiles(reads, n=4) if len(reads) >= 4 else [min(reads), None, max(reads)]
        s["projected_full_run"] = {
            "units": n_units, "dates": n_dates,
            "wall_h": round((n_units * per_unit + overhead) / 3600, 1),
            "wall_h_range_p25_p75": [round((n_units * q[0] + overhead) / 3600, 1),
                                     round((n_units * q[2] + overhead) / 3600, 1)],
            "output_gb": round(s["output_mb_per_date"] * n_dates / 1000, 1) if dates_done else None,
            "peak_rss_mb": s["peak_rss_mb"],
        }
    return s


def main():
    runs = []
    for dirname, label in FULL_RUN_DIRS.items():
        d = _latest_complete(RESULTS / dirname) if (RESULTS / dirname).exists() else None
        if d:
            runs.append((label, False, d))
    for sd in sorted(RESULTS.glob("landtrendr_ready_*_smoke*")):
        d = _latest_complete(sd)
        if d:
            runs.append((sd.name.removeprefix("landtrendr_ready_"), True, d))

    summary = []
    for label, smoke, d in runs:
        entry = {"aoi": label, "smoke": smoke, "source": d["_source"], "providers": {}}
        for p in PROVIDERS:
            if p in d["results"]:
                entry["providers"][p] = _summarize_provider(d["results"][p], smoke)
        comp = d.get("comparability") or {}
        if comp.get("shared_dates") is not None:
            entry["shared_dates"] = comp["shared_dates"]
        summary.append(entry)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(summary, indent=2) + "\n")

    hdr = f"{'aoi':<14}{'provider':<20}{'Mpx':>6}{'units':>7}{'dates':>7}{'s/unit':>8}{'RSS MB':>8}{'MB/date':>9}{'fail':>6}{'wall h':>8}"
    print(hdr)
    for e in summary:
        for p, s in e["providers"].items():
            wall = (s["wall_s"] / 3600 if "wall_s" in s else s["projected_full_run"]["wall_h"])
            tag = "" if "wall_s" in s else "*"
            print(f"{e['aoi']:<14}{p:<20}{s['grid_mpx']:>6}{s['units_discovered']:>7}"
                  f"{s['dates_output']:>7}{s['per_unit_read_s']:>8}{s['peak_rss_mb']:>8}"
                  f"{s['output_mb_per_date']:>9}{s['failures']:>6}{wall:>7.1f}{tag}")
    print("* projected full-run wall time from smoke sample")
    print(f"wrote {OUT.relative_to(REPO)}")


if __name__ == "__main__":
    main()
