#!/usr/bin/env python
"""Acquire the complete Landsat history of one or more CONUS ARD tiles
directly from USGS, and measure what it costs.

Config-driven: a YAML/JSON file names the tiles, date range, bands, save
format and workers (see bench/configs/ard_tile_history_h003v004.yaml for
every key). Direct-USGS (M2M-minted signed URLs -> COG reads) only: every
observation goes through DataLoader's own `usgs_ard.read_scene_bands` path
onto the tile's native grid -- a real acquisition, not a discovery count or
a projection.

    python bench/ard_tile_history_bench.py <subcommand> --config CFG [--tile hHHHvVVV]

Subcommands (state per tile under <output_dir>/<tile>/; --tile limits a
multi-tile config to one tile, otherwise tiles are processed in order):

    discover    STAC search of the WHOLE tile extent (filtered to the tile's
                grid h/v) over date_range; writes observations.json,
                items.json, grid.json. date_range.end omitted = today.
    pilot       a few full-tile reads from each Landsat era (serial), then a
                small concurrent batch; prints the full-run runtime/storage
                estimate. Writes pilot.jsonl.
    run [--label TEXT]
                the acquisition. Resumable: every finished observation is
                appended to records.jsonl as it completes; re-running skips
                those. The orchestrator re-launches the worker process if it
                is killed. --label tags this run's attempts in attempts.jsonl.
    report      summarizes records.jsonl/attempts.jsonl (targeted output),
                overall and per attempt; writes <summary_dir>/
                ard_tile_history_<tile>.json when summary_dir is set.

Saved imagery (save.format, into <output_dir>/<tile>/imagery/):
    uint16   the original C2 SR DN (lossless inverse of scale/offset;
             0 = nodata: fill, and QA-masked pixels when cloud_mask is on;
             scale/offset in the band tags), ~half of float32
    float32  reflectance, NaN nodata (DataLoader's engine writer)
    none     timing only
save.qa_pixel adds the raw QA_PIXEL bitmask as a last band named qa_pixel
(unscaled), so masking can be redone later and cloud can be told apart
from fill. With cloud_mask off and qa_pixel on, nothing is masked except
fill, and no information is lost.

Timing split per observation:
    mint_s  M2M `download-options` + `download-request` for the band
            files, INCLUDING time spent queued on the provider's
            one-request-at-a-time M2M lock (that wait is a real cost of the
            M2M restriction under concurrency, so it's attributed to minting)
    read_s  everything else: band file downloads (whole files in parallel
            for native-grid tile reads -- see usgs_ard._read_native_tile),
            decoding, QA masking, scale/offset
Bytes: each band file's size from a 1-byte Range GET (Content-Range), i.e.
what a full-tile read transfers; plus a machine-wide `netstat` delta per
attempt as a cross-check.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import resource
import statistics as st
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "bench"))

# CONUS ARD grid: Albers (WGS84) upper-left corner and tile size (5000 px x
# 30 m); matches h003v004's transform (-2115585, 2714805).
ARD_CRS = "+proj=aea +lat_0=23 +lon_0=-96 +lat_1=29.5 +lat_2=45.5 +x_0=0 +y_0=0 +datum=WGS84 +units=m"
ARD_ULX, ARD_ULY, ARD_TILE_M = -2565585.0, 3314805.0, 150_000.0
CANONICAL_BANDS = ("blue", "green", "red", "nir", "swir1", "swir2")
SAVE_FORMATS = ("uint16", "float32", "none")
RETRY_MARKERS = ("re-minting and retrying", "rate-limit retry", "transport retry", "STAC search retry")

# Pilot: one observation nearest each target date, per Landsat era.
PILOT_ERAS = [
    ("LT04", date(1990, 7, 1)), ("LT05", date(1995, 7, 15)), ("LT05", date(2008, 7, 15)),
    ("LE07", date(2001, 7, 15)), ("LE07", date(2015, 7, 15)),  # pre- and post-SLC-off
    ("LC08", date(2016, 7, 15)), ("LC09", date(2024, 7, 15)),
]
PILOT_BATCH_DATES = [date(1992, 8, 1), date(1998, 8, 1), date(2004, 8, 1), date(2010, 8, 1),
                     date(2013, 8, 1), date(2019, 8, 1), date(2022, 8, 1), date(2025, 8, 1)]
PILOT_BATCH_WORKERS = 4


# -- config ----------------------------------------------------------------

@dataclass(frozen=True)
class RunConfig:
    tiles: tuple[str, ...]
    start: date
    end: date | None  # None = today, at discover time
    bands: tuple[str, ...]
    save_format: str
    save_qa_pixel: bool
    cloud_mask: bool
    workers: int
    max_fails_per_obs: int
    output_dir: Path
    summary_dir: Path | None


_TOP_KEYS = {"tiles", "date_range", "bands", "save", "cloud_mask", "workers", "max_fails_per_obs",
             "output_dir", "summary_dir"}


def _repo_path(p) -> Path:
    p = Path(p)
    return p if p.is_absolute() else REPO / p


def parse_tile(tile: str) -> tuple[int, int]:
    m = re.fullmatch(r"h(\d{3})v(\d{3})", tile)
    if not m:
        raise ValueError(f"tile {tile!r}: expected CONUS ARD id like 'h003v004'")
    return int(m.group(1)), int(m.group(2))


def load_config(path) -> RunConfig:
    """YAML/JSON -> RunConfig. Unknown keys are errors (a typo must not
    silently fall back to a default on a multi-day run)."""
    path = Path(path)
    raw = path.read_text()
    if path.suffix in (".yaml", ".yml"):
        import yaml
        data = yaml.safe_load(raw)
    elif path.suffix == ".json":
        data = json.loads(raw)
    else:
        raise ValueError(f"unsupported config extension {path.suffix!r} -- use .yaml/.yml/.json")
    unknown = set(data) - _TOP_KEYS
    if unknown:
        raise ValueError(f"unknown config key(s): {sorted(unknown)}")

    tiles = data.get("tiles")
    if not tiles or not isinstance(tiles, list):
        raise ValueError("tiles: a non-empty list of ARD tile ids, e.g. [h003v004]")
    for t in tiles:
        parse_tile(t)
    dr = data.get("date_range") or {}
    if set(dr) - {"start", "end"} or "start" not in dr:
        raise ValueError("date_range: needs start (and optionally end), YYYY-MM-DD")
    start = date.fromisoformat(str(dr["start"]))
    end = date.fromisoformat(str(dr["end"])) if dr.get("end") else None
    if end and end < start:
        raise ValueError(f"date_range: end {end} is before start {start}")
    bands = tuple(data.get("bands", CANONICAL_BANDS))
    bad = [b for b in bands if b not in CANONICAL_BANDS]
    if bad or not bands:
        raise ValueError(f"bands: choose from {list(CANONICAL_BANDS)} (got {list(bands)})")
    save = data.get("save") or {}
    if set(save) - {"format", "qa_pixel"}:
        raise ValueError(f"save: unknown key(s) {sorted(set(save) - {'format', 'qa_pixel'})}")
    fmt = save.get("format", "uint16")
    if fmt not in SAVE_FORMATS:
        raise ValueError(f"save.format: one of {list(SAVE_FORMATS)} (got {fmt!r})")
    workers = int(data.get("workers", 4))
    if workers < 1:
        raise ValueError(f"workers must be >= 1, got {workers}")
    return RunConfig(
        tiles=tuple(tiles), start=start, end=end, bands=bands, save_format=fmt,
        save_qa_pixel=bool(save.get("qa_pixel", False)), cloud_mask=bool(data.get("cloud_mask", True)),
        workers=workers, max_fails_per_obs=int(data.get("max_fails_per_obs", 3)),
        output_dir=_repo_path(data.get("output_dir", "bench/results/ard_tile_history")),
        summary_dir=_repo_path(data["summary_dir"]) if data.get("summary_dir") else None,
    )


@dataclass(frozen=True)
class Tile:
    """One tile of a run: config + where its state lives."""
    cfg: RunConfig
    name: str

    @property
    def out(self) -> Path:
        return self.cfg.output_dir / self.name

    @property
    def hv(self) -> tuple[int, int]:
        return parse_tile(self.name)


# -- helpers ---------------------------------------------------------------

def _sensor(obs_id: str) -> str:
    return obs_id[:4]


def _net_bytes():
    from landtrendr_ready_run_case import _net_bytes as nb
    return nb()


def _peak_rss_mb() -> float:
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return (r if sys.platform == "darwin" else r * 1024) / 1e6


def _read_jsonl(p: Path) -> list[dict]:
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()] if p.exists() else []


class _CountingStdout(io.TextIOBase):
    """Tees stdout and counts provider retry messages (the providers report
    retries by printing; threads share one stdout, so counts are global)."""

    def __init__(self, real):
        self.real, self.retries, self._lock = real, 0, threading.Lock()

    def write(self, s):
        n = sum(s.count(m) for m in RETRY_MARKERS)
        if n:
            with self._lock:
                self.retries += n
        return self.real.write(s)

    def flush(self):
        self.real.flush()


def _provider():
    from data_loader.providers import get_provider
    p = get_provider("usgs_ard")
    # Time minting separately, per thread: wrap the one method that does
    # the M2M round trips (download-options + download-request).
    tl = threading.local()
    orig = p._signed_band_urls

    def timed(*a, **kw):
        t0 = time.perf_counter()
        try:
            return orig(*a, **kw)
        finally:
            tl.mint_s = getattr(tl, "mint_s", 0.0) + time.perf_counter() - t0

    p._signed_band_urls = timed
    p._mint_tl = tl
    return p


def _load_grid(t: Tile):
    from rasterio.transform import Affine
    from data_loader.providers.base import Grid
    g = json.loads((t.out / "grid.json").read_text())
    return Grid(crs=g["crs_wkt"], transform=Affine(*g["transform"]), width=g["width"], height=g["height"])


def _scene_refs(t: Tile, ids: list[str]) -> list:
    """Rebuild SceneRefs (the provider's read path needs the STAC item) for
    the given observation ids, from the cached discovery items."""
    import pystac
    from data_loader.providers.base import SceneRef
    from data_loader.providers.usgs_ard import _build_provenance
    items = {d["id"]: d for d in json.loads((t.out / "items.json").read_text())}
    refs = []
    for i in ids:
        it = pystac.Item.from_dict(items[i])
        refs.append(SceneRef(id=it.id, date=it.datetime.date(), cloud_percent=it.properties.get("eo:cloud_cover"),
                             handle=it, provenance=_build_provenance(it)))
    return refs


def _file_sizes(provider, obs_id: str, bands) -> int | None:
    """Sum of the band files' sizes (bands + QA_PIXEL) via 1-byte Range GETs
    on the already minted signed URLs (not timed as part of the observation)."""
    import requests
    from data_loader.providers.usgs_ard import _tile_product_id, band_file_suffixes
    tpid = _tile_product_id(obs_id)
    sfx = band_file_suffixes(tpid)
    total = 0
    for suffix in [sfx[b] for b in bands] + [sfx["qa"]]:
        url = provider._signed_url_cache.get((tpid, suffix))
        if not url:
            return None
        try:
            r = requests.get(url, headers={"Range": "bytes=0-0"}, timeout=30, stream=True)
            m = re.search(r"/(\d+)$", r.headers.get("Content-Range", ""))
            r.close()
            if not m:
                return None
            total += int(m.group(1))
        except Exception:
            return None
    return total


def _write_uint16(path: Path, arrays: dict, grid) -> None:
    """Reflectance -> original C2 SR DN (exact inverse of usgs_ard's
    scale/offset); NaN (fill, QA-masked) -> 0 = nodata. A `qa_pixel` array,
    if present, is written as-is as the last band (scale 1, offset 0)."""
    import numpy as np
    import rasterio
    from data_loader.providers.usgs_ard import SR_OFFSET, SR_SCALE
    names = [n for n in arrays if n != "qa_pixel"] + (["qa_pixel"] if "qa_pixel" in arrays else [])
    tmp = path.with_suffix(".tif.tmp")
    profile = {"driver": "GTiff", "dtype": "uint16", "count": len(names),
               "height": grid.height, "width": grid.width, "crs": grid.crs,
               "transform": grid.transform, "nodata": 0, "compress": "deflate",
               "predictor": 2, "tiled": True}
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(tmp, "w", **profile) as dst:
        for i, n in enumerate(names, start=1):
            if n == "qa_pixel":
                dn = arrays[n].astype("u2")
            else:
                dn = np.rint((arrays[n] - SR_OFFSET) / SR_SCALE)
                dn = np.where(np.isnan(dn), 0, np.clip(dn, 0, 65535)).astype("u2")
            dst.write(dn, i)
            dst.set_band_description(i, n)
        dst.scales = [1.0 if n == "qa_pixel" else SR_SCALE for n in names]
        dst.offsets = [0.0 if n == "qa_pixel" else SR_OFFSET for n in names]
    tmp.replace(path)


def acquire_one(provider, scene, grid, cfg: RunConfig, save_dir: Path | None = None,
                save_dtype: str = "float32") -> dict:
    """One full-tile observation through DataLoader's read path."""
    import numpy as np
    tl = provider._mint_tl
    tl.mint_s = 0.0
    rec = {"id": scene.id, "date": scene.date.isoformat(), "sensor": _sensor(scene.id),
           "cloud": scene.cloud_percent, "pid": os.getpid()}
    keep_qa = save_dir is not None and cfg.save_qa_pixel
    t0 = time.perf_counter()
    try:
        arrays = provider.read_scene_bands(scene, "landsat", list(cfg.bands), grid,
                                           pixel_cloud_mask=cfg.cloud_mask, keep_qa=keep_qa)
    except Exception as e:
        rec.update(ok=False, error=f"{type(e).__name__}: {e}"[:500],
                   total_s=time.perf_counter() - t0, mint_s=tl.mint_s)
        return rec
    total = time.perf_counter() - t0
    first = "nir" if "nir" in cfg.bands else cfg.bands[0]
    rec.update(ok=True, total_s=total, mint_s=tl.mint_s, read_s=total - tl.mint_s,
               # clear = neither fill nor (with cloud_mask) QA-masked -- both NaN.
               clear_frac=float(np.mean(~np.isnan(arrays[first]))),
               fill_pct=(scene.handle.properties.get("landsat:fill") if scene.handle is not None else None))
    rec["bytes"] = _file_sizes(provider, scene.id, cfg.bands)
    if save_dir is not None:
        from data_loader.geotiff import write_float32 as _write_geotiff
        p = save_dir / f"bands_{scene.date.isoformat()}_{scene.id}.tif"
        t1 = time.perf_counter()
        if save_dtype == "uint16":
            _write_uint16(p, arrays, grid)
        else:
            if "qa_pixel" in arrays:
                arrays["qa_pixel"] = arrays["qa_pixel"].astype("f4")
            _write_geotiff(p, arrays, grid)
        rec.update(write_s=time.perf_counter() - t1, saved_bytes=p.stat().st_size)
    del arrays
    return rec


def tile_bbox_lonlat(h: int, v: int) -> tuple[float, float, float, float]:
    """lon/lat bbox enclosing CONUS ARD tile (h, v), from densified edges."""
    import numpy as np
    from pyproj import Transformer
    x0, y1 = ARD_ULX + h * ARD_TILE_M, ARD_ULY - v * ARD_TILE_M
    e = np.linspace(0, ARD_TILE_M, 21)
    xs = np.concatenate([x0 + e, x0 + e, np.full(21, x0), np.full(21, x0 + ARD_TILE_M)])
    ys = np.concatenate([np.full(21, y1), np.full(21, y1 - ARD_TILE_M), y1 - e, y1 - e])
    lons, lats = Transformer.from_crs(ARD_CRS, "EPSG:4326", always_xy=True).transform(xs, ys)
    return float(min(lons)), float(min(lats)), float(max(lons)), float(max(lats))


# -- subcommands (each takes one Tile) --------------------------------------

def cmd_discover(t: Tile, _args):
    """Every observation of the tile. Searches the whole tile extent: an ARD
    item's geometry is its data footprint, so a small AOI misses "sliver"
    observations from neighboring WRS-2 paths that only clip the tile
    (h003v004's first run searched a 0.1 deg center AOI and found 2,411 of
    3,885). The grid query keeps neighboring tiles out."""
    import rasterio
    from pystac_client import Client
    from data_loader.providers.base import SceneRef
    from data_loader.providers.usgs_ard import COLLECTION, STAC_URL, _build_provenance, _tile_product_id
    t.out.mkdir(parents=True, exist_ok=True)
    p = _provider()
    end = t.cfg.end or date.today()
    h, v = t.hv
    bbox = tile_bbox_lonlat(h, v)
    t0 = time.perf_counter()
    items = list(Client.open(STAC_URL).search(
        collections=[COLLECTION], bbox=bbox, datetime=f"{t.cfg.start.isoformat()}/{end.isoformat()}", limit=500,
        query={"landsat:grid_horizontal": {"eq": f"{h:02d}"}, "landsat:grid_vertical": {"eq": f"{v:02d}"},
               "landsat:grid_region": {"eq": "CU"}}).items())
    disc_s = time.perf_counter() - t0
    if not items:
        print(f"{t.name}: no observations {t.cfg.start}..{end}")
        return
    refs = [SceneRef(id=it.id, date=it.datetime.date(), cloud_percent=it.properties.get("eo:cloud_cover"),
                     handle=it, provenance=_build_provenance(it)) for it in items]
    refs.sort(key=lambda r: (r.date, r.id))
    (t.out / "items.json").write_text(json.dumps([r.handle.to_dict() for r in refs]))
    obs = [{"id": r.id, "date": r.date.isoformat(), "sensor": _sensor(r.id), "cloud": r.cloud_percent,
            "scene_count": r.handle.properties.get("landsat:scene_count"),
            "fill": r.handle.properties.get("landsat:fill")} for r in refs]
    (t.out / "observations.json").write_text(json.dumps(
        {"tile": t.name, "start": t.cfg.start.isoformat(), "end": end.isoformat(), "discovery_s": disc_s,
         "search": f"whole tile extent {[round(b, 3) for b in bbox]}, grid h{h:02d}/v{v:02d}",
         "observations": obs}, indent=1))

    # Native tile grid, from one observation's QA file.
    tpid = _tile_product_id(refs[-1].id)
    url = p._band_url(tpid, "QA_PIXEL", ["QA_PIXEL"])
    with rasterio.open("/vsicurl/" + url) as src:
        (t.out / "grid.json").write_text(json.dumps({
            "crs_wkt": src.crs.to_wkt(), "transform": list(src.transform)[:6],
            "width": src.width, "height": src.height}))
        print(f"{t.name}: grid {src.width}x{src.height}, res {src.res}")
    by = {}
    for o in obs:
        by[o["sensor"]] = by.get(o["sensor"], 0) + 1
    print(f"{t.name}: {len(obs)} observations {obs[0]['date']}..{obs[-1]['date']} in {disc_s:.1f}s; by sensor {by}")


def cmd_pilot(t: Tile, _args):
    obs = json.loads((t.out / "observations.json").read_text())["observations"]
    done = {r["id"] for r in _read_jsonl(t.out / "pilot.jsonl")}

    def nearest(prefix, d, exclude):
        c = [o for o in obs if o["sensor"] == prefix and o["id"] not in exclude]
        return min(c, key=lambda o: abs(date.fromisoformat(o["date"]) - d))["id"] if c else None

    serial_ids, used = [], set()
    for pre, d in PILOT_ERAS:
        i = nearest(pre, d, used)
        if i:
            serial_ids.append(i); used.add(i)
    batch_ids = []
    for d in PILOT_BATCH_DATES:
        c = [o for o in obs if o["id"] not in used]
        if not c:
            break
        i = min(c, key=lambda o: abs(date.fromisoformat(o["date"]) - d))["id"]
        batch_ids.append(i); used.add(i)

    p = _provider()
    grid = _load_grid(t)
    sys.stdout = counter = _CountingStdout(sys.stdout)
    save_dir = t.out / "pilot_output"
    net0 = _net_bytes()
    with (t.out / "pilot.jsonl").open("a") as f:
        for i in serial_ids:
            if i in done:
                continue
            ref = _scene_refs(t, [i])[0]
            rec = acquire_one(p, ref, grid, t.cfg, save_dir=save_dir)
            rec["phase"] = "serial"
            f.write(json.dumps(rec) + "\n"); f.flush()
            print(f"  serial {i[:25]} {rec['date']} ok={rec['ok']} total={rec['total_s']:.1f}s "
                  f"mint={rec['mint_s']:.1f}s bytes={rec.get('bytes')} " + ("" if rec["ok"] else rec["error"][:200]),
                  flush=True)
        pending = [i for i in batch_ids if i not in done]
        if pending:
            t0 = time.perf_counter()
            refs = _scene_refs(t, pending)
            with ThreadPoolExecutor(PILOT_BATCH_WORKERS) as ex:
                futs = [ex.submit(acquire_one, p, r, grid, t.cfg) for r in refs]
                recs = [fu.result() for fu in futs]
            wall = time.perf_counter() - t0
            for rec in recs:
                rec.update(phase="batch", batch_wall_s=wall, batch_workers=PILOT_BATCH_WORKERS)
                f.write(json.dumps(rec) + "\n")
            print(f"  batch of {len(recs)} at {PILOT_BATCH_WORKERS} workers: wall {wall:.1f}s, "
                  f"ok {sum(r['ok'] for r in recs)}", flush=True)
    net1 = _net_bytes()
    sys.stdout = counter.real
    (t.out / "pilot_meta.json").write_text(json.dumps({
        "peak_rss_mb": _peak_rss_mb(), "retries": counter.retries,
        "net_mb_indicative": (net1 - net0) / 1e6 if net0 and net1 else None}))
    estimate(t)


def estimate(t: Tile, _args=None):
    """Full-run projection from pilot.jsonl -- targeted output only."""
    obs = json.loads((t.out / "observations.json").read_text())["observations"]
    recs = _read_jsonl(t.out / "pilot.jsonl")
    meta = json.loads((t.out / "pilot_meta.json").read_text()) if (t.out / "pilot_meta.json").exists() else {}
    ok = [r for r in recs if r["ok"]]
    serial = [r for r in ok if r["phase"] == "serial"]
    batch = [r for r in ok if r["phase"] == "batch"]
    counts = {}
    for o in obs:
        counts[o["sensor"]] = counts.get(o["sensor"], 0) + 1

    print(f"\n{t.name} pilot: {len(ok)}/{len(recs)} ok, retries={meta.get('retries')}, "
          f"peak RSS {meta.get('peak_rss_mb', 0):.0f} MB")
    print(f"{'id':<42}{'phase':<8}{'total s':>8}{'mint s':>8}{'read s':>8}{'MB':>7}{'saved MB':>9}{'clear':>7}")
    for r in recs:
        print(f"{r['id']:<42}{r['phase']:<8}{r['total_s']:>8.1f}{r['mint_s']:>8.1f}"
              f"{r.get('read_s', float('nan')):>8.1f}{(r.get('bytes') or 0)/1e6:>7.0f}"
              f"{(r.get('saved_bytes') or 0)/1e6:>9.0f}{r.get('clear_frac', float('nan')):>7.2f}"
              + ("" if r["ok"] else "  FAIL " + r["error"][:80]))
    if not serial:
        return
    all_mean = st.mean(r["total_s"] for r in serial)
    per_sensor = {s: st.mean(r["total_s"] for r in serial if r["sensor"] == s) for s in {r["sensor"] for r in serial}}
    serial_h = sum(n * per_sensor.get(s, all_mean) for s, n in counts.items()) / 3600
    speedup = None
    if batch:
        wall = batch[0]["batch_wall_s"]
        expected_serial = sum(per_sensor.get(r["sensor"], all_mean) for r in batch)
        speedup = expected_serial / wall
    mb = [r["bytes"] / 1e6 for r in ok if r.get("bytes")]
    saved = [r["saved_bytes"] / 1e6 for r in serial if r.get("saved_bytes")]
    n = len(obs)
    print(f"\nObservations: {n}  {counts}")
    print(f"Serial mean {all_mean:.1f}s/obs; per sensor " + ", ".join(f"{s} {v:.0f}s" for s, v in sorted(per_sensor.items())))
    print(f"Mint share: {100 * sum(r['mint_s'] for r in serial) / sum(r['total_s'] for r in serial):.0f}% of serial time")
    print(f"Projected serial: {serial_h:.1f} h")
    if speedup:
        print(f"Batch speedup at {PILOT_BATCH_WORKERS} workers: {speedup:.2f}x -> projected {serial_h / speedup:.1f} h "
              f"({n / (serial_h / speedup):.0f} obs/h)")
    if mb:
        print(f"Transfer: {st.mean(mb):.0f} MB/obs -> ~{st.mean(mb) * n / 1e3:.0f} GB total")
    if saved:
        print(f"Persistent storage (DataLoader float32 deflate GeoTIFF): {st.mean(saved):.0f} MB/obs "
              f"(range {min(saved):.0f}-{max(saved):.0f}) -> ~{st.mean(saved) * n / 1e3:.0f} GB")
    if mb:
        print(f"Persistent storage as delivered (uint16 COGs): ~{st.mean(mb) * n / 1e3:.0f} GB")


def cmd_run(t: Tile, args):
    """Orchestrator: re-launches the worker until nothing is pending."""
    for attempt in range(1, 51):
        pending = _pending_ids(t)
        if not pending:
            print(f"{t.name}: nothing pending -- run complete"); break
        print(f"[{t.name} attempt {attempt}] {len(pending)} observations pending", flush=True)
        rc = subprocess.run([sys.executable, __file__, "_worker", "--config", str(args.config), "--tile", t.name]
                            + (["--label", args.label] if args.label else [])).returncode
        if rc != 0:
            print(f"[{t.name} attempt {attempt}] worker exited {rc} (killed?) -- resuming", flush=True)
            time.sleep(10)
    cmd_report(t, args)


def _pending_ids(t: Tile) -> list[str]:
    obs = json.loads((t.out / "observations.json").read_text())["observations"]
    recs = _read_jsonl(t.out / "records.jsonl")
    ok = {r["id"] for r in recs if r["ok"]}
    fails = {}
    for r in recs:
        if not r["ok"]:
            fails[r["id"]] = fails.get(r["id"], 0) + 1
    return [o["id"] for o in obs if o["id"] not in ok and fails.get(o["id"], 0) < t.cfg.max_fails_per_obs]


def cmd_worker(t: Tile, args):
    cfg = t.cfg
    pending = _pending_ids(t)
    p = _provider()
    grid = _load_grid(t)
    sys.stdout = counter = _CountingStdout(sys.stdout)
    lock = threading.Lock()
    attempt = {"started": time.time(), "workers": cfg.workers, "pending_at_start": len(pending),
               "save": cfg.save_format, "save_qa_pixel": cfg.save_qa_pixel, "cloud_mask": cfg.cloud_mask,
               "label": args.label}
    save_dir = None if cfg.save_format == "none" else t.out / "imagery"
    net0 = _net_bytes()
    t0 = time.perf_counter()
    cpu0 = resource.getrusage(resource.RUSAGE_SELF)
    done = 0

    def flush_attempt(final=False):
        cpu = resource.getrusage(resource.RUSAGE_SELF)
        net1 = _net_bytes()
        attempt.update(wall_s=time.perf_counter() - t0, done=done, retries=counter.retries,
                       cpu_s=(cpu.ru_utime + cpu.ru_stime) - (cpu0.ru_utime + cpu0.ru_stime),
                       peak_rss_mb=_peak_rss_mb(), final=final,
                       net_mb_indicative=(net1 - net0) / 1e6 if net0 and net1 else None)
        # attempts.jsonl holds one line per attempt; rewrite this attempt's line.
        path = t.out / "attempts.jsonl"
        rows = [r for r in _read_jsonl(path) if r["started"] != attempt["started"]]
        path.write_text("".join(json.dumps(r) + "\n" for r in rows + [attempt]))

    with (t.out / "records.jsonl").open("a") as f, ThreadPoolExecutor(cfg.workers) as ex:
        futs = {ex.submit(acquire_one, p, ref, grid, cfg, save_dir, cfg.save_format): ref.id
                for ref in _scene_refs(t, pending)}
        for fu in as_completed(futs):
            rec = fu.result()
            rec["t_end"] = time.time()
            with lock:
                f.write(json.dumps(rec) + "\n"); f.flush()
                done += 1
                if done % 10 == 0:
                    flush_attempt()
                    print(f"  {t.name}: {done}/{len(pending)} done this attempt, "
                          f"{(time.perf_counter() - t0) / done:.1f}s/obs effective", flush=True)
    flush_attempt(final=True)
    sys.stdout = counter.real


def summarize(t: Tile) -> dict:
    """records.jsonl/attempts.jsonl -> compact summary, overall and per attempt."""
    obs = json.loads((t.out / "observations.json").read_text())
    recs = _read_jsonl(t.out / "records.jsonl")
    atts = _read_jsonl(t.out / "attempts.jsonl")
    ok = {}
    for r in recs:
        if r["ok"]:
            ok[r["id"]] = r
    okl = list(ok.values())
    failed_ids = {r["id"] for r in recs if not r["ok"]} - set(ok)
    n = len(obs["observations"])
    wall = sum(a["wall_s"] for a in atts)
    summ = {
        "tile": t.name, "period": [obs["start"], obs["end"]], "observations": n,
        "by_sensor": {}, "acquired": len(okl), "permanently_failed": len(failed_ids),
        "failed_attempts": sum(1 for r in recs if not r["ok"]),
        "retries_logged": sum(a.get("retries", 0) for a in atts), "attempts": len(atts),
        "workers": sorted({a["workers"] for a in atts}),
        "wall_h": round(wall / 3600, 2),
        "elapsed_h": round((max(a["started"] + a["wall_s"] for a in atts) - min(a["started"] for a in atts)) / 3600, 2) if atts else None,
        "discovery_s": round(obs["discovery_s"], 1),
        # Older observations.json files predate the whole-tile search.
        "discovery_search": obs.get("search", "0.1 deg AOI at tile center -- misses edge-sliver "
                                              "observations (h003v004: 2,411 of 3,885)"),
    }
    if okl:
        tot = [r["total_s"] for r in okl]
        summ.update(
            obs_per_hour=round(len(okl) / (wall / 3600), 1) if wall else None,
            effective_s_per_obs=round(wall / len(okl), 1) if wall else None,
            per_obs_total_s={"mean": round(st.mean(tot), 1), "median": round(st.median(tot), 1),
                             "p90": round(st.quantiles(tot, n=10)[-1], 1) if len(tot) >= 10 else None},
            mint_s={"mean": round(st.mean(r["mint_s"] for r in okl), 2),
                    "median": round(st.median(r["mint_s"] for r in okl), 2),
                    "sum_h": round(sum(r["mint_s"] for r in okl) / 3600, 2)},
            read_s={"mean": round(st.mean(r["read_s"] for r in okl), 1),
                    "median": round(st.median(r["read_s"] for r in okl), 1),
                    "sum_h": round(sum(r["read_s"] for r in okl) / 3600, 2)},
            bytes_gb=round(sum(r.get("bytes") or 0 for r in okl) / 1e9, 1),
            saved_gb=round(sum(r.get("saved_bytes") or 0 for r in okl) / 1e9, 1),
            write_s_mean=round(st.mean(r["write_s"] for r in okl if "write_s" in r), 1)
                if any("write_s" in r for r in okl) else None,
            net_gb_indicative=round(sum(a.get("net_mb_indicative") or 0 for a in atts) / 1e3, 1),
            peak_rss_mb=round(max(a["peak_rss_mb"] for a in atts)),
            cpu_h=round(sum(a["cpu_s"] for a in atts) / 3600, 2),
            mean_clear_frac=round(st.mean(r["clear_frac"] for r in okl), 3),
        )
        for s in sorted({o["sensor"] for o in obs["observations"]}):
            rs = [r for r in okl if r["sensor"] == s]
            summ["by_sensor"][s] = {
                "observations": sum(1 for o in obs["observations"] if o["sensor"] == s),
                "acquired": len(rs),
                "mean_total_s": round(st.mean(r["total_s"] for r in rs), 1) if rs else None,
                "mean_mb": round(st.mean((r.get("bytes") or 0) for r in rs) / 1e6) if rs else None}
    # Per attempt: a record belongs to the attempt whose time window holds
    # its t_end (attempts run one after another, never concurrently).
    summ["by_attempt"] = []
    for a in atts:
        rs = [r for r in recs if a["started"] <= r.get("t_end", 0) <= a["started"] + a["wall_s"] + 1]
        ok_a = [r for r in rs if r["ok"]]
        summ["by_attempt"].append({
            "label": a.get("label"), "started": time.strftime("%Y-%m-%dT%H:%M", time.localtime(a["started"])),
            "workers": a["workers"], "pending_at_start": a["pending_at_start"], "ok": len(ok_a),
            "failed": len(rs) - len(ok_a), "wall_h": round(a["wall_s"] / 3600, 2),
            "obs_per_hour": round(len(ok_a) / (a["wall_s"] / 3600), 1) if a["wall_s"] else None,
            "mean_fill_pct": round(st.mean(r["fill_pct"] for r in ok_a if r.get("fill_pct") is not None), 1)
                if any(r.get("fill_pct") is not None for r in ok_a) else None,
            "mean_s": {k: round(st.mean(r[k] for r in ok_a if k in r), 1)
                       for k in ("total_s", "mint_s", "read_s", "write_s") if any(k in r for r in ok_a)},
            "mean_mb": round(st.mean((r.get("bytes") or 0) for r in ok_a) / 1e6) if ok_a else None,
            "mean_saved_mb": round(st.mean((r.get("saved_bytes") or 0) for r in ok_a) / 1e6, 1) if ok_a else None,
        })
    return summ


def cmd_report(t: Tile, _args):
    summ = summarize(t)
    if t.cfg.summary_dir is not None:
        out = t.cfg.summary_dir / f"ard_tile_history_{t.name}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(summ, indent=2) + "\n")
    print(json.dumps(summ, indent=1))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["discover", "pilot", "estimate", "run", "report", "_worker"])
    ap.add_argument("--config", required=True, type=Path, help="YAML/JSON run config")
    ap.add_argument("--tile", help="process only this tile from the config")
    ap.add_argument("--label", help="run: tag for this run's attempts in attempts.jsonl")
    args = ap.parse_args()
    cfg = load_config(args.config)
    if args.tile and args.tile not in cfg.tiles:
        ap.error(f"--tile {args.tile} is not in the config's tiles {list(cfg.tiles)}")
    fn = {"discover": cmd_discover, "pilot": cmd_pilot, "estimate": estimate, "run": cmd_run,
          "report": cmd_report, "_worker": cmd_worker}[args.cmd]
    for name in ([args.tile] if args.tile else cfg.tiles):
        fn(Tile(cfg, name), args)


if __name__ == "__main__":
    main()
