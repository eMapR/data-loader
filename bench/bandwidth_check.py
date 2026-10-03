#!/usr/bin/env python
"""Is the full ARD tile history run limited by this machine's network or by
USGS?

Measures aggregate download throughput (MB/s) at several stream counts
against:
    usgs        M2M-minted signed URLs for the ARD band files of a few
                h003v004 tiles (the real source of the tile-history run;
                needs USGS_M2M_USERNAME/USGS_M2M_TOKEN)
    s3          a public Sentinel-2 COG in s3://sentinel-cogs (us-west-2,
                no Requester Pays), found via Earth Search STAC

Each test streams to memory (discarded) for at most --seconds, then
reports bytes / wall time. If USGS stays flat as streams increase while
the references scale, USGS (or its per-account limit) is the bottleneck;
if all sources plateau at the same rate, it's this machine's link.

    python bench/bandwidth_check.py [--seconds 30] [--streams 1 4 8 14] [--sources usgs s3]

Writes bench/results/bandwidth_check/<timestamp>.json.
"""
from __future__ import annotations

import argparse
import json
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
OUT = REPO / "bench" / "results" / "bandwidth_check"
TILE_OBS = REPO / "bench" / "results" / "ard_tile_history" / "h003v004" / "observations.json"
CHUNK = 1 << 20


def _stream(url: str, deadline: float, counter: list, lock: threading.Lock, headers=None):
    """Download `url` (restarting it if it finishes early) until `deadline`."""
    while time.perf_counter() < deadline:
        with requests.get(url, stream=True, timeout=30, headers=headers or {}) as r:
            r.raise_for_status()
            for chunk in r.iter_content(CHUNK):
                with lock:
                    counter[0] += len(chunk)
                if time.perf_counter() >= deadline:
                    return


def measure(urls: list[str], streams: int, seconds: float) -> dict:
    counter, lock = [0], threading.Lock()
    t0 = time.perf_counter()
    deadline = t0 + seconds
    errors = []
    with ThreadPoolExecutor(streams) as ex:
        futs = [ex.submit(_stream, urls[i % len(urls)], deadline, counter, lock) for i in range(streams)]
        for f in futs:
            try:
                f.result()
            except Exception as e:  # keep measuring the other streams
                errors.append(f"{type(e).__name__}: {e}"[:200])
    wall = time.perf_counter() - t0
    return {"streams": streams, "bytes": counter[0], "wall_s": wall,
            "mb_s": counter[0] / 1e6 / wall, "errors": errors}


def usgs_urls(n_tiles: int) -> list[str]:
    """Signed URLs for all 7 band files of the n most recent LC08 tiles."""
    from data_loader.providers import get_provider
    from data_loader.providers.usgs_ard import _tile_product_id, band_file_suffixes
    obs = json.loads(TILE_OBS.read_text())["observations"]
    ids = [o["id"] for o in obs if o["sensor"] == "LC08"][-n_tiles:]
    p = get_provider("usgs_ard")
    urls = []
    for i in ids:
        tpid = _tile_product_id(i)
        sfx = band_file_suffixes(tpid)
        wanted = sorted({sfx[b] for b in ("blue", "green", "red", "nir", "swir1", "swir2", "qa")})
        urls += list(p._signed_band_urls(tpid, wanted).values())
    return urls


def s3_url() -> str:
    r = requests.post("https://earth-search.aws.element84.com/v1/search", timeout=30, json={
        "collections": ["sentinel-2-l2a"], "bbox": [-122.0, 44.0, -121.5, 44.5],
        "datetime": "2024-07-01T00:00:00Z/2024-07-31T23:59:59Z", "limit": 1})
    r.raise_for_status()
    return r.json()["features"][0]["assets"]["nir"]["href"]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seconds", type=float, default=30)
    ap.add_argument("--streams", type=int, nargs="+", default=[1, 4, 8, 14])
    ap.add_argument("--sources", nargs="+", choices=["s3", "usgs"], default=["s3", "usgs"])
    args = ap.parse_args()

    getters = {"s3": lambda: [s3_url()], "usgs": lambda: usgs_urls(n_tiles=2)}  # usgs: 14 distinct band files
    sources = {name: getters[name]() for name in args.sources}
    results = {"host": socket.gethostname(), "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
               "seconds": args.seconds, "tests": []}
    print(f"{'source':<11}{'streams':>8}{'MB/s':>9}{'Mbit/s':>9}  errors")
    for name, urls in sources.items():
        for n in args.streams:
            r = measure(urls, n, args.seconds)
            r["source"] = name
            results["tests"].append(r)
            print(f"{name:<11}{n:>8}{r['mb_s']:>9.2f}{r['mb_s'] * 8:>9.0f}  {len(r['errors']) or ''}", flush=True)

    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{time.strftime('%Y%m%dT%H%M%S')}.json"
    path.write_text(json.dumps(results, indent=1))
    print(f"wrote {path.relative_to(REPO)}")


if __name__ == "__main__":
    main()
