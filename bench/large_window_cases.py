"""Case matrix for the large-COG-window validation benchmark (see
docs/DATALOADER_DEVELOPMENT_REPORT.md, "Large-window COG read validation").

Purpose: the Oregon stress test's serial-runtime model was fit almost
entirely from small/medium AOI reads (largest actually timed: ~2,000,000
pixels, the `oregon_large_aoi_2023_summer` bench case at a 1341x1490 grid --
see bench/results/). A typical real per-scene Oregon intersection is ~19M
pixels. This case matrix reads real Planetary Computer Landsat COGs at
window sizes actually spanning that gap (~5M, ~10M, ~20M, ~30-40M pixels)
so the serial/concurrency runtime model can be checked against genuine
large reads instead of extrapolated ones.

Two real, low-cloud Landsat 9 scenes are used (verified live 2026-09-16):
- PRIMARY: LC09_L2SP_044029_20230810_02_T1 (path044/row029, central Oregon,
  0.01% cloud) -- the same path/row cases.py already calls out as the
  largest-mean-overlap-area scene in the Oregon inventory.
- SECONDARY: LC09_L2SP_046029_20230808_02_T1 (path046/row029, west Oregon,
  0.14% cloud) -- a different scene/date, used at two window sizes to check
  the finding isn't an artifact of one specific COG.

Windows are square, centered on each scene's own footprint centroid
(computed from its STAC geometry, not guessed), sized so pixel count at
30m resolution hits the target -- see the module-level `_window_bbox`
below for the geometry. `search_bbox` is a small bbox guaranteed to be
inside the scene footprint, used only to look the scene up by search
(cheap) rather than needing a direct STAC item-id GET API.
"""
from __future__ import annotations

import math

COMMON_BANDS = ("blue", "green", "red", "nir", "swir1", "swir2")

# Degrees-per-km at ~44.6N (both scenes' centroid latitude), computed from
# spherical approximations (1 deg lat ~ 110.57 km; 1 deg lon ~ 111.32*cos(lat) km)
# -- adequate for sizing a benchmark window, not a scientific reprojection.
_LAT_DEG_PER_KM = 1 / 110.57
_LON_DEG_PER_KM = 1 / (111.32 * math.cos(math.radians(44.6)))


def _window_bbox(center_lon: float, center_lat: float, target_pixels: float, res_m: float = 30.0):
    side_km = math.sqrt(target_pixels) * res_m / 1000.0
    half_lon = (side_km / 2) * _LON_DEG_PER_KM
    half_lat = (side_km / 2) * _LAT_DEG_PER_KM
    return (
        center_lon - half_lon, center_lat - half_lat,
        center_lon + half_lon, center_lat + half_lat,
    )


# Centroids computed live from each scene's actual STAC `geometry` polygon
# (average of the 4 corner vertices), not assumed from the search bbox.
PRIMARY_SCENE_ID = "LC09_L2SP_044029_20230810_02_T1"
PRIMARY_CENTROID = (-119.795, 44.599)
PRIMARY_SEARCH_BBOX = (-119.95, 44.46, -119.65, 44.72)
PRIMARY_DATE = "2023-08-10"

SECONDARY_SCENE_ID = "LC09_L2SP_046029_20230808_02_T1"
SECONDARY_CENTROID = (-122.8888, 44.5990)
SECONDARY_SEARCH_BBOX = (-122.75, 44.05, -122.25, 44.45)
SECONDARY_DATE = "2023-08-08"

CASES = [
    dict(
        window_name="primary_5M", scene_id=PRIMARY_SCENE_ID, date=PRIMARY_DATE,
        search_bbox=PRIMARY_SEARCH_BBOX, target_pixels=5_000_000,
        window_bbox=_window_bbox(*PRIMARY_CENTROID, 5_000_000),
        repetitions=2,
    ),
    dict(
        window_name="primary_10M", scene_id=PRIMARY_SCENE_ID, date=PRIMARY_DATE,
        search_bbox=PRIMARY_SEARCH_BBOX, target_pixels=10_000_000,
        window_bbox=_window_bbox(*PRIMARY_CENTROID, 10_000_000),
        repetitions=2,
    ),
    dict(
        window_name="primary_20M", scene_id=PRIMARY_SCENE_ID, date=PRIMARY_DATE,
        search_bbox=PRIMARY_SEARCH_BBOX, target_pixels=20_000_000,
        window_bbox=_window_bbox(*PRIMARY_CENTROID, 20_000_000),
        repetitions=1,
    ),
    dict(
        window_name="primary_35M", scene_id=PRIMARY_SCENE_ID, date=PRIMARY_DATE,
        search_bbox=PRIMARY_SEARCH_BBOX, target_pixels=35_000_000,
        window_bbox=_window_bbox(*PRIMARY_CENTROID, 35_000_000),
        repetitions=1,
    ),
    dict(
        window_name="secondary_10M", scene_id=SECONDARY_SCENE_ID, date=SECONDARY_DATE,
        search_bbox=SECONDARY_SEARCH_BBOX, target_pixels=10_000_000,
        window_bbox=_window_bbox(*SECONDARY_CENTROID, 10_000_000),
        repetitions=1,
    ),
    dict(
        window_name="secondary_20M", scene_id=SECONDARY_SCENE_ID, date=SECONDARY_DATE,
        search_bbox=SECONDARY_SEARCH_BBOX, target_pixels=20_000_000,
        window_bbox=_window_bbox(*SECONDARY_CENTROID, 20_000_000),
        repetitions=1,
    ),
]
