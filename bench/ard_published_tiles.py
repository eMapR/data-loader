#!/usr/bin/env python
"""Which ARD tiles USGS actually publishes (has any SR item for), per
region, over the full h/v range of each grid. Writes the list the library
uses to tell "missing from the library" apart from "not published by USGS".

    python bench/ard_published_tiles.py --out library/products/landsat_ard_sr/c2/published_tiles.json

One LandsatLook STAC search per tile (public, no M2M account). Re-run when
USGS adds tiles; the file records when it was made.
"""
from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from data_loader.providers.usgs_ard import COLLECTION, STAC_URL  # noqa: E402
from data_loader.tiles import ARD_REGIONS, tile_id_for  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True)
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args()
    from pystac_client import Client

    def exists(code, h, v):
        c = Client.open(STAC_URL)
        s = c.search(collections=[COLLECTION], max_items=1, query={
            "landsat:grid_region": {"eq": code}, "landsat:grid_horizontal": {"eq": f"{h:02d}"},
            "landsat:grid_vertical": {"eq": f"{v:02d}"}})
        return tile_id_for(h, v, code), any(True for _ in s.items())

    out = {"made": date.today().isoformat(), "source": f"{STAC_URL} collection {COLLECTION}",
           "note": ("ARD tiles USGS publishes SR items for (bench/ard_published_tiles.py); other h/v positions "
                    "of each grid are ocean or outside the US"), "tiles": {}}
    for code, r in sorted(ARD_REGIONS.items()):
        jobs = [(code, h, v) for h in range(r.max_h + 1) for v in range(r.max_v + 1)]
        with ThreadPoolExecutor(a.workers) as ex:
            res = list(ex.map(lambda j: exists(*j), jobs))
        pub = sorted(t for t, ok in res if ok)
        out["tiles"][r.system] = pub
        print(f"{code} ({r.system}): {len(pub)} of {len(jobs)} h/v positions published")
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(out, indent=1) + "\n")
    print(f"wrote {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
