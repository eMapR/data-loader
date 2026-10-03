#!/usr/bin/env python
"""Full-tile usgs_ard read: whole-file path vs /vsicurl path, live.

For a few h003v004 observations, reads all 6 SR bands + QA_PIXEL onto the
tile's native grid twice through `UsgsArdProvider.read_scene_bands`:
    whole    the default for native-grid reads -- whole band files fetched
             in parallel, decoded from memory (usgs_ard._read_native_tile)
    vsicurl  the windowed /vsicurl + WarpedVRT path, forced by disabling
             the native-grid check (what every read used before 2026-10-02)
and reports both timings and whether the outputs are bit-identical.
Signed URLs are minted once up front, so neither timing includes M2M.

Needs USGS_M2M_USERNAME/USGS_M2M_TOKEN and the outputs of
`ard_tile_history_bench.py discover`.

    python bench/ard_read_path_compare.py [--n 2] [--sensor LC08]

Writes bench/results/ard_read_path_compare/<timestamp>.json.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "bench"))
OUT = REPO / "bench" / "results" / "ard_read_path_compare"


def main():
    import numpy as np

    import ard_tile_history_bench as ath
    import data_loader.providers.usgs_ard as ua

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--n", type=int, default=2, help="observations to compare")
    ap.add_argument("--sensor", default="LC08")
    args = ap.parse_args()

    obs = json.loads((ath.OUT / "observations.json").read_text())["observations"]
    cands = [o["id"] for o in obs if o["sensor"] == args.sensor]
    ids = [cands[int(k * len(cands) / args.n)] for k in range(args.n)]  # spread over the record
    grid = ath._load_grid()
    p = ath._provider()

    results = []
    print(f"{'observation':<26}{'whole s':>9}{'vsicurl s':>11}{'speedup':>9}  identical")
    for i in ids:
        scene = ath._scene_refs([i])[0]
        tpid = ua._tile_product_id(i)
        sfx = ua.band_file_suffixes(tpid)
        needed = [sfx[b] for b in ath.BANDS] + [sfx["qa"]]
        p._band_url(tpid, needed[0], needed)  # mint once, outside both timings

        t = time.perf_counter()
        whole = p.read_scene_bands(scene, "landsat", ath.BANDS, grid, pixel_cloud_mask=True)
        t_whole = time.perf_counter() - t

        native = ua._is_native_tile_grid
        ua._is_native_tile_grid = lambda *a: False
        try:
            t = time.perf_counter()
            vsi = p.read_scene_bands(scene, "landsat", ath.BANDS, grid, pixel_cloud_mask=True)
            t_vsi = time.perf_counter() - t
        finally:
            ua._is_native_tile_grid = native

        same = all(np.array_equal(whole[b], vsi[b], equal_nan=True) for b in ath.BANDS)
        results.append({"id": i, "whole_s": t_whole, "vsicurl_s": t_vsi, "identical": same})
        print(f"{i[:25]:<26}{t_whole:>9.1f}{t_vsi:>11.1f}{t_vsi / t_whole:>8.1f}x  {same}", flush=True)

    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{time.strftime('%Y%m%dT%H%M%S')}.json"
    path.write_text(json.dumps({"tile": ath.TILE, "results": results}, indent=1))
    print(f"wrote {path.relative_to(REPO)}")


if __name__ == "__main__":
    main()
