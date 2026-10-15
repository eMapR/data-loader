#!/usr/bin/env python
"""Sentinel-2 L2A reads from CDSE (ESA JP2 on eodata S3) vs Planetary
Computer (COG), live -- the go/no-go measurement for a CDSE provider.

For a few clear 10TDQ scenes found on both catalogs, reads B04_10m,
B11_20m and SCL_20m in native pixel windows of 100, 1000 and 10980 px
(1 km, 10 km, full tile at 10 m; 20 m bands get the matching half-size
window) through:
    pc          signed /vsicurl COG windowed read (today's S2 path)
    cdse        /vsis3 JP2 windowed read, GDAL defaults
    cdse_tuned  /vsis3 JP2 windowed read, 10 MB curl chunks + VSI cache
    cdse_whole  whole JP2 fetched with one signed GET, decoded from memory
Each (method, scene, size) runs in its own subprocess so GDAL's network
statistics (CPL_VSIL_SHOW_NETWORK_STATS) and caches are per case. Reports
seconds, GET count, MB downloaded, and whether CDSE DN equal PC DN (only
expected when both catalogs hold the same processing baseline).

Needs CDSE S3 keys (https://eodata-s3keysmanager.dataspace.copernicus.eu)
in CDSE_S3_ACCESS_KEY / CDSE_S3_SECRET_KEY; Planetary Computer needs none.

    python bench/cdse_read_bench.py [--n 2] [--sizes 100,1000,10980]
    python bench/cdse_read_bench.py --summarize bench/results/cdse_read_bench/<timestamp>.json

Writes bench/results/cdse_read_bench/<timestamp>.json; --summarize writes
the compact docs/development/benchmarks/data/cdse_read_bench.json.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import hmac
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "bench" / "results" / "cdse_read_bench"
SUMMARY = REPO / "docs" / "development" / "benchmarks" / "data" / "cdse_read_bench.json"
CDSE_STAC = "https://stac.dataspace.copernicus.eu/v1"
PC_STAC = "https://planetarycomputer.microsoft.com/api/stac/v1"
S3_HOST = "eodata.dataspace.copernicus.eu"
TILE, BBOX = "10TDQ", [-123.3, 44.5, -123.2, 44.6]
# CDSE asset key -> (PC asset key, pixel size in m)
BANDS = {"B04_10m": ("B04", 10), "B11_20m": ("B11", 20), "SCL_20m": ("SCL", 20)}
METHODS = ("pc", "cdse", "cdse_tuned", "cdse_whole")
CASE_TIMEOUT_S = 900


def _creds() -> tuple[str, str]:
    return os.environ["CDSE_S3_ACCESS_KEY"], os.environ["CDSE_S3_SECRET_KEY"]


def _post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=120))


def _acq_key(item_id: str) -> str:
    """Satellite + sensing time + orbit + tile: the same acquisition on
    both catalogs (ids differ in the baseline and processing-time parts)."""
    p = item_id.split("_")
    return "_".join([p[0], p[2]] + [x for x in p[3:] if x[0] in "RT" and x[1:3].isdigit()])


def discover(n: int) -> list[dict]:
    span = "2024-06-01T00:00:00Z/2024-09-30T23:59:59Z"
    q = {"bbox": BBOX, "datetime": span, "limit": 200}
    cdse = _post(f"{CDSE_STAC}/search", {**q, "collections": ["sentinel-2-l2a"]})["features"]
    pc = _post(f"{PC_STAC}/search", {**q, "collections": ["sentinel-2-l2a"]})["features"]
    pc_by = {_acq_key(f["id"]): f for f in pc if f["properties"].get("s2:mgrs_tile") == TILE}
    pairs = []
    for f in sorted(cdse, key=lambda f: f["properties"]["eo:cloud_cover"]):
        if f["properties"].get("grid:code") != f"MGRS-{TILE}" or _acq_key(f["id"]) not in pc_by:
            continue
        g = pc_by[_acq_key(f["id"])]
        pairs.append({
            "acquisition": _acq_key(f["id"]), "cloud": f["properties"]["eo:cloud_cover"],
            "cdse_id": f["id"], "cdse_baseline": f["properties"].get("processing:version"),
            "pc_id": g["id"], "pc_baseline": g["properties"].get("s2:processing_baseline"),
            "cdse": {b: f["assets"][b]["href"] for b in BANDS},
            "pc": {b: g["assets"][pc_key]["href"] for b, (pc_key, _) in BANDS.items()},
        })
        if len(pairs) == n:
            break
    return pairs


# -- child: one (method, scene, size) case -------------------------------

def _sigv4_get(key: str, secret: str, s3_path: str) -> bytes:
    """One signed GET of s3://<s3_path> (path-style; CDSE ignores region)."""
    now = dt.datetime.now(dt.timezone.utc)
    amz, day = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    uri = "/" + urllib.parse.quote(s3_path)
    payload = hashlib.sha256(b"").hexdigest()
    headers = {"host": S3_HOST, "x-amz-content-sha256": payload, "x-amz-date": amz}
    signed = ";".join(sorted(headers))
    canon = "\n".join(["GET", uri, "", "".join(f"{k}:{headers[k]}\n" for k in sorted(headers)), signed, payload])
    scope = f"{day}/default/s3/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz, scope, hashlib.sha256(canon.encode()).hexdigest()])
    k = ("AWS4" + secret).encode()
    for part in (day, "default", "s3", "aws4_request"):
        k = hmac.new(k, part.encode(), hashlib.sha256).digest()
    sig = hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()
    auth = f"AWS4-HMAC-SHA256 Credential={key}/{scope}, SignedHeaders={signed}, Signature={sig}"
    req = urllib.request.Request(f"https://{S3_HOST}{uri}", headers={**headers, "Authorization": auth})
    with urllib.request.urlopen(req, timeout=600) as r:
        return r.read()


def child(method: str, pair: dict, size: int, dn_dir: Path) -> dict:
    if method != "pc":
        key, secret = _creds()
        os.environ.update(AWS_ACCESS_KEY_ID=key, AWS_SECRET_ACCESS_KEY=secret, AWS_S3_ENDPOINT=S3_HOST,
                          AWS_VIRTUAL_HOSTING="FALSE", AWS_HTTPS="YES")
    os.environ["GDAL_DISABLE_READDIR_ON_OPEN"] = "EMPTY_DIR"
    if method == "cdse_tuned":
        os.environ.update(CPL_VSIL_CURL_CHUNK_SIZE="10485760", VSI_CACHE="TRUE", VSI_CACHE_SIZE="1000000000")
    import numpy as np
    import rasterio
    from rasterio.io import MemoryFile
    from rasterio.windows import Window

    if method == "pc":
        import planetary_computer
        hrefs = {b: planetary_computer.sign(h) for b, h in pair["pc"].items()}
    else:
        hrefs = pair["cdse"]
    whole_gets = whole_bytes = 0
    t0 = time.perf_counter()
    for b, (_, res) in BANDS.items():
        px = max(1, size * 10 // res)
        off = 0 if size >= 10980 else 5000 * 10 // res
        win = Window(off, off, px, px)
        if method == "cdse_whole":
            blob = _sigv4_get(*_creds(), hrefs[b][len("s3://"):])
            whole_gets, whole_bytes = whole_gets + 1, whole_bytes + len(blob)
            with MemoryFile(blob) as mf, mf.open() as ds:
                a = ds.read(1, window=win)
        else:
            src = hrefs[b].replace("s3://", "/vsis3/") if method != "pc" else "/vsicurl/" + hrefs[b]
            with rasterio.open(src) as ds:
                a = ds.read(1, window=win)
        np.save(dn_dir / f"{method}_{b}.npy", a)
    out = {"seconds": time.perf_counter() - t0}
    if method == "cdse_whole":  # urllib, not GDAL: count it here
        out.update(gets=whole_gets, mb=whole_bytes / 1e6)
    return out


def _net_stats(stderr: str) -> dict:
    i = stderr.find("Network statistics:")
    if i < 0:
        return {}
    s = json.JSONDecoder().raw_decode(stderr[stderr.index("{", i):])[0]
    get = s.get("methods", {}).get("GET", {})
    return {"gets": get.get("count", 0), "mb": get.get("downloaded_bytes", 0) / 1e6}


def summarize(raw: Path) -> None:
    """Per (size, method): seconds/GETs/MB for each scene; per scene: the
    two product ids and whether CDSE DN equalled PC DN."""
    d = json.loads(raw.read_text())
    cases = {}
    for r in d["results"]:
        if "error" in r:
            continue
        c = cases.setdefault(f"{r['size']}px/{r['method']}", {"seconds": [], "gets": [], "mb": []})
        c["seconds"].append(round(r["seconds"], 1))
        c["gets"].append(r.get("gets"))
        c["mb"].append(round(r.get("mb", 0), 1))
    scenes = [{"acquisition": p["acquisition"], "cdse_id": p["cdse_id"], "pc_id": p["pc_id"],
               "dn_equal_pc": sorted({r["dn_equal_pc"] for r in d["results"]
                                      if r["acquisition"] == p["acquisition"] and r["dn_equal_pc"] is not None})}
              for p in d["pairs"]]
    SUMMARY.write_text(json.dumps({"source": raw.name, "tile": d["tile"], "bands": d["bands"],
                                   "scenes": scenes, "cases": cases}, indent=1) + "\n")
    print(f"wrote {SUMMARY.relative_to(REPO)}")
    for k, c in cases.items():
        print(f"{k:<20} s {c['seconds']}  MB {c['mb']}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--n", type=int, default=2, help="scenes")
    ap.add_argument("--sizes", default="100,1000,10980", help="window edge in 10 m pixels")
    ap.add_argument("--methods", default=",".join(METHODS))
    ap.add_argument("--summarize", metavar="RAW_JSON", help="write the compact docs summary from a raw result")
    ap.add_argument("--_child", nargs=4, help=argparse.SUPPRESS)
    args = ap.parse_args()
    if args.summarize:
        summarize(Path(args.summarize))
        return
    if "--_child" not in sys.argv and not all(os.environ.get(k) for k in ("CDSE_S3_ACCESS_KEY", "CDSE_S3_SECRET_KEY")):
        sys.exit("set CDSE_S3_ACCESS_KEY and CDSE_S3_SECRET_KEY")
    if args._child:
        method, pair_file, size, dn_dir = args._child
        print("RESULT " + json.dumps(child(method, json.loads(Path(pair_file).read_text()), int(size), Path(dn_dir))))
        return

    import numpy as np

    stamp = time.strftime("%Y%m%dT%H%M%S")
    work = OUT / stamp
    work.mkdir(parents=True, exist_ok=True)
    pairs = discover(args.n)
    for p in pairs:
        print(f"{p['acquisition']}  cloud {p['cloud']:.2f}  baseline cdse {p['cdse_baseline']} / pc {p['pc_baseline']}")
    print(f"\n{'scene':<10}{'px':>6} {'method':<11}{'s':>8}{'GETs':>7}{'MB':>9}  DN == pc")
    results = []
    for i, p in enumerate(pairs):
        pf = work / f"pair{i}.json"
        pf.write_text(json.dumps(p))
        for size in map(int, args.sizes.split(",")):
            dn_dir = work / f"pair{i}_{size}"
            dn_dir.mkdir(exist_ok=True)
            for m in args.methods.split(","):
                env = {**os.environ, "CPL_VSIL_SHOW_NETWORK_STATS": "YES"}
                cmd = [sys.executable, "-I", __file__, "--_child", m, str(pf), str(size), str(dn_dir)]
                try:
                    r = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=CASE_TIMEOUT_S)
                    line = next((l for l in r.stdout.splitlines() if l.startswith("RESULT ")), None)
                    res = json.loads(line[7:]) if line else {"error": (r.stderr.split("Network statistics:")[0].strip().splitlines() or ["?"])[-1][:200]}
                    res = {**_net_stats(r.stdout + r.stderr), **res}
                except subprocess.TimeoutExpired:
                    res = {"error": f"timeout {CASE_TIMEOUT_S}s"}
                same = None
                if m != "pc" and "error" not in res and (dn_dir / f"pc_{next(iter(BANDS))}.npy").exists():
                    same = all(np.array_equal(np.load(dn_dir / f"{m}_{b}.npy"), np.load(dn_dir / f"pc_{b}.npy"))
                               for b in BANDS)
                res.update(acquisition=p["acquisition"], size=size, method=m, dn_equal_pc=same)
                results.append(res)
                if "error" in res:
                    print(f"{p['acquisition'][:9]:<10}{size:>6} {m:<11}  ERROR {res['error']}", flush=True)
                else:
                    print(f"{p['acquisition'][:9]:<10}{size:>6} {m:<11}{res['seconds']:>8.1f}{res.get('gets', 0):>7}"
                          f"{res.get('mb', 0):>9.1f}  {'' if same is None else same}", flush=True)
            for f in dn_dir.glob("*.npy"):
                f.unlink()
    path = OUT / f"{stamp}.json"
    path.write_text(json.dumps({"tile": TILE, "bands": list(BANDS), "pairs": pairs, "results": results}, indent=1))
    print(f"wrote {path.relative_to(REPO)}")


if __name__ == "__main__":
    main()
