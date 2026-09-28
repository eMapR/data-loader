"""Static benchmark case matrix -- data only, no control flow. See
run_case.py for how one case is executed and run_benchmark.py for how cases
are expanded into repetitions and dispatched.

AOI/date choices deliberately ALIGN across providers for the same sensor
(same bbox, same date range) so results can later support two different
comparisons: a "real-world acquisition benchmark" (each provider finds
whatever it finds for the same request) and, once scene IDs are cross-
referenced from the recorded `scenes` list, a "controlled transport
benchmark" restricted to the same underlying acquisition(s).

`comparison_group` tags which providers are fair to compare directly:
- "landsat_c2l2": all providers serving the same USGS Collection 2 Level-2
  product (planetary_computer, aws_earth_search, usgs_m2m, gee).
- "sentinel2_l2a": providers serving the same Sentinel-2 L2A product
  (planetary_computer, aws_earth_search).
- "glad_ard": GLAD ARD's 16-day composite product -- a different product
  with different temporal semantics, kept out of the landsat_c2l2 group
  intentionally (see the architecture review).
"""
from __future__ import annotations

# --- AOIs (bbox = west, south, east, north; EPSG:4326) ---

# ~8km x 6km, Oregon Coast Range -- same AOI as examples/annual_composite.yaml.
# Entirely inside one GLAD ARD 1-degree tile (123W_44N): floor(-122.4327) ==
# floor(-122.34258) == -123, floor(44.23148) == floor(44.28595) == 44. Used
# as the GLAD "native window, no tile crossing" control case.
SMALL_AOI = (-122.4327, 44.23148, -122.34258, 44.28595)

# ~1.5km x 1.5km -- same AOI as examples/scene_mode.yaml / usgs_m2m.yaml.
# Used only for the Sentinel-2 PC/AWS pair.
TINY_AOI = (-123.855, 45.882, -123.835, 45.896)

# ~40km x 45km, expanded from SMALL_AOI, still a single UTM zone.
MEDIUM_AOI = (-122.75, 44.05, -122.25, 44.45)

# Deliberately straddles the -123 degree longitude line: floor(-123.05) ==
# -124, floor(-122.95) == -123 -- two adjacent GLAD ARD tiles (124W_44N and
# 123W_44N). Kept small (~8km x 11km) so this is a cheap correctness probe,
# not a scale test.
GLAD_CROSSING_AOI = (-123.05, 44.20, -122.95, 44.30)

# --- Oregon statewide-workload benchmark AOIs (2026-09 inventory) ---
# Centers taken from real scene centroids at each WRS-2 path/row (verified
# live against Planetary Computer, not guessed): west=path046/row029,
# central=path044/row029 (also the path/row with the largest mean Oregon
# overlap area in the full inventory, ~35,362 km^2/scene), east=path042/
# row030. Same ~8km x 6km footprint as SMALL_AOI for a controlled
# per-region comparison; MEDIUM_AOI (already defined above) is reused as
# the "large AOI" stratum.
OREGON_WEST_AOI = SMALL_AOI  # Coast Range, already the project's default AOI
OREGON_CENTRAL_AOI = (-119.85, 44.56, -119.75, 44.62)  # near path044/row029 centroid
OREGON_EAST_AOI = (-117.23, 43.14, -117.13, 43.20)  # near path042/row030 centroid

COMMON_BANDS = ("blue", "green", "red", "nir", "swir1", "swir2")
COMMON_INDICES = ("nbr", "ndvi")

CASES = [
    dict(
        case_name="pc_landsat_small_1month",
        comparison_group="landsat_c2l2",
        provider="planetary_computer", provider_options={},
        sensor="landsat", aoi_bbox=SMALL_AOI, resolution_m=30.0,
        date_start="2023-07-01", date_end="2023-07-31",
        season_start=None, season_end=None,
        max_cloud_percent=60, pixel_cloud_mask=True,
        bands=list(COMMON_BANDS), indices=list(COMMON_INDICES),
        temporal_mode="scene", composite_mode="scene", reduce="median",
    ),
    dict(
        case_name="pc_landsat_medium_1year_season",
        comparison_group="landsat_c2l2",
        provider="planetary_computer", provider_options={},
        sensor="landsat", aoi_bbox=MEDIUM_AOI, resolution_m=30.0,
        date_start="2023-01-01", date_end="2023-12-31",
        season_start="06-01", season_end="09-15",
        max_cloud_percent=60, pixel_cloud_mask=True,
        bands=list(COMMON_BANDS), indices=list(COMMON_INDICES),
        temporal_mode="annual_composite", composite_mode="local_reduce",
        composite_year=2023, reduce="median",
    ),
    dict(
        case_name="pc_sentinel2_small_1month",
        comparison_group="sentinel2_l2a",
        provider="planetary_computer", provider_options={},
        sensor="sentinel2", aoi_bbox=TINY_AOI, resolution_m=10.0,
        date_start="2020-07-01", date_end="2020-07-31",
        season_start=None, season_end=None,
        max_cloud_percent=60, pixel_cloud_mask=True,
        bands=list(COMMON_BANDS), indices=list(COMMON_INDICES),
        temporal_mode="scene", composite_mode="scene", reduce="median",
    ),
    dict(
        case_name="aws_es_sentinel2_small_1month",
        comparison_group="sentinel2_l2a",
        provider="aws_earth_search", provider_options={},
        sensor="sentinel2", aoi_bbox=TINY_AOI, resolution_m=10.0,
        date_start="2020-07-01", date_end="2020-07-31",
        season_start=None, season_end=None,
        max_cloud_percent=60, pixel_cloud_mask=True,
        bands=list(COMMON_BANDS), indices=list(COMMON_INDICES),
        temporal_mode="scene", composite_mode="scene", reduce="median",
    ),
    dict(
        case_name="usgs_m2m_landsat_small_1month",
        comparison_group="landsat_c2l2",
        provider="usgs_m2m", provider_options={},
        sensor="landsat", aoi_bbox=SMALL_AOI, resolution_m=30.0,
        date_start="2023-07-01", date_end="2023-07-31",
        season_start=None, season_end=None,
        max_cloud_percent=60, pixel_cloud_mask=True,
        bands=list(COMMON_BANDS), indices=list(COMMON_INDICES),
        temporal_mode="scene", composite_mode="scene", reduce="median",
        notes=(
            "AOI/date deliberately match pc_landsat_small_1month and "
            "gee_landsat_small_1month (not the historical examples/"
            "usgs_m2m.yaml AOI/date) so scene IDs can later be cross-"
            "referenced for the controlled transport benchmark."
        ),
    ),
    dict(
        case_name="gee_landsat_small_1month",
        comparison_group="landsat_c2l2",
        provider="gee", provider_options={},
        sensor="landsat", aoi_bbox=SMALL_AOI, resolution_m=30.0,
        date_start="2023-07-01", date_end="2023-07-31",
        season_start=None, season_end=None,
        max_cloud_percent=60, pixel_cloud_mask=True,
        bands=list(COMMON_BANDS), indices=list(COMMON_INDICES),
        temporal_mode="scene", composite_mode="scene", reduce="median",
    ),
    dict(
        case_name="gee_landsat_medium_1year_season",
        comparison_group="landsat_c2l2",
        provider="gee", provider_options={},
        sensor="landsat", aoi_bbox=MEDIUM_AOI, resolution_m=30.0,
        date_start="2023-01-01", date_end="2023-12-31",
        season_start="06-01", season_end="09-15",
        max_cloud_percent=60, pixel_cloud_mask=True,
        bands=list(COMMON_BANDS), indices=list(COMMON_INDICES),
        temporal_mode="annual_composite", composite_mode="fast_path",
        composite_year=2023, reduce="median",
        notes=(
            "Uses GEE's server-side read_annual_composite fast path "
            "(gee.py:223), not the local-reduce path -- this is the normal "
            "way a GEE-based workflow would build an annual composite, and "
            "is the actual GEE-as-baseline comparison point against "
            "pc_landsat_medium_1year_season."
        ),
    ),
    dict(
        case_name="glad_ard_landsat_native_window",
        comparison_group="glad_ard",
        provider="glad_ard", provider_options={},
        sensor="landsat", aoi_bbox=SMALL_AOI, resolution_m=30.0,
        date_start="2023-01-01", date_end="2023-01-16",
        season_start=None, season_end=None,
        max_cloud_percent=60, pixel_cloud_mask=True,
        bands=list(COMMON_BANDS), indices=list(COMMON_INDICES),
        temporal_mode="scene", composite_mode="scene", reduce="median",
        glad_tile_check=True, expected_seam_boundary=None,
        notes=(
            "Single native 16-day GLAD interval, AOI entirely within one "
            "tile -- control case for the tile-crossing case below."
        ),
    ),
    dict(
        case_name="glad_ard_landsat_tile_crossing",
        comparison_group="glad_ard",
        provider="glad_ard", provider_options={},
        sensor="landsat", aoi_bbox=GLAD_CROSSING_AOI, resolution_m=30.0,
        date_start="2023-01-01", date_end="2023-01-16",
        season_start=None, season_end=None,
        max_cloud_percent=60, pixel_cloud_mask=True,
        bands=list(COMMON_BANDS), indices=list(COMMON_INDICES),
        temporal_mode="scene", composite_mode="scene", reduce="median",
        glad_tile_check=True,
        expected_seam_boundary={"axis": "lon", "value": -123.0},
        notes=(
            "Deliberately spans two GLAD ARD tiles (124W_44N, 123W_44N). "
            "A successful run is NOT validation of the mosaic logic -- see "
            "glad_tile_check.seam_flag and its note in the result record, "
            "and inspect the output GeoTIFF manually before trusting it."
        ),
    ),

    # --- Oregon 2000-2025 statewide-workload representative sample ---
    # Stratified across west/central/east Oregon, small vs. large AOI, and
    # date windows chosen to fall within different Landsat-era operational
    # windows (actual platform/cloud composition per case is measured, not
    # assumed -- see the accompanying report).
    dict(
        case_name="oregon_west_small_2023_summer",
        comparison_group="landsat_c2l2",
        provider="planetary_computer", provider_options={},
        sensor="landsat", aoi_bbox=OREGON_WEST_AOI, resolution_m=30.0,
        date_start="2023-06-01", date_end="2023-08-31",
        season_start=None, season_end=None,
        max_cloud_percent=100, pixel_cloud_mask=True,
        bands=list(COMMON_BANDS), indices=list(COMMON_INDICES),
        temporal_mode="scene", composite_mode="scene", reduce="median",
        notes="West OR (Coast Range), recent L8/L9 era, summer.",
    ),
    dict(
        case_name="oregon_west_small_2023_winter",
        comparison_group="landsat_c2l2",
        provider="planetary_computer", provider_options={},
        sensor="landsat", aoi_bbox=OREGON_WEST_AOI, resolution_m=30.0,
        date_start="2023-12-01", date_end="2024-01-31",
        season_start=None, season_end=None,
        max_cloud_percent=100, pixel_cloud_mask=True,
        bands=list(COMMON_BANDS), indices=list(COMMON_INDICES),
        temporal_mode="scene", composite_mode="scene", reduce="median",
        notes="West OR, winter -- expected higher cloud fraction.",
    ),
    dict(
        case_name="oregon_west_small_2005_l5era",
        comparison_group="landsat_c2l2",
        provider="planetary_computer", provider_options={},
        sensor="landsat", aoi_bbox=OREGON_WEST_AOI, resolution_m=30.0,
        date_start="2005-06-01", date_end="2005-08-31",
        season_start=None, season_end=None,
        max_cloud_percent=100, pixel_cloud_mask=True,
        bands=list(COMMON_BANDS), indices=list(COMMON_INDICES),
        temporal_mode="scene", composite_mode="scene", reduce="median",
        notes="West OR, Landsat 5/7 era.",
    ),
    dict(
        case_name="oregon_central_small_2023_summer",
        comparison_group="landsat_c2l2",
        provider="planetary_computer", provider_options={},
        sensor="landsat", aoi_bbox=OREGON_CENTRAL_AOI, resolution_m=30.0,
        date_start="2023-06-01", date_end="2023-08-31",
        season_start=None, season_end=None,
        max_cloud_percent=100, pixel_cloud_mask=True,
        bands=list(COMMON_BANDS), indices=list(COMMON_INDICES),
        temporal_mode="scene", composite_mode="scene", reduce="median",
        notes="Central OR (path044/row029, largest mean Oregon overlap area in the inventory), recent era.",
    ),
    dict(
        case_name="oregon_central_small_2015_l7l8era",
        comparison_group="landsat_c2l2",
        provider="planetary_computer", provider_options={},
        sensor="landsat", aoi_bbox=OREGON_CENTRAL_AOI, resolution_m=30.0,
        date_start="2015-06-01", date_end="2015-08-31",
        season_start=None, season_end=None,
        max_cloud_percent=100, pixel_cloud_mask=True,
        bands=list(COMMON_BANDS), indices=list(COMMON_INDICES),
        temporal_mode="scene", composite_mode="scene", reduce="median",
        notes="Central OR, Landsat 7/8 overlap era.",
    ),
    dict(
        case_name="oregon_east_small_2023_summer",
        comparison_group="landsat_c2l2",
        provider="planetary_computer", provider_options={},
        sensor="landsat", aoi_bbox=OREGON_EAST_AOI, resolution_m=30.0,
        date_start="2023-06-01", date_end="2023-08-31",
        season_start=None, season_end=None,
        max_cloud_percent=100, pixel_cloud_mask=True,
        bands=list(COMMON_BANDS), indices=list(COMMON_INDICES),
        temporal_mode="scene", composite_mode="scene", reduce="median",
        notes="East OR (near Vale/Malheur County, path042/row030), recent era.",
    ),
    dict(
        case_name="oregon_east_small_2023_winter",
        comparison_group="landsat_c2l2",
        provider="planetary_computer", provider_options={},
        sensor="landsat", aoi_bbox=OREGON_EAST_AOI, resolution_m=30.0,
        date_start="2023-12-01", date_end="2024-01-31",
        season_start=None, season_end=None,
        max_cloud_percent=100, pixel_cloud_mask=True,
        bands=list(COMMON_BANDS), indices=list(COMMON_INDICES),
        temporal_mode="scene", composite_mode="scene", reduce="median",
        notes="East OR, winter -- expected higher cloud/snow fraction.",
    ),
    dict(
        case_name="oregon_large_aoi_2023_summer",
        comparison_group="landsat_c2l2",
        provider="planetary_computer", provider_options={},
        sensor="landsat", aoi_bbox=MEDIUM_AOI, resolution_m=30.0,
        date_start="2023-06-01", date_end="2023-08-31",
        season_start=None, season_end=None,
        max_cloud_percent=100, pixel_cloud_mask=True,
        bands=list(COMMON_BANDS), indices=list(COMMON_INDICES),
        temporal_mode="scene", composite_mode="scene", reduce="median",
        notes="Large-AOI stratum (~40km x 45km, ~1341x1490px grid) -- tests read cost scaling with output grid size, not scene footprint size.",
    ),
]
