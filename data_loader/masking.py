"""Per-pixel cloud/shadow masks as pure numpy functions.

Both return a boolean array where True = pixel should be excluded (bad).
Bit/value choices match GeoTimeSeriesApp3's gee_export/export_timeseries.py
so results stay consistent whether an AOI is fetched through this loader
or through the GEE batch pipeline.
"""
from __future__ import annotations

import numpy as np

# Collection 2 Level-2 QA_PIXEL bits: 1=dilated cloud, 2=cirrus, 3=cloud,
# 4=cloud shadow. Bit 0 (fill) is not a cloud-mask choice: fill is masked
# unconditionally from the SR bands themselves (dn_to_reflectance), so it
# becomes NaN even with pixel_cloud_mask off.
LANDSAT_QA_BAD_BITS = (1, 2, 3, 4)

# Surface-reflectance DN 0 is fill/nodata for Landsat C2 SR and Sentinel-2
# L2A alike: their SR COGs declare nodata=0 (checked 2026-10-03 on USGS ARD
# and Planetary Computer files), and valid C2 SR DN starts at 7273. GDAL's
# WarpedVRT honors that nodata, so warped reads also return 0 where there is
# no valid source pixel.
SR_FILL_DN = 0

# Sentinel-2 Scene Classification (SCL) values: 3=cloud shadow,
# 8/9=cloud medium/high probability, 10=thin cirrus.
SENTINEL2_SCL_BAD_VALUES = (3, 8, 9, 10)

# GLAD ARD QA band: categorical class codes (not bitflags, despite the name),
# per Potapov et al. 2020 (https://doi.org/10.3390/rs12030426) and confirmed
# against github.com/TESS-Laboratory/ardglad's ard-glad-mask.R default
# keep_bits=c(1,2,15). 0=no data, 1=land, 2=water, 3=cloud, 4=cloud shadow,
# 5=topographic shadow, 6=snow/ice, 7=haze, 8-14=proximity/other-shadow
# classes, 15=land (dup of 1), 16=dup of 11, 17=dup of 14. Keep only clear
# land/water.
GLAD_ARD_QA_GOOD_VALUES = (1, 2, 15)


def dn_to_reflectance(dn: np.ndarray, scale: float, offset: float) -> np.ndarray:
    """SR DN -> float32 reflectance, with fill (DN 0) as NaN. Without this,
    fill came out as `offset` (-0.2 for Landsat C2, 0.0 for Sentinel-2) and
    passed through composites and indices as if it were real reflectance."""
    out = dn.astype("f4") * scale + offset
    out[dn == SR_FILL_DN] = np.nan
    return out


def glad_ard_qa_mask(qa: np.ndarray, good_values=GLAD_ARD_QA_GOOD_VALUES) -> np.ndarray:
    return ~np.isin(qa, good_values)


def landsat_qa_mask(qa_pixel: np.ndarray, bad_bits=LANDSAT_QA_BAD_BITS) -> np.ndarray:
    qi = qa_pixel.astype(np.uint32)
    bad = np.zeros(qi.shape, bool)
    for b in bad_bits:
        bad |= ((qi >> b) & 1).astype(bool)
    return bad


def sentinel2_scl_mask(scl: np.ndarray, bad_values=SENTINEL2_SCL_BAD_VALUES) -> np.ndarray:
    bad = np.zeros(scl.shape, bool)
    for v in bad_values:
        bad |= scl == v
    return bad


# What the QA bands mean and which values the masks above treat as bad --
# written into every manifest so downstream code never has to look up a
# provider's QA conventions. Landsat: USGS Collection 2 Level-2 QA_PIXEL;
# Sentinel-2: ESA L2A Scene Classification.
LANDSAT_QA_PIXEL_BITS = {
    0: "fill", 1: "dilated_cloud", 2: "cirrus", 3: "cloud", 4: "cloud_shadow", 5: "snow",
    6: "clear", 7: "water", "8-9": "cloud_confidence", "10-11": "cloud_shadow_confidence",
    "12-13": "snow_ice_confidence", "14-15": "cirrus_confidence",
}
SENTINEL2_SCL_CLASSES = {
    0: "no_data", 1: "saturated_or_defective", 2: "dark_area_pixels", 3: "cloud_shadows",
    4: "vegetation", 5: "not_vegetated", 6: "water", 7: "unclassified", 8: "cloud_medium_probability",
    9: "cloud_high_probability", 10: "thin_cirrus", 11: "snow",
}
GLAD_ARD_QA_CLASSES = {
    0: "no_data", 1: "land", 2: "water", 3: "cloud", 4: "cloud_shadow", 5: "topographic_shadow",
    6: "snow_ice", 7: "haze", 15: "land",
}

QA_DESCRIPTIONS = {
    "landsat_qa_pixel": {"name": "qa_pixel", "encoding": "bitmask",
                         "reference": "USGS Landsat Collection 2 Level-2 QA_PIXEL",
                         "bits": {str(k): v for k, v in LANDSAT_QA_PIXEL_BITS.items()}},
    "sentinel2_scl": {"name": "scl", "encoding": "classes",
                      "reference": "ESA Sentinel-2 L2A Scene Classification (SCL)",
                      "classes": {str(k): v for k, v in SENTINEL2_SCL_CLASSES.items()}},
    "glad_ard_qa": {"name": "qa", "encoding": "classes",
                    "reference": "GLAD ARD QA (Potapov et al. 2020)",
                    "classes": {str(k): v for k, v in GLAD_ARD_QA_CLASSES.items()}},
}


def describe_mask(qa_kind: str) -> dict:
    """Exactly which QA values pixel_cloud_mask turns into nodata."""
    if qa_kind == "landsat_qa_pixel":
        return {"qaBand": "qa_pixel", "rule": "masked if any listed bit is set",
                "bits": {str(b): LANDSAT_QA_PIXEL_BITS[b] for b in LANDSAT_QA_BAD_BITS}}
    if qa_kind == "sentinel2_scl":
        return {"qaBand": "scl", "rule": "masked if the class is listed",
                "classes": {str(v): SENTINEL2_SCL_CLASSES[v] for v in SENTINEL2_SCL_BAD_VALUES}}
    if qa_kind == "glad_ard_qa":
        return {"qaBand": "qa", "rule": "kept only if the class is listed",
                "keptClasses": {str(v): GLAD_ARD_QA_CLASSES[v] for v in GLAD_ARD_QA_GOOD_VALUES}}
    raise ValueError(f"unknown qa kind {qa_kind!r}")


MASKS = {
    "landsat_qa_pixel": landsat_qa_mask,
    "sentinel2_scl": sentinel2_scl_mask,
    "glad_ard_qa": glad_ard_qa_mask,
}
