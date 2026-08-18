"""AOI/grid helpers. `target_grid` moved unchanged from the original
single-provider `data_loader.py`.
"""
from __future__ import annotations

import numpy as np


def auto_utm_epsg(bbox) -> str:
    """bbox (west, south, east, north) in EPSG:4326 -> a UTM zone EPSG code
    picked from the bbox centroid. Fine for small AOIs; pick a CRS
    explicitly for anything crossing a UTM zone boundary."""
    lon_c = (bbox[0] + bbox[2]) / 2
    lat_c = (bbox[1] + bbox[3]) / 2
    zone = int((lon_c + 180) // 6) + 1
    return f"EPSG:{32600 + zone if lat_c >= 0 else 32700 + zone}"


def target_grid(bbox, target_epsg, res):
    """bbox in EPSG:4326 -> (Affine transform, width, height) snapped to `res`."""
    from pyproj import Transformer
    from rasterio.transform import from_origin

    tf = Transformer.from_crs("EPSG:4326", target_epsg, always_xy=True)
    xs, ys = [], []
    for lon in (bbox[0], bbox[2]):
        for lat in (bbox[1], bbox[3]):
            x, y = tf.transform(lon, lat)
            xs.append(x)
            ys.append(y)
    xmin = np.floor(min(xs) / res) * res
    xmax = np.ceil(max(xs) / res) * res
    ymin = np.floor(min(ys) / res) * res
    ymax = np.ceil(max(ys) / res) * res
    w = int(round((xmax - xmin) / res))
    h = int(round((ymax - ymin) / res))
    return from_origin(xmin, ymax, res, res), w, h
