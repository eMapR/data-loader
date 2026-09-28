"""Per-pixel cloud/shadow masks as pure numpy functions.

Both return a boolean array where True = pixel should be excluded (bad).
Bit/value choices match GeoTimeSeriesApp3's gee_export/export_timeseries.py
so results stay consistent whether an AOI is fetched through this loader
or through the GEE batch pipeline.
"""
from __future__ import annotations

import numpy as np

# Collection 2 Level-2 QA_PIXEL bits: 1=dilated cloud, 2=cirrus, 3=cloud,
# 4=cloud shadow. Bit 0 (fill) is already implied by nodata elsewhere.
LANDSAT_QA_BAD_BITS = (1, 2, 3, 4)

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


MASKS = {
    "landsat_qa_pixel": landsat_qa_mask,
    "sentinel2_scl": sentinel2_scl_mask,
    "glad_ard_qa": glad_ard_qa_mask,
}
