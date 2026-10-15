#!/usr/bin/env python
"""Memory growth of usgs_ard whole-tile acquisition, without USGS.

The live Oregon run (usgs_ard, 4 workers) reached 62 GB peak RSS, mostly
glibc malloc arenas. This replays the real per-observation path offline:
7 real ARD band files served by a local HTTP server are fetched by
UsgsArdProvider._read_native_tile (requests + its download threads),
decoded by GDAL, written as one multi-band GeoTIFF by geotiff.write_raster
(GDAL deflate on ALL_CPUS threads), hashed, and deleted -- `workers`
observations at a time. Only signed-URL minting (M2M) is replaced.

Variants (each in its own subprocess):
    current            the code as it is, no failures
    arena2             MALLOC_ARENA_MAX=2 in the environment
    gdal_threads4      GeoTIFF writes with num_threads=4 instead of ALL_CPUS
    shared_pool        one long-lived download pool instead of a pool per observation
    failures_kept      every 3rd observation fails on its last band file (as a USGS 504
                       would), and finished futures are kept until the pass ends, as the
                       1.0 engine did
    failures_released  the same failures, each future released once handled

Band files: a directory with SR_B2..SR_B7 + QA_PIXEL .TIF (one ARD tile),
e.g. single-band copies of an archive file.

    python bench/download_memory_bench.py BAND_DIR [--units 300] [--workers 4] [--variants current,arena2]

Writes bench/results/download_memory_bench/<timestamp>.json.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
OUT = REPO / "bench" / "results" / "download_memory_bench"
VARIANTS = ("current", "arena2", "gdal_threads4", "shared_pool", "failures_kept", "failures_released")
FAIL_EVERY = 3
SUFFIXES = ["SR_B2", "SR_B3", "SR_B4", "SR_B5", "SR_B6", "SR_B7", "QA_PIXEL"]
NAMES = ["blue", "green", "red", "nir", "swir1", "swir2", "qa_pixel"]


def _rss_kb() -> tuple[int, int]:
    s = Path("/proc/self/status").read_text()
    get = lambda k: int(next(l for l in s.splitlines() if l.startswith(k)).split()[1])
    return get("VmRSS:"), get("VmHWM:")


def _serve(directory: Path) -> int:
    from functools import partial
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

    class Quiet(SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), partial(Quiet, directory=str(directory)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv.server_address[1]


def child(variant: str, band_dir: Path, units: int, workers: int) -> dict:
    import rasterio
    from rasterio.transform import Affine

    import data_loader.geotiff as gt
    import data_loader.providers.usgs_ard as ua
    from data_loader.providers.base import Grid

    port = _serve(band_dir)
    with rasterio.open(band_dir / "SR_B2.TIF") as s:
        grid = Grid(crs=s.crs.to_wkt(), transform=Affine(*list(s.transform)[:6]), width=s.width, height=s.height)
    p = ua.UsgsArdProvider(username="x", token="x", retries=1)
    failing = variant.startswith("failures")

    def band_url(tile, suffix, needed, force_refresh=False):
        i = int(tile[4:])
        if failing and i % FAIL_EVERY == 0 and suffix == SUFFIXES[-1]:
            return f"http://127.0.0.1:{port}/missing/{suffix}.TIF"  # 404 after the other bands downloaded
        return f"http://127.0.0.1:{port}/{suffix}.TIF"

    p._band_url = band_url
    if variant == "gdal_threads4":
        orig = gt._profile
        gt._profile = lambda *a, **k: {**orig(*a, **k), "num_threads": 4}
    if variant == "shared_pool":
        shared = ThreadPoolExecutor(len(SUFFIXES) * workers)

        class _Shared:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return shared

            def __exit__(self, *a):
                return False

        ua.ThreadPoolExecutor = _Shared
    tmp = Path(tempfile.mkdtemp(prefix="dlmem_"))
    samples = []

    def observation(i):
        if i % 25 == 0:
            samples.append((i, round(_rss_kb()[0] / 2**20, 2)))
        dn = p._read_native_tile(f"tile{i}", SUFFIXES, grid, {s: None for s in SUFFIXES})
        arrays = {n: dn[s] for n, s in zip(NAMES, SUFFIXES)}
        path = tmp / f"obs{i}.tif"
        gt.write_raster(path, arrays, grid, dtype="uint16", nodata=0,
                        scales={n: 2.75e-5 for n in NAMES[:6]}, offsets={n: -0.2 for n in NAMES[:6]})
        gt.sha256(path)
        path.unlink()

    t = time.perf_counter()
    failed = 0
    from concurrent.futures import as_completed

    with ThreadPoolExecutor(workers) as pool:
        futures = {pool.submit(observation, i): i for i in range(units)}
        for fu in as_completed(futures):
            try:
                fu.result()
            except Exception:
                failed += 1
            if variant != "failures_kept":
                del futures[fu]
    rss, hwm = _rss_kb()
    return {"variant": variant, "units": units, "workers": workers, "failed": failed,
            "seconds": round(time.perf_counter() - t, 1),
            "final_rss_gb": round(rss / 2**20, 2), "peak_rss_gb": round(hwm / 2**20, 2),
            "rss_gb_by_unit": sorted(samples)}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("band_dir")
    ap.add_argument("--units", type=int, default=300)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--variants", default=",".join(VARIANTS))
    ap.add_argument("--_child", nargs=4, help=argparse.SUPPRESS)
    args = ap.parse_args()
    if args._child:
        v, d, n, w = args._child
        print("RESULT " + json.dumps(child(v, Path(d), int(n), int(w))))
        return
    results = []
    print(f"{'variant':<15}{'units':>6}{'s':>7}{'peak GB':>9}{'final GB':>10}  RSS GB every 25 units")
    for v in args.variants.split(","):
        env = dict(os.environ, **({"MALLOC_ARENA_MAX": "2"} if v == "arena2" else {}))
        r = subprocess.run([sys.executable, __file__, "_", "--_child", v, args.band_dir, str(args.units),
                            str(args.workers)], capture_output=True, text=True, env=env)
        line = next((l for l in r.stdout.splitlines() if l.startswith("RESULT ")), None)
        if line is None:
            print(f"{v}: failed\n{r.stderr[-2000:]}")
            continue
        res = json.loads(line[7:])
        results.append(res)
        trace = " ".join(str(g) for _, g in res["rss_gb_by_unit"])
        print(f"{v:<15}{res['units']:>6}{res['seconds']:>7}{res['peak_rss_gb']:>9}{res['final_rss_gb']:>10}  {trace}",
              flush=True)
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{time.strftime('%Y%m%dT%H%M%S')}.json"
    path.write_text(json.dumps(results, indent=1))
    print(f"wrote {path.relative_to(REPO)}")


if __name__ == "__main__":
    main()
